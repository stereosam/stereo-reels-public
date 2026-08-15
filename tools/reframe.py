#!/usr/bin/env python3
"""Turn a landscape video into a vertical one, following the action.

A static centre crop throws away whoever is standing off-centre. Face detection
would fix that but costs a heavy dependency, and this pipeline promises none. So
the camera path is built from motion instead: in a talking head, a screencast or
an interview, the part of the frame that changes between frames is the part worth
keeping.

How it works, and every step is plain ffmpeg plus arithmetic:

  1. Ask ffmpeg for small greyscale frames — 96 px wide, a few per second. A
     20-minute video becomes a few megabytes of raw bytes.
  2. For each pair of neighbouring frames, sum the per-column difference. That
     column profile is "where things moved".
  3. Slide a window of the target width across the profile and take the position
     with the most movement. That is the centre for that moment.
  4. Smooth the centres over time and cap how fast the crop may travel, so the
     result pans instead of twitching.
  5. Render with `crop`, driven by a `sendcmd` script — one line per keyframe.

Motion, not faces: a still person next to a moving hand loses. Documented, not
hidden. `--anchor` overrides everything and crops statically.

Usage:
    python reframe.py --src talk.mp4 --out vertical.mp4
    python reframe.py --src talk.mp4 --out vertical.mp4 --anchor center
    python reframe.py --src talk.mp4 --out vertical.mp4 --plan-only
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SAMPLE_W = 96          # ширина уменьшенного кадра для анализа
SAMPLE_FPS = 3         # кадров в секунду на анализ
KEYFRAME_SEC = 1.0     # как часто разрешаем менять положение кадра
MAX_TRAVEL = 0.14      # доля ширины исходника в секунду — потолок скорости панорамы


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    d = json.loads(out.stdout)
    s = d["streams"][0]
    return {"w": int(s["width"]), "h": int(s["height"]),
            "duration": float(d["format"]["duration"])}


def column_activity(src: Path, sample_h: int) -> list:
    """Список профилей движения по колонкам, по одному на выборочный кадр."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(src),
           "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_W}:{sample_h},format=gray",
           "-f", "rawvideo", "-"]
    frame_size = SAMPLE_W * sample_h
    profiles, prev = [], None

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            if prev is not None:
                cols = [0] * SAMPLE_W
                for row in range(sample_h):
                    base = row * SAMPLE_W
                    for col in range(SAMPLE_W):
                        i = base + col
                        d = buf[i] - prev[i]
                        cols[col] += d if d >= 0 else -d
                profiles.append(cols)
            prev = buf
    finally:
        proc.stdout.close()
        proc.wait()
    return profiles


def best_centre(profile: list, win: int) -> float:
    """Позиция окна с наибольшим движением, в долях ширины (0..1).

    Два шага. Сначала окно с максимальной суммой — грубо находим, в какой части
    кадра вообще что-то происходит. Потом центр тяжести активности ВНУТРИ этого
    окна — иначе узкий говорящий человек оказывается у самой кромки: окно-то
    поймало его, но серединой смотрит в пустую сцену рядом.
    """
    if win >= len(profile):
        return 0.5

    running = sum(profile[:win])
    best_sum, best_start = running, 0
    for i in range(1, len(profile) - win + 1):
        running += profile[i + win - 1] - profile[i - 1]
        if running > best_sum:
            best_sum, best_start = running, i

    chunk = profile[best_start:best_start + win]
    total = sum(chunk)
    if total <= 0:
        return (best_start + win / 2) / len(profile)
    centroid = sum((best_start + i) * v for i, v in enumerate(chunk)) / total
    return (centroid + 0.5) / len(profile)


def build_path(profiles: list, win_frac: float, duration: float) -> list:
    """Профили -> сглаженный список (время, центр в долях)."""
    if not profiles:
        return [(0.0, 0.5)]

    win = max(1, int(round(SAMPLE_W * win_frac)))
    per_frame = [best_centre(p, win) for p in profiles]

    # усреднение скользящим окном ~2 секунды: гасим дрожание на отдельных кадрах
    span = max(1, int(SAMPLE_FPS * 2))
    smoothed = []
    for i in range(len(per_frame)):
        lo, hi = max(0, i - span), min(len(per_frame), i + span + 1)
        smoothed.append(sum(per_frame[lo:hi]) / (hi - lo))

    # прореживаем до ключевых кадров и ограничиваем скорость движения
    step = max(1, int(round(SAMPLE_FPS * KEYFRAME_SEC)))
    path, current = [], smoothed[0]
    for i in range(0, len(smoothed), step):
        t = i / SAMPLE_FPS
        target = smoothed[i]
        limit = MAX_TRAVEL * KEYFRAME_SEC
        if target - current > limit:
            current += limit
        elif current - target > limit:
            current -= limit
        else:
            current = target
        path.append((round(t, 3), round(current, 4)))
    if path and path[-1][0] < duration - 1:
        path.append((round(duration, 3), path[-1][1]))
    return path


def render(src: Path, out: Path, size: dict, path: list, crop_w: int,
           crop_h: int, quality: str) -> None:
    half = crop_w / 2
    lo, hi = 0.0, float(size["w"] - crop_w)

    lines = []
    for t, centre in path:
        x = min(hi, max(lo, centre * size["w"] - half))
        lines.append(f"{t:.3f} crop x {int(round(x))};")

    with tempfile.NamedTemporaryFile("w", suffix=".sendcmd", delete=False,
                                     encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        script = f.name

    crf, preset = ("28", "veryfast") if quality == "draft" else ("18", "slow")
    # sendcmd меняет x у crop по расписанию; путь к скрипту экранируем для фильтра
    esc = script.replace("\\", "/").replace(":", "\\:")
    vf = (f"sendcmd=f='{esc}',crop={crop_w}:{crop_h}:0:(ih-{crop_h})/2,"
          f"scale=1080:1920:flags=lanczos")
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", vf,
             "-c:v", "libx264", "-preset", preset, "-crf", crf,
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
            check=True, stdout=subprocess.DEVNULL,
        )
    finally:
        Path(script).unlink(missing_ok=True)


def reframe(src: Path, out: Path, anchor: str = None, ratio: str = "9:16",
            quality: str = "draft", plan_only: bool = False) -> dict:
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found in PATH")

    size = probe(src)
    rw, rh = (int(x) for x in ratio.split(":"))
    crop_h = size["h"]
    crop_w = int(round(crop_h * rw / rh))
    if crop_w > size["w"]:                       # источник уже вертикальнее цели
        crop_w = size["w"]
        crop_h = int(round(crop_w * rh / rw))
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2

    if anchor:
        frac = {"left": 0.25, "center": 0.5, "right": 0.75}.get(anchor)
        if frac is None:
            frac = float(anchor)
        path = [(0.0, frac)]
        method = f"static anchor {anchor}"
    else:
        sample_h = max(2, int(round(SAMPLE_W * size["h"] / size["w"])))
        sample_h -= sample_h % 2
        profiles = column_activity(src, sample_h)
        path = build_path(profiles, crop_w / size["w"], size["duration"])
        method = f"motion, {len(profiles)} sampled frames"

    info = {"src": str(src), "out": str(out), "source": size,
            "crop": {"w": crop_w, "h": crop_h}, "method": method,
            "keyframes": len(path),
            "path_preview": [{"t": t, "centre": c} for t, c in path[:8]]}

    if not plan_only:
        render(src, out, size, path, crop_w, crop_h, quality)
        info["bytes"] = out.stat().st_size
    return info


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", required=True, type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--ratio", default="9:16")
    p.add_argument("--anchor", default=None,
                   help="left | center | right | 0..1 — static crop, no analysis")
    p.add_argument("--quality", choices=["draft", "final"], default="draft")
    p.add_argument("--plan-only", action="store_true",
                   help="only compute the camera path, render nothing")
    args = p.parse_args()

    if not args.plan_only and not args.out:
        sys.exit("--out is required unless --plan-only")

    info = reframe(args.src, args.out or Path("unused.mp4"), args.anchor,
                   args.ratio, args.quality, args.plan_only)
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
