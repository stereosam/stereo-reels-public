#!/usr/bin/env python3
"""Transcribe a finished clip and check it against what the cut was supposed to contain.

This is the step that makes the pipeline trustworthy. Source timestamps lie more
often than you would expect: voice-activity detection merges a pause into the
following phrase, so a segment can claim 38 seconds for three words. Cut blindly
on that and you ship 35 seconds of silence with a punchline stapled to the end.

So every clip is transcribed again, on its own, and compared with the text it was
cut for. Four things are checked:

  silence_head   speech starts too late into the clip
  silence_tail   speech stops too early before the end
  text_drift     what was heard does not match what was expected
  empty          no speech recognised at all

The clip is not "verified" because a human watched it. It is verified because the
same recogniser, given only the clip, heard the same words.

Usage:
    python verify.py --clip clip_01.mp4 --expect-text "..." --out clip_01.verify.json
    python verify.py --clip clip_01.mp4 --expect-from transcript.json --start 128.4 --end 161.0
"""
import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

from transcribe import DEFAULT_API, load_env, transcribe

WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalise(text: str) -> list:
    return WORD.sub(" ", (text or "").lower()).split()


def similarity(a: str, b: str) -> float:
    wa, wb = normalise(a), normalise(b)
    if not wa or not wb:
        return 0.0
    return SequenceMatcher(None, wa, wb).ratio()


def expected_text(transcript: Path, start: float, end: float) -> str:
    """Every source segment that overlaps the requested window."""
    data = json.loads(transcript.read_text(encoding="utf-8"))
    parts = [s["text"] for s in data.get("segments", [])
             if s["end"] > start and s["start"] < end]
    return " ".join(parts).strip()


def check(clip_data: dict, expect: str, head_limit: float, tail_limit: float,
          min_similarity: float) -> dict:
    segs = clip_data.get("segments", [])
    duration = float(clip_data.get("duration") or 0.0)
    heard = " ".join(s["text"] for s in segs).strip()
    issues = []

    if not segs or not heard:
        issues.append({"code": "empty",
                       "detail": "no speech recognised in the clip"})
        return {"ok": False, "issues": issues, "heard": heard,
                "expected": expect, "similarity": 0.0, "duration": duration}

    head = float(segs[0]["start"])
    tail = duration - float(segs[-1]["end"])
    ratio = similarity(expect, heard) if expect else None

    if head > head_limit:
        issues.append({"code": "silence_head", "seconds": round(head, 2),
                       "detail": f"speech starts {head:.1f}s in; trim the front "
                                 f"or move --start forward"})
    if tail > tail_limit:
        issues.append({"code": "silence_tail", "seconds": round(tail, 2),
                       "detail": f"{tail:.1f}s of silence at the end; move --end back"})
    if ratio is not None and ratio < min_similarity:
        issues.append({"code": "text_drift", "similarity": round(ratio, 3),
                       "detail": "clip does not contain the expected words"})

    return {"ok": not issues, "issues": issues, "heard": heard,
            "expected": expect, "similarity": None if ratio is None else round(ratio, 3),
            "duration": duration, "speech_starts_at": round(head, 2),
            "silence_at_end": round(tail, 2)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--clip", required=True, type=Path)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--expect-text", default=None)
    p.add_argument("--expect-from", type=Path, default=None,
                   help="source transcript.json; needs --start and --end")
    p.add_argument("--start", type=float, default=None)
    p.add_argument("--end", type=float, default=None)
    p.add_argument("--head-limit", type=float, default=1.0)
    p.add_argument("--tail-limit", type=float, default=1.5)
    p.add_argument("--min-similarity", type=float, default=0.6)
    p.add_argument("--token", default=None)
    p.add_argument("--env", type=Path, default=Path(".env"))
    p.add_argument("--api", default=None)
    args = p.parse_args()

    expect = args.expect_text or ""
    if args.expect_from:
        if args.start is None or args.end is None:
            sys.exit("--expect-from needs --start and --end")
        expect = expected_text(args.expect_from, args.start, args.end)

    env = load_env(args.env)
    token = args.token or env.get("DICTATOR_TOKEN")
    if not token:
        sys.exit("No token. Send /app to @stereo_dictator_bot.")
    api = args.api or env.get("DICTATOR_API") or DEFAULT_API

    clip_data = transcribe(args.clip, token, api)
    report = check(clip_data, expect, args.head_limit, args.tail_limit,
                   args.min_similarity)
    report["clip"] = str(args.clip)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    verdict = "OK" if report["ok"] else "PROBLEMS"
    print(f"{verdict}  {args.clip.name}  {report['duration']:.1f}s  "
          f"similarity={report['similarity']}")
    for issue in report["issues"]:
        print(f"  ! {issue['code']}: {issue['detail']}")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
