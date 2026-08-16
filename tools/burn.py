#!/usr/bin/env python3
"""Burn subtitles into the video, styled for a phone held in one hand.

A sidecar .srt is the right answer when an editor will restyle it. It is the wrong
answer for a reel: most of the audience watches muted, and a file nobody loads
shows nothing. So this renders the captions into the frame.

Defaults are chosen for vertical video, not for television:

  * large — 76 px on a 1080-wide frame, roughly what mobile apps use;
  * white with a thick outline and a soft shadow, so it survives a bright slide
    behind it as well as a dark stage;
  * sat above the bottom edge, clear of the platform's own interface, which eats
    the lower ~12% of the screen with buttons and captions of its own;
  * no background box by default — it reads as a caption, not as a subtitle track.

Three styles:

  karaoke  one line on screen, filling word by word as it is spoken (ASS \\kf).
           The line holds still; only the colour sweeps. This is what the
           platforms' own auto-captions do, and it is the default.
  word     one word highlighted and enlarged at a time, the rest of the group
           plain. Louder, more of a "clip" look.
  plain    the .srt as written, one cue at a time.

karaoke and word need per-word timings, which means transcribing with

    python transcribe.py --src clip.mp4 --out clip.json --words

and passing that file as --timing. There is deliberately no fallback that
estimates word length from letter count: it drifts by half a second, which is
exactly long enough to look broken, and the estimate cannot be told apart from
a real timing once it is in the file.

Usage:
    python burn.py --src v.mp4 --srt c.srt --timing c.json --out out.mp4
    python burn.py --src v.mp4 --srt c.srt --out out.mp4 --style plain
    python burn.py --src v.mp4 --srt c.srt --timing c.json --out o.mp4 --style word
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# Новая группа начинается, если между словами пауза длиннее этого. Иначе строка
# растягивается через молчание: человек уже замолчал, а заливка всё ползёт.
GAP_SPLIT = 0.6
# Сколько строка висит после последнего слова, если дальше тишина.
HOLD_AFTER = 1.2


def ass_colour(value: str) -> str:
    """#RRGGBB -> &HBBGGRR& — libass читает цвет задом наперёд."""
    v = value.lstrip("#")
    if len(v) != 6:
        sys.exit(f"colour must be #RRGGBB, got {value}")
    return f"&H00{v[4:6]}{v[2:4]}{v[0:2]}&".upper()


def escape_for_filter(path: Path) -> str:
    """Путь внутрь фильтра ffmpeg: двоеточие диска и слэши надо экранировать,
    иначе 'C:\\clip.srt' разбирается как имя фильтра с параметрами."""
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:")


def parse_srt(path: Path) -> list:
    """.srt -> [(начало_сек, конец_сек, текст)]."""
    import re
    stamp = re.compile(r"(\d\d):(\d\d):(\d\d)[,.](\d{1,3})")

    def secs(m):
        h, mi, s, ms = (int(x) for x in m.groups())
        return h * 3600 + mi * 60 + s + ms / 1000

    out, block = [], []
    for line in path.read_text(encoding="utf-8-sig").splitlines() + [""]:
        if line.strip():
            block.append(line)
            continue
        if len(block) >= 2:
            times = [m for m in stamp.finditer(block[1])]
            if len(times) == 2:
                text = " ".join(b.strip() for b in block[2:] if b.strip())
                out.append((secs(times[0]), secs(times[1]), text))
        block = []
    return out


def ass_time(t: float) -> str:
    if t < 0:
        t = 0.0
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


TRAILING_PUNCT = ".,;:…"


def strip_punct(word: str) -> str:
    """Убирает точку и запятую на конце слова, оставляя ? и !.

    В ленте титры пишут без точек: точка в конце строки читается как пауза,
    которой в речи нет, и на крупном кегле выглядит мусором. Вопрос и
    восклицание несут интонацию — их оставляем.
    """
    return word.rstrip(TRAILING_PUNCT) or word


def load_words(timing: Path, keep_punct: bool = False) -> list:
    """Транскрипт с word_timestamps -> плоский [(слово, начало, конец)].

    Это НАСТОЯЩИЕ границы слов: whisper считает их выравниванием по
    cross-attention, а не делит фразу пропорционально. Разница видна сразу —
    подсветка попадает в слог, а паузы получаются сами собой, потому что у
    слова перед паузой честный конец, а у следующего честное начало.
    """
    data = json.loads(timing.read_text(encoding="utf-8"))
    out = []
    for seg in data.get("segments", []):
        for w in (seg.get("words") or []):
            text = (w.get("w") or w.get("word") or "").strip()
            if not text:
                continue
            if not keep_punct:
                text = strip_punct(text)
            out.append((text, float(w["start"]), float(w["end"])))
    if not out:
        sys.exit(f"{timing} has no word timings — transcribe with --words")
    return out


def group_words(words: list, per_screen: int) -> list:
    """Слова -> группы «сколько держим на экране разом».

    Режем не только по счётчику, но и по паузе: строка не должна переживать
    молчание. Пауза — естественная граница мысли, и группа, которая её
    перешагивает, всегда выглядит запоздавшей.
    """
    groups, current = [], []
    for word, start, end in words:
        if current:
            gap = start - current[-1][2]
            if len(current) >= per_screen or gap > GAP_SPLIT:
                groups.append(current)
                current = []
        current.append((word, start, end))
    if current:
        groups.append(current)
    return groups


def group_end(group: list, nxt: list) -> float:
    """Когда группа уходит с экрана: перед следующей, но не висит в пустоте."""
    last = group[-1][2]
    if nxt is None:
        return last + HOLD_AFTER
    return min(nxt[0][1], last + HOLD_AFTER)


def karaoke_events(groups: list) -> list:
    """Одна реплика на группу, заливка тегами \\kf.

    Так это делают везде, и не случайно: строка стоит на месте, двигается только
    цвет. Отдельная реплика на слово (первый заход) заставляет libass
    перерисовывать строку на каждом слове — на глаз это рывок, даже когда
    тайминги точные.

    \\kf<сотые> заливает слово из SecondaryColour в PrimaryColour за это время.
    \\k<сотые> — та же длительность, но БЕЗ заливки: так проходят паузы между
    словами, поэтому в молчании ничего не ползёт.
    """
    events = []
    for i, group in enumerate(groups):
        nxt = groups[i + 1] if i + 1 < len(groups) else None
        start = group[0][1]
        cursor = start
        parts = []
        for j, (word, w_start, w_end) in enumerate(group):
            if j:
                parts.append(" ")
            gap = w_start - cursor
            if gap > 0.02:
                parts.append("{\\k%d}" % int(round(gap * 100)))
            parts.append("{\\kf%d}%s" % (max(1, int(round((w_end - w_start) * 100))), word))
            cursor = w_end
        tail = group_end(group, nxt) - cursor
        if tail > 0.02:
            parts.append("{\\k%d}" % int(round(tail * 100)))
        events.append((start, group_end(group, nxt), "".join(parts)))
    return events


def word_events(groups: list, highlight: str, scale: int) -> list:
    """По реплике на слово: группа на экране, активное слово подсвечено и крупнее.

    Громче караоке-заливки и заметно резче. Оставлено как опция — на коротких
    рубленых фразах читается лучше, чем плавная заливка.
    """
    hi = ass_colour(highlight)
    events = []
    for i, group in enumerate(groups):
        nxt = groups[i + 1] if i + 1 < len(groups) else None
        for idx, (_, w_start, _) in enumerate(group):
            end = group[idx + 1][1] if idx + 1 < len(group) else group_end(group, nxt)
            parts = []
            for j, (word, _, _) in enumerate(group):
                if j == idx:
                    parts.append(f"{{\\c{hi}\\fscx{scale}\\fscy{scale}}}{word}{{\\r}}")
                else:
                    parts.append(word)
            events.append((w_start, end, " ".join(parts)))
    return events


def build_ass(events: list, width: int, height: int, size: int, primary: str,
              secondary: str, outline: int, margin: int, font: str,
              box: bool) -> str:
    """Собираем .ass сами — ТОЛЬКО так размеры оказываются в пикселях кадра.

    В .srt разрешения нет, libass подставляет своё (384x288) и масштабирует под
    видео: кегль 76 превращается в ~500, отступ снизу 269 — в 1793, и титр
    улетает за верхнюю кромку. Здесь PlayResX/Y равны реальному кадру, поэтому
    76 — это 76 пикселей, а 269 — это 269 пикселей от низа.

    PrimaryColour = цвет УЖЕ спетого, SecondaryColour = ещё не спетого: именно
    в эту сторону сдвигает \\kf. Для остальных стилей они совпадают.
    """
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Reel,{font},{size},{ass_colour(primary)},{ass_colour(secondary)},"
        f"&H00000000&,&H80000000&,-1,0,0,0,100,100,0,0,{3 if box else 1},"
        f"{outline},2,2,80,80,{margin},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    body = [
        f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Reel,,0,0,0,,{t}"
        for a, b, t in events
    ]
    return "\n".join(head + body) + "\n"


def probe_size(path: Path) -> tuple:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    w, h = out.stdout.strip().rstrip(",").split(",")[:2]
    return int(w), int(h)


def burn(src: Path, out: Path, srt: Path = None, timing: Path = None,
         size: int = 76, colour: str = "#FFFFFF", outline: int = 5,
         margin_frac: float = 0.30, font: str = "Arial", box: bool = False,
         quality: str = "final", style: str = "karaoke", words: int = 4,
         highlight: str = "#FFE94A", scale: int = 112,
         keep_punct: bool = False) -> dict:
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found in PATH")

    width, height = probe_size(src)
    margin = int(round(height * margin_frac))
    primary = secondary = colour
    cue_count = 0

    if style == "plain":
        if not srt or not srt.is_file():
            sys.exit("--srt is required for --style plain")
        events = parse_srt(srt)
        cue_count = len(events)
        if not events:
            sys.exit(f"no cues parsed from {srt}")
        groups = []
    else:
        if not timing or not timing.is_file():
            sys.exit(f"--style {style} needs per-word timings. Run:\n"
                     f"  python transcribe.py --src {src} --out timings.json --words\n"
                     f"then pass --timing timings.json")
        groups = group_words(load_words(timing, keep_punct), words)
        if style == "karaoke":
            events = karaoke_events(groups)
            primary, secondary = highlight, colour
        else:
            events = word_events(groups, highlight, scale)

    ass_path = out.parent / (out.stem + ".ass")
    out.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(
        build_ass(events, width, height, size, primary, secondary, outline,
                  margin, font, box),
        encoding="utf-8",
    )
    vf = f"ass='{escape_for_filter(ass_path)}'"

    crf, preset = ("28", "veryfast") if quality == "draft" else ("18", "slow")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", vf,
         "-c:v", "libx264", "-preset", preset, "-crf", crf,
         "-c:a", "copy", "-movflags", "+faststart", str(out)],
        check=True, stdout=subprocess.DEVNULL,
    )
    return {"src": str(src), "out": str(out), "ass": str(ass_path),
            "video_size": f"{width}x{height}", "font_size": size,
            "style": style, "cues": cue_count or None,
            "groups": len(groups) or None, "events": len(events),
            "words_per_screen": words if style != "plain" else None,
            "margin_bottom_px": margin, "box": box,
            "bytes": out.stat().st_size}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--srt", type=Path, default=None,
                   help="subtitle file — required for --style plain")
    p.add_argument("--timing", type=Path, default=None,
                   help="transcript from `transcribe.py --words`, with per-word "
                        "start/end. Required for karaoke and word styles")
    p.add_argument("--size", type=int, default=76,
                   help="64-88 reads best on a 1080x1920 phone screen")
    p.add_argument("--style", choices=["karaoke", "word", "plain"],
                   default="karaoke",
                   help="karaoke: line fills word by word. word: one word "
                        "highlighted at a time. plain: the .srt as written")
    p.add_argument("--words", type=int, default=4,
                   help="words on screen at once (4-6 reads best)")
    p.add_argument("--highlight", default="#FFE94A")
    p.add_argument("--scale", type=int, default=112,
                   help="percent size of the highlighted word (--style word)")
    p.add_argument("--colour", "--color", dest="colour", default="#FFFFFF")
    p.add_argument("--outline", type=int, default=5)
    p.add_argument("--margin", type=float, default=0.30,
                   help="bottom margin as a fraction of frame height. 0.30 keeps "
                        "the text clear of the bottom quarter, where the platform "
                        "puts its own buttons")
    p.add_argument("--keep-punct", action="store_true",
                   help="keep trailing dots and commas. Off by default — a full "
                        "stop at the end of a caption reads as a pause the "
                        "speaker never made")
    p.add_argument("--font", default="Arial")
    p.add_argument("--box", action="store_true",
                   help="draw a filled box behind the text instead of an outline")
    p.add_argument("--quality", choices=["draft", "final"], default="final")
    args = p.parse_args()

    print(json.dumps(burn(args.src, args.out, args.srt, args.timing, args.size,
                          args.colour, args.outline, args.margin, args.font,
                          args.box, args.quality, args.style, args.words,
                          args.highlight, args.scale, args.keep_punct),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
