# har2mp4

Rebuild HLS videos captured in a browser HAR file into MP4 — **fully offline**.
Playlists, segments and AES-128 keys are read from the response bodies stored in
the HAR; nothing is downloaded.

## Requirements

- Python 3.9+ (stdlib only)
- `ffmpeg`
- `openssl` (only needed for AES-128 encrypted streams)

## Capturing a HAR

1. Open DevTools → Network, then play the video **all the way through**
   (only segments that were actually loaded end up in the HAR).
2. Right-click the request list → *Save all as HAR* (Firefox) /
   *Save all as HAR with content* (Chrome).

> HAR files contain your cookies, auth tokens and signed URLs. Don't share or
> commit them (`*.har` is git-ignored).

## Usage

```sh
./har2mp4.py capture.har                  # -> capture.mp4 next to the HAR
./har2mp4.py capture.har --list           # show videos/variants and coverage
./har2mp4.py capture.har --variant 720    # pick a variant by index or resolution/URL substring
./har2mp4.py capture.har -o out/video.mp4 -f
```

| Option | Meaning |
| --- | --- |
| `-o, --output` | Output path. With several videos, `-1`, `-2`, … are appended. |
| `-f, --force` | Overwrite an existing output file. |
| `--list` | List detected videos and variants (`*` marks the one that will be used). |
| `--variant SPEC` | Variant index from `--list`, or a substring of its resolution/URL. |
| `--allow-missing` | Build even if some segments/keys are missing (gaps are skipped). |
| `--reencode` | Re-encode to H.264/AAC instead of lossless remux. |

## How it works

- Every response body starting with `#EXTM3U` is treated as a playlist. Master
  playlists group their variants into one *video*; media playlists not referenced
  by any master become their own video. Every video found is converted.
- Per video, the variant with the most captured segments wins (ties → highest
  bandwidth).
- Missing segments/keys abort the conversion with a list of what's missing,
  unless `--allow-missing` is given.
- AES-128 segments are decrypted with `openssl` (explicit IV or media sequence
  number). `SAMPLE-AES`/DRM streams are not supported.
- Segments are concatenated per discontinuity (stream IDs and timestamps may
  change at `#EXT-X-DISCONTINUITY`), joined with ffmpeg's concat demuxer, and
  remuxed losslessly to MP4 (`-c copy`, `+faststart`). If remuxing fails it falls
  back to re-encoding.
- Also handles fMP4 segments (`#EXT-X-MAP`), byte ranges, separate audio
  renditions and HTTP redirects recorded in the HAR.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests generate synthetic (encrypted and clear) HLS captures with ffmpeg and
openssl, so no real HAR is needed.
