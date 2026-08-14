# STEREO Reels

An agent skill that turns a long recording into short vertical clips. It transcribes
the source with timecodes, lets the agent propose hooks, cuts what the human picks —
and then transcribes every finished clip again to prove the cut is not broken. Five
small tools, `ffmpeg`, and one HTTP call to a speech recogniser. No local model.

**Status:** working, used on real recordings. Auto-reframing is not implemented.

---

## The problem it solves

Everyone building clips from podcasts hits the same wall: the transcript timecodes
are not trustworthy.

Voice-activity detection merges a pause into the phrase after it. A segment then
claims **38 seconds for three words** — and nothing about it looks wrong in the
transcript. Cut on that number and you get 35 seconds of silence with a punchline
at the end. You only find out by watching, which defeats the point of automating.

Real numbers from a 25-minute podcast, `large-v2` on a GPU:

| | |
|---|---|
| Audio extracted locally | 35 MB → 11.7 MB in 11s |
| Transcribed | 1488s of audio in 100s — 15× faster than realtime |
| Segments / speaker turns | 265 / 322 |
| Bad cut, caught automatically | similarity **0.36**, silence at both ends |
| Good cut | similarity **0.93**, clean |

And on a 4K vertical source, the same 13.7-second clip rendered two ways:

| | Time | Size | Resolution |
|---|---|---|---|
| `--quality draft` | **3.9s** | 0.6 MB | 406×720 |
| `--quality final` | 54.4s | 45.7 MB | 2160×3840 |

**14× faster, 70× smaller.** Review runs on drafts; only approved clips are rendered
at full quality. The draft still verifies clean (0.93) because the audio is left
good enough for the recogniser — which is the only thing verification listens to.

## How it works

```
source video
   │  ffmpeg: strip the audio locally, upload ~12 MB instead of gigabytes
   ▼
transcribe ──► transcript.json      timecodes + text + speaker timeline
   │
   │  the AGENT reads it and proposes hooks as text
   │  the HUMAN picks — nothing is cut before that
   ▼
cut ──────────► clip.mp4            exact, re-encoded, always from the original
   │
   ▼
verify ───────► clip.verify.json    transcribe the clip ALONE, compare to expectation
   │                                 silence_head · silence_tail · text_drift · empty
   │  failed? re-cut with corrected bounds. never patch a cut file.
   ▼
srt ──────────► clip.srt            timecodes rebased to zero, split for a phone screen
```

The verification step is the point of the whole thing. A clip is not approved
because someone watched it — it is approved because the same recogniser, given only
the clip and no context, heard the words it was supposed to contain.

## Tools

| Tool | Does |
|---|---|
| `tools/transcribe.py` | media → `transcript.json`; extracts audio locally first, optional diarization |
| `tools/cut.py` | exact cut from the original, re-encoded so it lands on the frame, not the keyframe |
| `tools/verify.py` | re-transcribes a clip and reports four specific failure modes |
| `tools/srt.py` | subtitles with timecodes rebased to the clip, split at word boundaries |
| `SKILL.md` | the sequence an agent follows, including where it must stop and ask |

Each is a standalone script with `--help`. Nothing imports a framework.

## Why cutting re-encodes

`ffmpeg -c copy` can only cut on keyframes, so the result drifts by up to the
keyframe interval — commonly 2–5 seconds. On a 30-second reel that is the difference
between landing the punchline and missing it. Re-encoding costs a few seconds per
clip and is exact.

Every cut is made from the original file. When the reviewer says "two more seconds
at the front", the clip is cut again with new bounds rather than stacked on top of
the previous render, so ten rounds of corrections cost no quality.

## Setup

```bash
# 1. ffmpeg must be in PATH
ffmpeg -version

# 2. token: send /app to @stereo_dictator_bot
cp .env.example .env      # then paste the token into DICTATOR_TOKEN
```

Recognition runs on a GPU server, so there is nothing to install and no model to
download. Python 3.9+, standard library only.

## Quick run

```bash
python tools/transcribe.py --src podcast.mp4 --out transcript.json --diarize
# read transcript.json, choose a window, then:
python tools/cut.py    --src podcast.mp4 --start 1186.6 --end 1230.1 --out clips/01.mp4 --pad 0.3
python tools/verify.py --clip clips/01.mp4 --expect-from transcript.json --start 1186.6 --end 1230.1
python tools/transcribe.py --src clips/01.mp4 --out clips/01.json
python tools/srt.py    --transcript clips/01.json --out clips/01.srt
```

## Roadmap

- **MCP server** wrapping the same tools, so any agent can drive the pipeline without
  shelling out.
- **Auto-reframing** for static wide shots: face tracking and a camera path to 9:16.
- **Burned-in captions** as an option, with styling.
- **Batch mode**: propose, cut and verify a whole shortlist in one pass.

## Known limitations

- **Hook selection is not automated.** By design — that is the part where taste lives.
  The skill hands the agent a transcript and stops.
- **No auto-reframing.** A wide static shot has to be cropped by hand, or shot vertical.
- **Russian-first.** The recogniser is configured for Russian; other languages work
  but are not what this was tuned on.
- **Final renders of 4K sources are slow.** 54 seconds and 46 MB for a 13-second
  clip. That is why review happens on drafts; but a shortlist of twenty finals from
  a 4K master is still a coffee break.
- **One recogniser.** The pipeline talks to a single HTTP endpoint. If it is down,
  nothing runs; there is no local fallback yet.

## License

MIT © 2026 Sergey Drozdov
