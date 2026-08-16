#!/usr/bin/env python3
"""Find the pauses in a clip and say whether anything is happening during them.

Silence is not automatically dead air. A speaker who stops talking while drawing
on a whiteboard is worth watching; a speaker who stops talking in front of a
motionless slide is not. The two look identical in the audio, so this reports
both facts and lets a human decide.

Both measurements are already paid for elsewhere:

  * where the speech is — from the per-word timings the recogniser returns
    (`transcribe.py --words`). A gap between two words is a pause. Nothing is
    computed; the data is already in the file.
  * what the picture is doing — from `reframe.column_activity`, the same
    per-column frame differencing that builds the camera path. A few hundred
    small greyscale frames, about a second of work on a short clip.

Cross the two and a pause falls into one of three cases:

  keep    something moves — a slide changes, a hand draws. The silence carries it.
  cut     nothing moves and nobody speaks. Dead.
  short   below the threshold; normal speech rhythm, leave it alone.

This tool does not edit anything. Removing a stretch from the middle means
re-mapping every later word timing, and a caption that is 1.4 seconds out looks
exactly like a broken pipeline — so the cut belongs in a separate, deliberate
step. Usually the right fix is not a cut at all: move the clip bounds in
`cut.py` so the dead stretch is never included.

One caveat worth knowing before trusting a verdict: **a word the recogniser
missed is indistinguishable from silence here.** On the clip this was built
against, one engine heard a "Да." that the other dropped entirely, and the
dropped version turns a spoken second into a reported pause. That is a reason
to read the report rather than pipe it into an automatic cut.

Usage:
    python pauses.py --src clip.mp4 --timing clip.json
    python pauses.py --src clip.mp4 --timing clip.json --min 0.8 --json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reframe  # noqa: E402  — соседний модуль, не пакет

MIN_PAUSE = 0.6        # короче — обычный речевой ритм, не пауза
STILL_FRACTION = 0.08  # доля от самого подвижного кадра, ниже которой «картинка стоит»


def word_gaps(timing: Path, min_pause: float) -> list:
    """Дырки между словами -> [(начало, конец)]."""
    words = []
    data = json.loads(timing.read_text(encoding="utf-8"))
    for seg in data.get("segments", []):
        for w in (seg.get("words") or []):
            words.append((float(w["start"]), float(w["end"])))
    words.sort()
    gaps = []
    for i in range(len(words) - 1):
        start, end = words[i][1], words[i + 1][0]
        if end - start >= min_pause:
            gaps.append((round(start, 2), round(end, 2)))
    return gaps


def motion_series(src: Path) -> tuple:
    """(движение по кадрам в долях от максимума, кадров в секунду)."""
    size = reframe.probe(src)
    sample_h = max(2, int(round(reframe.SAMPLE_W * size["h"] / size["w"])))
    sample_h -= sample_h % 2
    profiles = reframe.column_activity(src, sample_h)
    totals = [sum(p) for p in profiles]
    peak = max(totals) or 1
    return [t / peak for t in totals], reframe.SAMPLE_FPS


def still_runs(series: list, fps: int, start: float, end: float,
               still: float, min_run: float) -> list:
    """Неподвижные отрезки ВНУТРИ паузы -> [(начало, конец)].

    Считать максимум по всей паузе нельзя: в четырёх секундах молчания слайд
    успевает смениться дважды, один яркий кадр красит всю паузу в «оставить»,
    и полторы мёртвых секунды между сменами теряются. Поэтому идём по кадрам и
    собираем подряд идущие неподвижные.
    """
    # series[i] — разница между кадрами i и i+1, то есть промежуток
    # (i/fps, (i+1)/fps]. Момент времени t описывается элементом с индексом
    # ceil(t*fps)-1; без этой поправки окно съезжает на кадр, и всплеск от
    # смены слайда приписывается соседней, неподвижной секунде.
    def at(t):
        return max(0, min(len(series) - 1, int(t * fps + 0.999) - 1))

    lo, hi = at(start), at(end) + 1
    runs, run_start = [], None
    for i in range(lo, hi):
        if series[i] < still:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            runs.append((run_start, i))
            run_start = None
    if run_start is not None:
        runs.append((run_start, hi))

    out = []
    for a, b in runs:
        t0, t1 = max(start, a / fps), min(end, b / fps)
        if t1 - t0 >= min_run:
            out.append((round(t0, 2), round(t1, 2)))
    return out


def analyse(src: Path, timing: Path, min_pause: float = MIN_PAUSE,
            still: float = STILL_FRACTION) -> dict:
    gaps = word_gaps(timing, min_pause)
    series, fps = motion_series(src)
    out = []
    for start, end in gaps:
        runs = still_runs(series, fps, start, end, still, min_pause)
        dead = sum(b - a for a, b in runs)
        out.append({
            "start": start, "end": end, "seconds": round(end - start, 2),
            "still": [{"start": a, "end": b, "seconds": round(b - a, 2)}
                      for a, b in runs],
            "dead_seconds": round(dead, 2),
            "verdict": "cut" if dead >= min_pause else "keep",
        })
    total = sum(p["dead_seconds"] for p in out)
    return {"src": str(src), "pauses": out,
            "dead_seconds": round(total, 2),
            "min_pause": min_pause, "still_threshold": still}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", required=True, type=Path)
    p.add_argument("--timing", required=True, type=Path,
                   help="transcript from `transcribe.py --words`")
    p.add_argument("--min", dest="min_pause", type=float, default=MIN_PAUSE,
                   help="shortest gap counted as a pause, seconds")
    p.add_argument("--still", type=float, default=STILL_FRACTION,
                   help="motion below this fraction of the busiest frame counts "
                        "as a still picture")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    report = analyse(args.src, args.timing, args.min_pause, args.still)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if not report["pauses"]:
        print(f"no pauses longer than {args.min_pause}s")
        return
    print(f"{'silence':>15}  {'len':>5}   nothing on screen during it")
    for p_ in report["pauses"]:
        window = f"{p_['start']:6.2f}-{p_['end']:6.2f}"
        if p_["still"]:
            spans = ", ".join(f"{s['start']:.2f}-{s['end']:.2f} ({s['seconds']:.1f}s)"
                              for s in p_["still"])
        else:
            spans = "— something moves throughout, the silence carries it"
        print(f"{window:>15}  {p_['seconds']:5.2f}s  {spans}")
    print(f"\n{report['dead_seconds']:.1f}s dead in total. Usually better fixed by "
          f"moving the clip bounds in cut.py than by cutting the middle out.")


if __name__ == "__main__":
    main()
