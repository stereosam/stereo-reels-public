#!/usr/bin/env python3
"""Turn clip segments into an .srt subtitle file, optionally split for vertical video.

Two things this does that a naive dump does not:

1. Timecodes are shifted to the clip, not the source. A clip cut at 1186.6s must
   start its subtitles at 0, or every editor that imports the file will place the
   captions twenty minutes into a forty-second video.

2. Long segments are split into readable lines. Recognisers happily return one
   twelve-second sentence; on a phone that is a wall of text. Segments longer than
   --max-chars are broken at word boundaries and their duration is shared out in
   proportion to the text.

Usage:
    python srt.py --transcript clip_01.json --out clip_01.srt
    python srt.py --transcript transcript.json --offset 1186.6 --out clip_01.srt
"""
import argparse
import json
from pathlib import Path


def stamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_long(text: str, start: float, end: float, max_chars: int) -> list:
    """Break one long caption into several, splitting on words and sharing the
    duration in proportion to how much text each part carries."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return [(start, end, text)]

    words, lines, current = text.split(), [], ""
    for w in words:
        if current and len(current) + 1 + len(w) > max_chars:
            lines.append(current)
            current = w
        else:
            current = f"{current} {w}".strip()
    if current:
        lines.append(current)

    total = sum(len(l) for l in lines) or 1
    span = max(0.001, end - start)
    out, cursor = [], start
    for i, line in enumerate(lines):
        share = span * (len(line) / total)
        finish = end if i == len(lines) - 1 else cursor + share
        out.append((cursor, finish, line))
        cursor = finish
    return out


def build(segments: list, offset: float, max_chars: int,
          min_duration: float) -> str:
    blocks, index = [], 1
    for seg in segments:
        start = float(seg["start"]) - offset
        end = float(seg["end"]) - offset
        if end <= 0:
            continue
        for a, b, text in split_long(seg.get("text", ""), start, end, max_chars):
            if not text:
                continue
            if b - a < min_duration:
                b = a + min_duration
            blocks.append(f"{index}\n{stamp(a)} --> {stamp(b)}\n{text}\n")
            index += 1
    return "\n".join(blocks)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--transcript", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--offset", type=float, default=0.0,
                   help="seconds to subtract — the clip's start in the source")
    p.add_argument("--max-chars", type=int, default=42,
                   help="characters per caption line (42 reads well on a phone)")
    p.add_argument("--min-duration", type=float, default=0.7)
    args = p.parse_args()

    data = json.loads(args.transcript.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    body = build(segments, args.offset, args.max_chars, args.min_duration)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(body, encoding="utf-8")
    print(f"{body.count(' --> ')} captions -> {args.out}")


if __name__ == "__main__":
    main()
