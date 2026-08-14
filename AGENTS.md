# AGENTS.md

Things that are not visible from the code.

- **Never return a clip that has not passed `verify.py`.** A non-zero exit means the
  clip is broken, not that the check is fussy. Fix the bounds and cut again.
- **Re-cut from the source, never from a previous cut.** Corrections arrive in
  rounds ("two seconds earlier", "drop that one"); stacking renders loses quality
  for no reason.
- **`--quality draft` while the human is reviewing. `final` only after approval.**
  On 4K that is 3.9s versus 54s per clip, and review is never one round.
- **Stop after proposing hooks. Do not cut until a human picks.** Choosing which
  twenty seconds matter is the human's call; everything before and after it is not.
- **Source timecodes are not trustworthy.** Voice-activity detection merges pauses
  into the following phrase, so a segment can claim 38 seconds for three words.
  This is the reason verification exists — do not treat it as a formality.
- **Subtitles come from transcribing the clip, not from slicing the source
  transcript.** Timecodes then start at zero, which is what every editor expects.
- `tools/verify.py` imports `tools/transcribe.py`. Run them from `tools/`, or add
  that directory to `PYTHONPATH`.
- No dependencies beyond the standard library and `ffmpeg`. Keep it that way — this
  runs on other people's machines.
