---
name: reels-from-podcast
description: Turn a long recording into short vertical clips. Transcribe with timecodes, pick hooks, cut, re-transcribe every cut to prove it is not broken, then subtitle. Use when asked to make reels, shorts, or clips out of a podcast, stream, webinar or interview.
---

# Reels from a podcast

You have five tools in `tools/`. They do the mechanical work — extracting, cutting,
recognising, checking. **You do the judgement**: which twenty seconds of a
two-hour recording are worth anyone's attention. No script can decide that, and
this skill does not pretend otherwise.

## The one rule

**Never hand over a clip you have not verified.**

Source timecodes lie. Voice-activity detection merges a pause into the phrase that
follows it, so a segment claims 38 seconds for three words. Cut on that number and
you produce 35 seconds of silence with a punchline stapled to the end — and it
looks fine in the transcript. The only way to know is to transcribe the finished
clip on its own and check that the words are still there.

Measured on a real 25-minute podcast: the bad cut came back with similarity 0.36
and two silence warnings; the good cut came back 0.93 and clean.

## Sequence

### 1. Transcribe the source

```bash
python tools/transcribe.py --src podcast.mp4 --out transcript.json --diarize
```

Audio is extracted locally first, so a 2 GB video uploads as ~12 MB. A 25-minute
recording takes about 100 seconds end to end. `--diarize` adds a speaker timeline —
useful when two people talk over each other and you need to know who lands the line.

### 2. Read it and propose hooks — then stop

Read `transcript.json` yourself. Look for: a claim stated flatly, a number, a
reversal, a short answer to a question the audience already has, an argument that
starts mid-thought. Ignore greetings, logistics and anything that needs context
from ten minutes earlier.

Propose **as text, not as video** — cutting first wastes everyone's time:

```
1.  19:46–20:30  (44s)  "If ChatGPT opens on your phone, forget the VPN and the laptop"
2.  13:17–13:57  (40s)  "..."
```

For each: timecode, length, the quote, one line on why it works. **Wait for the
human to choose.** Cutting is cheap; their attention is not.

### 3. Cut what they picked — as drafts

```bash
python tools/cut.py --src podcast.mp4 --start 1186.6 --end 1230.1 \
    --out clips/01.mp4 --pad 0.3 --quality draft
```

Always from the original file — never from a previous cut. `--pad` adds breathing
room on both sides; 0.3–0.5s usually stops a clip from starting mid-syllable.

**Always draft during review.** On a 4K source a draft takes 3.9s and 0.6 MB where a
final takes 54s and 46 MB — and review costs several rounds. The draft still verifies
correctly, because verification listens to the audio and the audio stays good.

### 4. Verify every clip

```bash
python tools/verify.py --clip clips/01.mp4 --expect-from transcript.json \
    --start 1186.6 --end 1230.1 --out clips/01.verify.json
```

Exit code 0 means clean. Non-zero means fix it, and the report says how:

| Code | What it means | What you do |
|---|---|---|
| `silence_head` | speech starts too late | move `--start` forward by that many seconds |
| `silence_tail` | dead air at the end | move `--end` back |
| `text_drift` | the words are not the expected ones | the source timecode was wrong — re-read the transcript and pick real bounds |
| `empty` | no speech at all | wrong window entirely |

Re-cut with corrected bounds and verify again. Do not "fix" a clip by trimming the
already-cut file — go back to the original.

### 5. Show the clips, take corrections

Now the human watches. Expect instructions in seconds: "drop the first one", "give
me two more seconds at the front", "cut it right after he says the number". Apply
by re-cutting from the original with new bounds, then verify again. Every round
trip is a fresh cut, so quality never degrades no matter how many passes.

### 6. Render the approved ones properly

Only once the human is happy, and with the exact bounds that were verified:

```bash
python tools/cut.py --src podcast.mp4 --start 1186.6 --end 1230.1 \
    --out final/01.mp4 --pad 0.3 --quality final
```

Full resolution, from the original. Nothing is upscaled from the draft.

### 7. Subtitles

```bash
python tools/transcribe.py --src clips/01.mp4 --out clips/01.json
python tools/srt.py --transcript clips/01.json --out clips/01.srt
```

Transcribe the clip rather than slicing the source transcript — the timecodes then
start at zero and the recogniser has already seen exactly what the viewer will hear.
Captions are split at word boundaries to about 42 characters, which reads on a phone.

The `.srt` drops straight into CapCut, Premiere or Resolve. Burn them in only if
asked — a sidecar file leaves the editor free to restyle.

### 8. Vertical, if the source is landscape

```bash
python tools/reframe.py --src podcast.mp4 --out final/01_vertical.mp4 --quality final
```

Skip it when the source is already vertical. The crop follows motion, so on a talking
head it tracks the speaker; on footage where the subject is still and something else
moves, use `--anchor center|left|right` and crop statically. `--plan-only` prints the
camera path without rendering, which is the cheap way to check it before committing.

## Running as an MCP server

`tools/mcp_server.py` exposes `transcribe`, `cut`, `verify` and `subtitles` as typed
MCP tools, so you call them directly instead of shelling out and parsing stdout:

```bash
claude mcp add reels -- python /path/to/tools/mcp_server.py
```

Same rules apply — verify before handing anything over, draft before final.

## What this skill deliberately does not do

**Choosing hooks for you.** That is the part where taste lives.

## Setup

Needs `ffmpeg` in PATH and a token: send `/app` to `@stereo_dictator_bot`, put it in
`.env` as `DICTATOR_TOKEN`. Recognition runs on a GPU server — nothing heavy is
installed locally, and no model is downloaded.
