#!/usr/bin/env python3
"""har2mp4 - rebuild HLS videos captured in a HAR file into MP4 files.

Works fully offline: playlists, segments and AES-128 keys are taken from the
response bodies stored in the HAR. Nothing is downloaded.

Requirements: Python 3.9+, ffmpeg (including ffprobe), and openssl (only for AES-128 streams).

Examples:
    har2mp4.py capture.har                 # -> capture.mp4 next to the HAR
    har2mp4.py capture.har --list          # show detected videos / variants
    har2mp4.py capture.har --variant 720   # prefer a variant matching "720"
    har2mp4.py capture.har -o out.mp4 -f   # custom output, overwrite
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit


# --------------------------------------------------------------------------- #
# HAR access
# --------------------------------------------------------------------------- #

def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def _strip_query(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


@dataclass
class HarBody:
    url: str
    status: int
    data: bytes
    range_header: Optional[str]


class Har:
    """Index of HAR response bodies by URL, with lenient lookup."""

    def __init__(self, path: Path):
        with open(path, "r", encoding="utf-8") as fh:
            har = json.load(fh)
        self.bodies: list[HarBody] = []
        self._redirects: dict[str, str] = {}
        for entry in har.get("log", {}).get("entries", []):
            req = entry.get("request", {})
            resp = entry.get("response", {})
            url = req.get("url", "")
            if not url or url.startswith("data:"):
                continue
            target = resp.get("redirectURL")
            if target and 300 <= int(resp.get("status") or 0) < 400:
                self._redirects[_strip_fragment(url)] = urljoin(url, target)
                continue
            data = self._decode(resp.get("content", {}))
            if not data:
                continue
            rng = None
            for h in req.get("headers", []):
                if h.get("name", "").lower() == "range":
                    rng = h.get("value")
            self.bodies.append(HarBody(url, int(resp.get("status") or 0), data, rng))

        self._exact: dict[str, list[HarBody]] = {}
        self._noquery: dict[str, list[HarBody]] = {}
        for b in self.bodies:
            u = _strip_fragment(b.url)
            self._exact.setdefault(u, []).append(b)
            self._noquery.setdefault(_strip_query(u), []).append(b)

    @staticmethod
    def _decode(content: dict) -> bytes:
        text = content.get("text")
        if not text:
            return b""
        if content.get("encoding") == "base64":
            try:
                return base64.b64decode(text)
            except (ValueError, TypeError):
                return b""
        return text.encode("utf-8")

    def resolve(self, url: str) -> str:
        """Follow HTTP redirects recorded in the HAR."""
        u = _strip_fragment(url)
        for _ in range(10):
            nxt = self._redirects.get(u)
            if nxt is None:
                break
            u = _strip_fragment(nxt)
        return u

    def candidates(self, url: str) -> list[HarBody]:
        """Captured bodies for `url` (exact match, then ignoring the query string)."""
        u = self.resolve(url)
        for table, key in ((self._exact, u), (self._noquery, _strip_query(u))):
            if key in table:
                return list(reversed(table[key]))  # prefer latest capture
        return []

    def get(self, url: str, byterange: Optional[tuple[int, int]] = None) -> Optional[bytes]:
        """Return the body for `url` (optionally a byte range of it)."""
        cands = self.candidates(url)
        if byterange is None:
            for b in cands:
                if b.status != 206:  # partial bodies only make sense for byte ranges
                    return b.data
            return None
        offset, length = byterange
        # Full body captured: slice it.
        for b in cands:
            if b.status == 200 and len(b.data) >= offset + length:
                return b.data[offset:offset + length]
        # Partial content response with matching Range header.
        for b in cands:
            if b.status == 206 and b.range_header:
                m = re.match(r"bytes=(\d+)-(\d*)", b.range_header.strip())
                if m and int(m.group(1)) == offset and len(b.data) >= length:
                    return b.data[:length]
        return None

    def playlists(self) -> list[tuple[str, str]]:
        """(url, text) of every captured body that looks like an M3U8 playlist."""
        out, seen = [], set()
        for b in self.bodies:
            head = b.data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
            if not head.startswith(b"#EXTM3U"):
                continue
            key = _strip_query(_strip_fragment(b.url))
            if key in seen:
                continue
            seen.add(key)
            out.append((b.url, b.data.decode("utf-8", "replace")))
        return out


# --------------------------------------------------------------------------- #
# M3U8 parsing
# --------------------------------------------------------------------------- #

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attrs(s: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR_RE.findall(s)}


@dataclass
class Key:
    method: str
    uri: Optional[str]
    iv: Optional[bytes]


@dataclass
class Segment:
    uri: str
    duration: float
    seq: int
    key: Optional[Key]
    byterange: Optional[tuple[int, int]]
    init_uri: Optional[str]
    init_byterange: Optional[tuple[int, int]]
    discontinuity: bool


@dataclass
class MediaPlaylist:
    url: str
    segments: list[Segment]


@dataclass
class Variant:
    uri: str
    bandwidth: int
    resolution: str
    audio_group: Optional[str]


@dataclass
class Rendition:
    type: str
    group: str
    name: str
    uri: Optional[str]
    default: bool


@dataclass
class MasterPlaylist:
    url: str
    variants: list[Variant]
    renditions: list[Rendition]


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def _parse_byterange(value: str, last_end: int) -> tuple[int, int]:
    if "@" in value:
        length, offset = value.split("@", 1)
        return int(offset), int(length)
    return last_end, int(value)


def parse_master(url: str, text: str) -> MasterPlaylist:
    variants, renditions = [], []
    lines = _lines(text)
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#EXT-X-STREAM-INF:"):
            a = parse_attrs(ln.split(":", 1)[1])
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                j += 1
            if j < len(lines):
                variants.append(Variant(
                    uri=urljoin(url, lines[j]),
                    bandwidth=int(a.get("BANDWIDTH", "0") or 0),
                    resolution=a.get("RESOLUTION", ""),
                    audio_group=a.get("AUDIO"),
                ))
            i = j
        elif ln.startswith("#EXT-X-MEDIA:"):
            a = parse_attrs(ln.split(":", 1)[1])
            renditions.append(Rendition(
                type=a.get("TYPE", ""),
                group=a.get("GROUP-ID", ""),
                name=a.get("NAME", ""),
                uri=urljoin(url, a["URI"]) if a.get("URI") else None,
                default=a.get("DEFAULT", "NO") == "YES",
            ))
        i += 1
    return MasterPlaylist(url, variants, renditions)


def parse_media(url: str, text: str) -> MediaPlaylist:
    segments: list[Segment] = []
    seq = 0
    key: Optional[Key] = None
    init_uri: Optional[str] = None
    init_br: Optional[tuple[int, int]] = None
    duration = 0.0
    byterange: Optional[tuple[int, int]] = None
    last_end = 0
    disc = False
    for ln in _lines(text):
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(ln.split(":", 1)[1])
        elif ln.startswith("#EXT-X-KEY:"):
            a = parse_attrs(ln.split(":", 1)[1])
            method = a.get("METHOD", "NONE").upper()
            if method == "NONE":
                key = None
            else:
                iv = a.get("IV")
                key = Key(
                    method=method,
                    uri=urljoin(url, a["URI"]) if a.get("URI") else None,
                    iv=bytes.fromhex(iv[2:].rjust(32, "0")) if iv else None,
                )
        elif ln.startswith("#EXT-X-MAP:"):
            a = parse_attrs(ln.split(":", 1)[1])
            init_uri = urljoin(url, a["URI"]) if a.get("URI") else None
            init_br = _parse_byterange(a["BYTERANGE"], 0) if a.get("BYTERANGE") else None
        elif ln.startswith("#EXTINF:"):
            try:
                duration = float(ln.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                duration = 0.0
        elif ln.startswith("#EXT-X-BYTERANGE:"):
            byterange = _parse_byterange(ln.split(":", 1)[1], last_end)
            last_end = byterange[0] + byterange[1]
        elif ln.startswith("#EXT-X-DISCONTINUITY") and not ln.startswith("#EXT-X-DISCONTINUITY-"):
            disc = True
        elif not ln.startswith("#"):
            segments.append(Segment(
                uri=urljoin(url, ln), duration=duration, seq=seq, key=key,
                byterange=byterange, init_uri=init_uri, init_byterange=init_br,
                discontinuity=disc,
            ))
            seq += 1
            duration, byterange, disc = 0.0, None, False
    return MediaPlaylist(url, segments)


# --------------------------------------------------------------------------- #
# Coverage / selection
# --------------------------------------------------------------------------- #

@dataclass
class Coverage:
    playlist: MediaPlaylist
    present: list[int] = field(default_factory=list)   # segment indexes we can build
    missing: list[str] = field(default_factory=list)   # human-readable reasons
    unsupported: Optional[str] = None

    @property
    def total(self) -> int:
        return len(self.playlist.segments)

    @property
    def duration(self) -> float:
        return sum(self.playlist.segments[i].duration for i in self.present)


def coverage(har: Har, pl: MediaPlaylist) -> Coverage:
    cov = Coverage(pl)
    for idx, seg in enumerate(pl.segments):
        if seg.key and seg.key.method != "AES-128":
            cov.unsupported = f"encryption method {seg.key.method} is not supported (DRM)"
            cov.missing.append(f"#{seg.seq} {seg.uri} (unsupported {seg.key.method})")
            continue
        problems = []
        if har.get(seg.uri, seg.byterange) is None:
            problems.append("segment")
        if seg.init_uri and har.get(seg.init_uri, seg.init_byterange) is None:
            problems.append("init segment")
        if seg.key and (not seg.key.uri or har.get(seg.key.uri) is None):
            problems.append("key")
        if problems:
            cov.missing.append(f"#{seg.seq} {seg.uri} (missing {', '.join(problems)})")
        else:
            cov.present.append(idx)
    return cov


@dataclass
class VariantInfo:
    label: str
    bandwidth: int
    url: str
    cov: Optional[Coverage]            # None if the media playlist isn't in the HAR
    audio: Optional[Coverage] = None   # separate audio rendition, if any


@dataclass
class Video:
    source: str                        # master or media playlist URL
    variants: list[VariantInfo]


def discover(har: Har) -> list[Video]:
    playlists = har.playlists()
    masters = [(u, t) for u, t in playlists if is_master(t)]
    medias = {_strip_query(_strip_fragment(u)): (u, t) for u, t in playlists if not is_master(t)}
    claimed: set[str] = set()
    videos: list[Video] = []

    def load_media(url: str) -> Optional[Coverage]:
        key = _strip_query(har.resolve(url))
        hit = medias.get(key)
        if hit is None:
            return None
        claimed.add(key)
        return coverage(har, parse_media(*hit))

    for murl, mtext in masters:
        master = parse_master(murl, mtext)
        audio_by_group: dict[str, list[Rendition]] = {}
        for r in master.renditions:
            if r.type == "AUDIO" and r.uri:
                audio_by_group.setdefault(r.group, []).append(r)
        infos = []
        for v in master.variants:
            label = v.resolution or Path(urlsplit(v.uri).path).name
            info = VariantInfo(label, v.bandwidth, v.uri, load_media(v.uri))
            if v.audio_group and v.audio_group in audio_by_group:
                best = None
                for r in audio_by_group[v.audio_group]:
                    ac = load_media(r.uri)
                    if ac and (best is None or (len(ac.present), r.default) >
                               (len(best[0].present), best[1])):
                        best = (ac, r.default)
                if best:
                    info.audio = best[0]
                elif info.cov is not None:
                    info.cov.missing.append("separate audio playlist not captured")
            infos.append(info)
        if any(i.cov for i in infos):
            videos.append(Video(murl, infos))
        else:
            print(f"warning: master playlist {murl} found but none of its variant "
                  f"playlists were captured; skipping", file=sys.stderr)

    for key, (url, text) in medias.items():
        if key in claimed:
            continue
        cov = coverage(har, parse_media(url, text))
        if not cov.total:
            continue
        videos.append(Video(url, [VariantInfo(Path(urlsplit(url).path).name, 0, url, cov)]))
    return videos


def choose_variant(video: Video, spec: Optional[str]) -> Optional[VariantInfo]:
    avail = [v for v in video.variants if v.cov is not None]
    if spec:
        if spec.isdigit() and 1 <= int(spec) <= len(video.variants) and len(spec) <= 2:
            picked = video.variants[int(spec) - 1]
            return picked if picked.cov else None
        matches = [v for v in avail if spec.lower() in v.label.lower() or spec.lower() in v.url.lower()]
        if not matches:
            return None
        avail = matches
    return max(avail, key=lambda v: (len(v.cov.present), v.bandwidth)) if avail else None


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #

class BuildError(Exception):
    pass


def decrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    if not shutil.which("openssl"):
        raise BuildError("openssl is required to decrypt AES-128 segments but was not found")
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise BuildError("openssl decryption failed: " + proc.stderr.decode(errors="replace").strip())
    return proc.stdout


def write_stream(har: Har, cov: Coverage, tmpd: Path, prefix: str) -> list[str]:
    """Decrypt the available segments of `cov` into files under `tmpd`.

    Segments are binary-concatenated within a "piece"; a new piece starts at every
    EXT-X-DISCONTINUITY (stream PIDs/timestamps may change there) and at every gap
    left by missing segments. Returns ffmpeg input arguments for the stream: a plain
    `-i file` for a single piece, or a concat-demuxer list joining the pieces.
    """
    keys: dict[str, bytes] = {}
    pieces: list[Path] = []
    out = None
    last_init: Optional[tuple[str, Optional[tuple[int, int]]]] = None
    prev_idx = None
    try:
        for idx in cov.present:
            seg = cov.playlist.segments[idx]
            new_piece = (out is None or seg.discontinuity
                         or prev_idx is None or idx != prev_idx + 1)
            prev_idx = idx
            data = har.get(seg.uri, seg.byterange)
            if seg.key:
                k = keys.get(seg.key.uri)
                if k is None:
                    k = har.get(seg.key.uri)
                    if len(k) != 16:
                        raise BuildError(f"key {seg.key.uri} is {len(k)} bytes, expected 16")
                    keys[seg.key.uri] = k
                iv = seg.key.iv or seg.seq.to_bytes(16, "big")
                data = decrypt_aes128(data, k, iv)
            is_mp4 = bool(seg.init_uri) or data[4:8] in (b"ftyp", b"styp", b"moof")
            if not is_mp4 and data[:1] != b"\x47" and not pieces:
                print(f"warning: segment {seg.uri} does not look like MPEG-TS "
                      f"(wrong key or unknown format?)", file=sys.stderr)
            if new_piece:
                if out:
                    out.close()
                path = tmpd / f"{prefix}-{len(pieces):04d}.{'mp4' if is_mp4 else 'ts'}"
                pieces.append(path)
                out = open(path, "wb")
                last_init = None
            if seg.init_uri and (seg.init_uri, seg.init_byterange) != last_init:
                out.write(har.get(seg.init_uri, seg.init_byterange))
                last_init = (seg.init_uri, seg.init_byterange)
            out.write(data)
    finally:
        if out:
            out.close()

    if len(pieces) == 1:
        return ["-i", str(pieces[0])]
    listing = tmpd / f"{prefix}-concat.txt"
    listing.write_text("ffconcat version 1.0\n" + "".join(
        "file '" + str(p).replace("'", "'\\''") + "'\n" for p in pieces))
    return ["-f", "concat", "-safe", "0", "-i", str(listing)]


def run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise BuildError(proc.stderr.decode(errors="replace").strip() or "ffmpeg failed")


def probe_duration(path: Path) -> Optional[float]:
    """Return the container duration of a media file in seconds using ffprobe."""
    if not shutil.which("ffprobe"):
        return None
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        dur = data.get("format", {}).get("duration")
        return float(dur) if dur is not None else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def build(har: Har, info: VariantInfo, out: Path, reencode: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="har2mp4-") as tmp:
        tmpd = Path(tmp)
        in_args = write_stream(har, info.cov, tmpd, "video")
        two_inputs = bool(info.audio and info.audio.present)
        if two_inputs:
            in_args += write_stream(har, info.audio, tmpd, "audio")
        maps = ["-map", "0:v?", "-map", "0:a?"] if not two_inputs else \
            ["-map", "0:v?", "-map", "1:a?"]
        tmp_out = tmpd / "out.mp4"
        copy = ["-c", "copy", "-bsf:a", "aac_adtstoasc"]
        enc = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-c:a", "aac", "-b:a", "192k"]
        tail = ["-movflags", "+faststart", str(tmp_out)]

        if not reencode:
            try:
                run_ffmpeg([*in_args, *maps, *copy, *tail])
            except BuildError as e:
                print(f"warning: lossless remux failed ({e.args[0].splitlines()[-1]}); "
                      f"re-encoding instead", file=sys.stderr)
                reencode = True
        if reencode:
            run_ffmpeg([*in_args, *maps, *enc, *tail])
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_out), out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def fmt_dur(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 \
        else f"{sec // 60}:{sec % 60:02d}"


def print_listing(videos: list[Video], spec: Optional[str]) -> None:
    for vi, video in enumerate(videos, 1):
        print(f"Video {vi}: {video.source}")
        chosen = choose_variant(video, spec)
        for n, v in enumerate(video.variants, 1):
            mark = "*" if v is chosen else " "
            if v.cov is None:
                status = "playlist not captured"
            else:
                status = (f"{len(v.cov.present)}/{v.cov.total} segments "
                          f"({fmt_dur(v.cov.duration)})")
                if v.audio:
                    status += f", audio {len(v.audio.present)}/{v.audio.total}"
                if v.cov.unsupported:
                    status += f" [{v.cov.unsupported}]"
            bw = f"{v.bandwidth // 1000} kbps" if v.bandwidth else ""
            print(f"  {mark} {n:>2}. {v.label:<12} {bw:>10}  {status}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert HLS video captured in a HAR file into MP4 (offline).")
    ap.add_argument("har", type=Path, help="input .har file")
    ap.add_argument("-o", "--output", type=Path,
                    help="output .mp4 path (default: next to the HAR, same basename). "
                         "With several videos, -1, -2, ... are appended.")
    ap.add_argument("-f", "--force", action="store_true", help="overwrite existing output")
    ap.add_argument("--list", action="store_true", help="list detected videos/variants and exit")
    ap.add_argument("--variant", metavar="SPEC",
                    help="variant to use: index from --list (e.g. 2) or a substring of the "
                         "resolution/URL (e.g. 1080, 1280x720)")
    ap.add_argument("--allow-missing", action="store_true",
                    help="build even if some segments/keys are missing (leaves gaps)")
    ap.add_argument("--reencode", action="store_true",
                    help="re-encode to H.264/AAC instead of lossless remux")
    ap.add_argument("--strict-duration", action="store_true",
                    help="fail if output duration differs from playlist duration")
    args = ap.parse_args(argv)

    if not args.har.is_file():
        ap.error(f"{args.har} not found")
    try:
        har = Har(args.har)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"error: {args.har} is not a valid HAR/JSON file: {e}", file=sys.stderr)
        return 2

    videos = discover(har)
    if not videos:
        print("error: no HLS playlists with captured content found in the HAR "
              "(was 'Save all as HAR with content' used?)", file=sys.stderr)
        return 1

    if args.list:
        print_listing(videos, args.variant)
        return 0

    if not shutil.which("ffmpeg"):
        print("error: ffmpeg not found in PATH", file=sys.stderr)
        return 2

    base = args.output or args.har.with_suffix(".mp4")
    if base.suffix.lower() != ".mp4":
        base = base.with_name(base.name + ".mp4")
    outputs = [base] if len(videos) == 1 else \
        [base.with_name(f"{base.stem}-{i}{base.suffix}") for i in range(1, len(videos) + 1)]

    failures = 0
    for vi, (video, out) in enumerate(zip(videos, outputs), 1):
        tag = f"[video {vi}/{len(videos)}]"
        info = choose_variant(video, args.variant)
        if info is None:
            print(f"{tag} error: no captured variant matches "
                  f"{args.variant!r} for {video.source}", file=sys.stderr)
            failures += 1
            continue
        cov = info.cov
        missing = list(cov.missing) + (info.audio.missing if info.audio else [])
        print(f"{tag} {info.label}: {len(cov.present)}/{cov.total} segments, "
              f"{fmt_dur(cov.duration)} -> {out}")
        if cov.unsupported:
            print(f"{tag} error: {cov.unsupported}", file=sys.stderr)
            failures += 1
            continue
        if not cov.present:
            print(f"{tag} error: none of the segments were captured", file=sys.stderr)
            failures += 1
            continue
        if missing:
            print(f"{tag} {'warning' if args.allow_missing else 'error'}: "
                  f"{len(missing)} item(s) missing from the HAR:", file=sys.stderr)
            for m in missing[:20]:
                print(f"    {m}", file=sys.stderr)
            if len(missing) > 20:
                print(f"    ... and {len(missing) - 20} more", file=sys.stderr)
            if not args.allow_missing:
                print(f"{tag} hint: watch the whole video before exporting the HAR, "
                      f"or pass --allow-missing", file=sys.stderr)
                failures += 1
                continue
        if out.exists() and not args.force:
            print(f"{tag} error: {out} exists (use -f to overwrite)", file=sys.stderr)
            failures += 1
            continue
        try:
            build(har, info, out, args.reencode)
        except BuildError as e:
            print(f"{tag} error: {e}", file=sys.stderr)
            failures += 1
            continue

        actual_dur = probe_duration(out)
        if actual_dur is not None:
            expected_dur = cov.duration
            orig_dur = sum(s.duration for s in cov.playlist.segments)
            tol = max(2.0, expected_dur * 0.02)
            diff = actual_dur - expected_dur
            if expected_dur > 0 and abs(diff) > tol:
                level = "error" if args.strict_duration else "warning"
                target_desc = "playlist" if len(cov.present) == cov.total else "captured"
                print(f"{tag} {level}: output duration ({fmt_dur(actual_dur)}) differs from "
                      f"{target_desc} duration ({fmt_dur(expected_dur)}) by {diff:+.1f}s",
                      file=sys.stderr)
                if args.strict_duration:
                    out.unlink(missing_ok=True)
                    failures += 1
                    continue
            dur_label = fmt_dur(actual_dur)
            if len(cov.present) < cov.total and orig_dur > 0:
                dur_label += f", original {fmt_dur(orig_dur)}"
            print(f"{tag} wrote {out} ({dur_label})")
        else:
            if not shutil.which("ffprobe"):
                print(f"{tag} warning: ffprobe not found in PATH; duration could not be verified",
                      file=sys.stderr)
            else:
                print(f"{tag} warning: could not determine output duration with ffprobe",
                      file=sys.stderr)
            if args.strict_duration:
                print(f"{tag} error: --strict-duration could not verify duration", file=sys.stderr)
                out.unlink(missing_ok=True)
                failures += 1
                continue
            print(f"{tag} wrote {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
