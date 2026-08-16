#!/usr/bin/env python3
"""Transcribe a media file with STEREO Dictator and write segments as JSON.

Works on video too: the audio track is extracted locally with ffmpeg first, so a
2 GB recording travels as a ~12 MB mono AAC file instead. That single step is why
a 25-minute podcast finishes in under two minutes end to end.

Get a token: send /app to @stereo_dictator_bot, then either pass --token or put
DICTATOR_TOKEN in a .env file next to your project.

Usage:
    python transcribe.py --src podcast.mp4 --out transcript.json --diarize
    python transcribe.py --src clip_01.mp4 --out clip_01.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import urllib.request
import urllib.error

DEFAULT_API = "https://bot.stereosam.ru/dictate"
AUDIO_EXT = {".wav", ".mp3", ".ogg", ".m4a", ".flac", ".aac"}


def load_env(path: Path) -> dict:
    """Minimal .env reader — no dependency for one file with two keys."""
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def extract_audio(src: Path) -> Path:
    """Video -> mono 64 kbps AAC. Mono because diarization does not need stereo
    and it halves the upload."""
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found in PATH")
    tmp = Path(tempfile.mkdtemp(prefix="reels_")) / "audio.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src),
         "-vn", "-acodec", "aac", "-b:a", "64k", "-ac", "1", str(tmp)],
        check=True, stdout=subprocess.DEVNULL,
    )
    return tmp


def post_file(api: str, token: str, audio: Path, diarize: bool,
              max_speakers: int, timeout: int, words: bool = False) -> dict:
    boundary = "----stereoreels"
    fields = {"diarize": "true" if diarize else "false",
              "max_speakers": str(max_speakers),
              "words": "true" if words else "false"}

    body = bytearray()
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="audio"; '
             f'filename="{audio.name}"\r\n').encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += audio.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{api.rstrip('/')}/transcribe_file", data=bytes(body), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        sys.exit(f"API returned {e.code}: {detail}")


def transcribe(src: Path, token: str, api: str = DEFAULT_API,
               diarize: bool = False, max_speakers: int = 4,
               timeout: int = 1800, words: bool = False) -> dict:
    """`words=True` asks for per-word boundaries as well as per-segment ones.

    Only karaoke subtitles need them, and they cost extra time on the server, so
    the default is off. Without them a burner has to guess how long each word
    lasts from its letter count — which drifts by half a second and looks it.
    """
    audio = src if src.suffix.lower() in AUDIO_EXT else extract_audio(src)
    data = post_file(api, token, audio, diarize, max_speakers, timeout, words)
    data["source"] = str(src)
    return data


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--token", default=None)
    p.add_argument("--env", type=Path, default=Path(".env"))
    p.add_argument("--api", default=None)
    p.add_argument("--diarize", action="store_true")
    p.add_argument("--words", action="store_true",
                   help="also return per-word start/end — needed for karaoke subtitles")
    p.add_argument("--max-speakers", type=int, default=4)
    p.add_argument("--timeout", type=int, default=1800)
    args = p.parse_args()

    env = load_env(args.env)
    token = args.token or os.environ.get("DICTATOR_TOKEN") or env.get("DICTATOR_TOKEN")
    if not token:
        sys.exit("No token. Send /app to @stereo_dictator_bot, then pass --token "
                 "or set DICTATOR_TOKEN in .env")
    api = args.api or os.environ.get("DICTATOR_API") or env.get("DICTATOR_API") or DEFAULT_API

    data = transcribe(args.src, token, api, args.diarize, args.max_speakers,
                      args.timeout, args.words)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    segs = data.get("segments", [])
    word_count = sum(len(s.get("words") or []) for s in segs)
    extra = f" · {word_count} word timings" if word_count else ""
    print(f"{len(segs)} segments · {data.get('duration', 0):.0f}s audio · "
          f"{data.get('elapsed', 0):.0f}s to transcribe{extra} · -> {args.out}")


if __name__ == "__main__":
    main()
