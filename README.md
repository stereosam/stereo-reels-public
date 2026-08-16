# STEREO Reels

An agent skill that turns a long recording into short vertical clips. It transcribes
the source with timecodes, lets the agent propose hooks, cuts what the human picks —
and then transcribes every finished clip again to prove the cut is not broken. Five
small tools, `ffmpeg`, and one HTTP call to a speech recogniser. No local model.

**Status:** working, used on real recordings.

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
| `tools/burn.py` | captions rendered into the frame, filling word by word as they are spoken |
| `tools/reframe.py` | landscape → vertical, following the action instead of cropping the centre |
| `tools/mcp_server.py` | the same operations as MCP tools, so an agent calls them instead of shelling out |
| `SKILL.md` | the sequence an agent follows, including where it must stop and ask |

Each is a standalone script with `--help`. Nothing imports a framework.

## As an MCP server

```bash
claude mcp add reels -- python /path/to/tools/mcp_server.py
```

Four typed tools — `transcribe`, `cut`, `verify`, `subtitles` — with schemas, so the
agent is told what arguments exist and a bad call comes back as a message instead of a
traceback. JSON-RPC over stdio, standard library only, no SDK.

One detail worth stealing: the server hands `sys.stdout` to the protocol and points
everything else at stderr. A single stray `print` — or an ffmpeg child that writes to
stdout — lands in the middle of a JSON-RPC frame and kills the session. Found it the
first time a real cut ran through the server.

## Vertical from landscape, without a dependency

```bash
python tools/reframe.py --src talk.mp4 --out vertical.mp4          # follow the action
python tools/reframe.py --src talk.mp4 --out vertical.mp4 --anchor center
python tools/reframe.py --src talk.mp4 --plan-only                 # just the camera path
```

Face detection would be the obvious way, and it costs a heavy dependency this pipeline
promised not to have. So the camera path comes from motion: ffmpeg emits small greyscale
frames, the per-column difference between neighbours says where things move, a window of
the target width slides to the busiest spot, and the centre is then pulled onto the
centroid of activity inside that window — without that last step a narrow speaker ends up
clipped at the edge of the crop.

The path is smoothed and speed-capped, so it pans rather than twitches, and is rendered
through `sendcmd` driving `crop` — one keyframe per second.

Measured on a 40-second 1280×720 stage recording: **0.7s to analyse** (119 sampled
frames), 6.5s to render a 1080×1920 draft.

## Captions that do not run ahead of the voice

```bash
python tools/transcribe.py --src clip.mp4 --out clip.json --words
python tools/burn.py --src vertical.mp4 --timing clip.json --out captioned.mp4
```

Most of the audience watches muted, so a sidecar `.srt` nobody loads shows nothing.
The captions are rendered into the frame, one line at a time, filling word by word
as each word is spoken — ASS `\kf`, so the line holds still and only the colour moves.
Pauses pass as `\k`, which holds without filling: when the speaker stops, so does the
caption.

The word boundaries come from the recogniser (`--words`), not from arithmetic. The
first version estimated them from letter count, and it drifted by about half a second
— close enough to look deliberate, far enough to look broken. That code is gone;
`burn.py` refuses to guess rather than shipping an estimate that nothing downstream
can tell apart from a measurement.

Trailing full stops are dropped by default: a period at the end of a caption reads
as a pause the speaker never made.

## Three ways to fill a vertical frame

`--mode crop` follows the action and cuts to 9:16 — right for a talking head.
`--mode pad` puts the whole frame at full width on a solid background, shifted up so
captions have somewhere to sit — right for slides, where a 9:16 crop would slice the
diagram in half. `--mode fit` fills the margins with a blurred copy instead; it flatters
footage that nearly fits and betrays a wide slide, where the blur takes over half the
screen and the captions still have nothing to sit against.

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

- **Batch mode**: propose, cut and verify a whole shortlist in one pass.
- **`reframe` and `burn` as MCP tools** — scripts today, not yet exposed over the protocol.
- **Face-aware reframing** as an opt-in extra, for footage where the subject is still and
  something else in frame moves.

## Known limitations

- **Hook selection is not automated.** By design — that is the part where taste lives.
  The skill hands the agent a transcript and stops.
- **Reframing follows motion, not faces.** On a talking head, a screencast or an
  interview that is the same thing. On a still speaker standing next to a moving
  screen it is not: the crop will chase the screen. `--anchor` overrides it.
- **Russian-first.** The recogniser is configured for Russian; other languages work
  but are not what this was tuned on.
- **Final renders of 4K sources are slow.** 54 seconds and 46 MB for a 13-second
  clip. That is why review happens on drafts; but a shortlist of twenty finals from
  a 4K master is still a coffee break.
- **One recogniser.** The pipeline talks to a single HTTP endpoint. If it is down,
  nothing runs; there is no local fallback yet.

## License

MIT © 2026 Sergey Drozdov
