#!/usr/bin/env python3
"""Cut a clip out of a source video, frame-accurately.

Why re-encode instead of `-c copy`: stream copy can only cut on keyframes, so the
result drifts by up to the keyframe interval — often 2-5 seconds. For a 30-second
reel that is the difference between landing on the punchline and missing it.
Re-encoding is slower and exact, and at reel length the cost is a few seconds.

Every cut is made from the ORIGINAL file, never from a previous cut. When the
reviewer says "give me two more seconds at the front", we re-cut with new bounds
instead of stacking generation loss.

Usage:
    python cut.py --src video.mp4 --start 128.4 --end 161.0 --out clip_01.mp4
    python cut.py --src video.mp4 --start 128.4 --end 161.0 --out clip_01.mp4 --pad 0.4
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


# Draft first, final once. Review takes several rounds — "two more seconds at the
# front", "drop that one" — and re-rendering 4K on every round is wasted time and
# disk. A draft is downscaled and cheap; only the clips that survive review get
# rendered at full quality, from the original, with the exact bounds that were
# verified. Audio stays good in both, because verification listens to it.
QUALITY = {
    # name:  (max height, crf, preset, audio bitrate)
    "draft": (720, 28, "veryfast", "96k"),
    "final": (None, 18, "slow", "192k"),
}


def cut(src: Path, start: float, end: float, out: Path, pad: float = 0.0,
        quality: str = "draft") -> dict:
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found in PATH")
    if end <= start:
        sys.exit(f"end ({end}) must be greater than start ({start})")
    if quality not in QUALITY:
        sys.exit(f"quality must be one of {list(QUALITY)}")

    height, crf, preset, abr = QUALITY[quality]
    total = ffprobe_duration(src)
    a = max(0.0, start - pad)
    b = min(total, end + pad)

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        # -ss AFTER -i is the slow, exact seek. Before -i it is fast but snaps
        # to the nearest keyframe, which is what we are trying to avoid.
        "-i", str(src),
        "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
    ]
    if height:
        # -2 keeps the aspect ratio and an even width, which h264 requires.
        cmd += ["-vf", f"scale=-2:'min({height},ih)'"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "aac", "-b:a", abr,
        "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)

    return {
        "src": str(src),
        "out": str(out),
        "quality": quality,
        "requested": {"start": start, "end": end, "pad": pad},
        "actual": {"start": round(a, 3), "end": round(b, 3),
                   "duration": round(b - a, 3)},
        "bytes": out.stat().st_size,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", required=True, type=Path)
    p.add_argument("--start", required=True, type=float, help="seconds")
    p.add_argument("--end", required=True, type=float, help="seconds")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--pad", type=float, default=0.0,
                   help="seconds of breathing room added on both sides")
    p.add_argument("--quality", choices=list(QUALITY), default="draft",
                   help="draft: 720p, fast, for review. final: full resolution, "
                        "slow, for the clips that were approved")
    args = p.parse_args()

    print(json.dumps(cut(args.src, args.start, args.end, args.out,
                         args.pad, args.quality), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
