"""End-to-end tests: build synthetic HLS captures as HAR files and convert them.

Run with:  python3 -m unittest discover -s tests -v
Requires ffmpeg, ffprobe and openssl in PATH (tests are skipped otherwise).
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "har2mp4.py"
HOST = "https://cdn.example.com/v/abc"
KEY = bytes(range(16))

HAVE_TOOLS = all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "openssl"))


def make_segments(tmp: Path, name: str, seconds: int, start_pid: str = "0x0100") -> list[bytes]:
    """Encode a test clip and split it into 1 s MPEG-TS segments."""
    out = tmp / name
    out.mkdir()
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-g", "25", "-keyint_min", "25", "-sc_threshold", "0",
        "-c:a", "aac", "-shortest",
        "-f", "segment", "-segment_time", "1", "-segment_format", "mpegts",
        "-mpegts_start_pid", start_pid,
        str(out / "seg%03d.ts"),
    ], check=True)
    return [p.read_bytes() for p in sorted(out.glob("seg*.ts"))]


def encrypt(data: bytes, iv: bytes) -> bytes:
    return subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-K", KEY.hex(), "-iv", iv.hex()],
        input=data, stdout=subprocess.PIPE, check=True).stdout


def entry(url: str, body: bytes, mime: str, binary: bool) -> dict:
    content = {"mimeType": mime, "size": len(body)}
    if binary:
        content.update(text=base64.b64encode(body).decode(), encoding="base64")
    else:
        content["text"] = body.decode()
    return {
        "request": {"method": "GET", "url": url, "headers": []},
        "response": {"status": 200, "content": content},
    }


def build_har(path: Path, parts: list[list[bytes]], encrypted: bool,
              drop: frozenset = frozenset(), seg_duration: float = 1.0) -> int:
    """Write a HAR with master + media playlist + segments (+ key).

    `parts` are lists of segments separated by EXT-X-DISCONTINUITY.
    `drop` holds segment indexes to leave out of the HAR. Returns segment count.
    """
    entries = []
    master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=320x240\n"
              "low/index.m3u8?token=x\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x480\n"
              "high/index.m3u8\n")
    entries.append(entry(f"{HOST}/master.m3u8?sig=1", master.encode(),
                         "application/vnd.apple.mpegurl", False))
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(seg_duration)}",
             "#EXT-X-MEDIA-SEQUENCE:1", "#EXT-X-PLAYLIST-TYPE:VOD"]
    if encrypted:
        lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="https://keys.example.com/k1"')
        entries.append(entry("https://keys.example.com/k1", KEY, "binary/octet-stream", True))
    n = 0
    for pi, part in enumerate(parts):
        if pi:
            lines.append("#EXT-X-DISCONTINUITY")
        for data in part:
            seq = n + 1
            # Mix relative and root-relative URIs, like real CDNs do.
            uri = f"s{seq:03d}.ts" if seq % 2 else f"/v/abc/low/s{seq:03d}.ts"
            lines += [f"#EXTINF:{seg_duration},", uri]
            if n not in drop:
                body = encrypt(data, seq.to_bytes(16, "big")) if encrypted else data
                entries.append(entry(f"{HOST}/low/s{seq:03d}.ts", body, "video/mp2t", True))
            n += 1
    lines.append("#EXT-X-ENDLIST")
    entries.append(entry(f"{HOST}/low/index.m3u8?token=x", "\n".join(lines).encode(),
                         "application/x-mpegurl", False))
    path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return n


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(path)], stdout=subprocess.PIPE, check=True).stdout
    return json.loads(out)


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg/ffprobe/openssl not available")
class Har2Mp4Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.part_a = make_segments(cls.tmp, "a", 4)
        cls.part_b = make_segments(cls.tmp, "b", 3, start_pid="0x01e1")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=self.tmp))

    def assertVideo(self, mp4: Path, seconds: float):
        info = probe(mp4)
        kinds = sorted(s["codec_type"] for s in info["streams"])
        self.assertEqual(kinds, ["audio", "video"])
        self.assertAlmostEqual(float(info["format"]["duration"]), seconds, delta=0.5)

    def test_encrypted(self):
        har = self.dir / "clip.har"
        build_har(har, [self.part_a], encrypted=True)
        r = run(har)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertVideo(self.dir / "clip.mp4", 4)

    def test_clear(self):
        har = self.dir / "clip.har"
        build_har(har, [self.part_a], encrypted=False)
        r = run(har, "-o", self.dir / "out" / "x.mp4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertVideo(self.dir / "out" / "x.mp4", 4)

    def test_discontinuity_with_new_pids_and_timestamps(self):
        har = self.dir / "clip.har"
        build_har(har, [self.part_a, self.part_b], encrypted=True)
        r = run(har)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertVideo(self.dir / "clip.mp4", 7)

    def test_missing_segments(self):
        har = self.dir / "clip.har"
        build_har(har, [self.part_a], encrypted=True, drop=frozenset({2}))
        r = run(har)
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing", r.stderr)
        self.assertFalse((self.dir / "clip.mp4").exists())
        r = run(har, "--allow-missing")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertVideo(self.dir / "clip.mp4", 3)

    def test_no_overwrite_without_force(self):
        har = self.dir / "clip.har"
        build_har(har, [self.part_a], encrypted=True)
        (self.dir / "clip.mp4").write_bytes(b"keep")
        r = run(har)
        self.assertEqual(r.returncode, 1)
        self.assertEqual((self.dir / "clip.mp4").read_bytes(), b"keep")
        r = run(har, "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertVideo(self.dir / "clip.mp4", 4)

    def test_list(self):
        har = self.dir / "clip.har"
        build_har(har, [self.part_a], encrypted=True)
        r = run(har, "--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("4/4 segments", r.stdout)
        self.assertIn("playlist not captured", r.stdout)

    def test_no_video(self):
        har = self.dir / "empty.har"
        har.write_text(json.dumps({"log": {"entries": []}}))
        r = run(har)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no HLS playlists", r.stderr)

    def test_duration_mismatch_warning(self):
        har = self.dir / "clip.har"
        build_har(har, [self.part_a], encrypted=False, seg_duration=10.0)
        r = run(har)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("warning: output duration", r.stderr)
        self.assertTrue((self.dir / "clip.mp4").exists())

    def test_duration_mismatch_strict(self):
        har = self.dir / "clip.har"
        build_har(har, [self.part_a], encrypted=False, seg_duration=10.0)
        r = run(har, "--strict-duration")
        self.assertEqual(r.returncode, 1)
        self.assertIn("error: output duration", r.stderr)
        self.assertFalse((self.dir / "clip.mp4").exists())


if __name__ == "__main__":
    unittest.main()
