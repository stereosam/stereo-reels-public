#!/usr/bin/env python3
"""MCP server exposing the reels pipeline as tools any agent can call.

Without this, an agent has to shell out and parse stdout. With it, the whole
pipeline is typed tools with schemas: the agent gets told what arguments exist
and what comes back, and a bad call fails with a message instead of a traceback.

Speaks JSON-RPC 2.0 over stdio — the transport MCP clients use for local servers.
Standard library only, no SDK: this has to run on a machine where nothing is
installed except ffmpeg and Python.

Register with Claude Code:

    claude mcp add reels -- python /path/to/tools/mcp_server.py

or in a client config:

    {"mcpServers": {"reels": {"command": "python",
                              "args": ["/path/to/tools/mcp_server.py"]}}}

The token is read the same way the scripts read it: DICTATOR_TOKEN in the
environment or in a .env file next to the project.
"""
import json
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import burn as burn_tool        # noqa: E402
import cut as cut_tool          # noqa: E402
import pauses as pause_tool     # noqa: E402
import reframe as rf_tool       # noqa: E402
import srt as srt_tool          # noqa: E402
import transcribe as tr_tool    # noqa: E402
import verify as vf_tool        # noqa: E402

SERVER = {"name": "stereo-reels", "version": "1.1.0"}
DEFAULT_PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "transcribe",
        "description": (
            "Transcribe audio or video into segments with timecodes. Video is "
            "stripped to mono AAC locally first, so a 2 GB file uploads as ~12 MB. "
            "A 25-minute recording takes about 100 seconds. Returns the path to a "
            "JSON file plus a summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "path to the media file"},
                "out": {"type": "string", "description": "where to write transcript JSON"},
                "diarize": {"type": "boolean", "default": False,
                            "description": "also return a speaker timeline"},
                "words": {"type": "boolean", "default": False,
                          "description": "also return the start and end of every word. "
                                         "Required if the captions will be burned in"},
                "max_speakers": {"type": "integer", "default": 4},
            },
            "required": ["src", "out"],
        },
    },
    {
        "name": "cut",
        "description": (
            "Cut a clip out of the source, frame-accurately, always from the original "
            "file. quality=draft is 720p and fast (use it while a human is reviewing); "
            "quality=final is full resolution and slow (use it only after approval)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "start": {"type": "number", "description": "seconds"},
                "end": {"type": "number", "description": "seconds"},
                "out": {"type": "string"},
                "pad": {"type": "number", "default": 0.3,
                        "description": "seconds of breathing room on both sides"},
                "quality": {"type": "string", "enum": ["draft", "final"], "default": "draft"},
            },
            "required": ["src", "start", "end", "out"],
        },
    },
    {
        "name": "verify",
        "description": (
            "Transcribe a finished clip on its own and check it against what it was "
            "cut for. Catches silence_head, silence_tail, text_drift and empty. "
            "Source timecodes lie — voice-activity detection merges pauses into the "
            "next phrase — so never hand over a clip that has not passed this."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "clip": {"type": "string"},
                "transcript": {"type": "string",
                               "description": "source transcript JSON the clip was cut from"},
                "start": {"type": "number"},
                "end": {"type": "number"},
                "expect_text": {"type": "string",
                                "description": "alternative to transcript+start+end"},
            },
            "required": ["clip"],
        },
    },
    {
        "name": "subtitles",
        "description": (
            "Build an .srt from a transcript, with timecodes rebased to the clip and "
            "captions split at word boundaries. Transcribe the clip itself first — "
            "slicing the source transcript leaves the timecodes twenty minutes off."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript": {"type": "string"},
                "out": {"type": "string"},
                "offset": {"type": "number", "default": 0.0,
                           "description": "seconds to subtract if the transcript is the source one"},
                "max_chars": {"type": "integer", "default": 42},
            },
            "required": ["transcript", "out"],
        },
    },
    {
        "name": "reframe",
        "description": (
            "Turn a landscape video vertical. mode=crop follows the motion in frame, "
            "which tracks a speaker but chases a moving screen if the speaker is still "
            "— check with plan_only first. mode=pad puts the whole frame at full width "
            "on a solid background with room underneath for captions, which is the "
            "right choice for slides and diagrams: a crop would slice them in half. "
            "Do not run this at all on a source that is already vertical."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "out": {"type": "string"},
                "mode": {"type": "string", "enum": ["crop", "fit", "pad"], "default": "crop"},
                "anchor": {"type": "string",
                           "description": "left | center | right | 0..1 — static crop, no analysis"},
                "quality": {"type": "string", "enum": ["draft", "final"], "default": "draft"},
                "plan_only": {"type": "boolean", "default": False,
                              "description": "compute the camera path and render nothing"},
            },
            "required": ["src"],
        },
    },
    {
        "name": "burn",
        "description": (
            "Render captions into the frame, one line at a time, filling word by word "
            "as each word is spoken. Needs a transcript made with words=true — there is "
            "no fallback that estimates word length, because an estimate drifts about "
            "half a second and cannot be told apart from a measurement afterwards. "
            "style=karaoke fills the line; style=word shows one word at a time and is "
            "the answer if the captions feel like they run ahead of the voice; "
            "style=plain renders an .srt unchanged and is the only style needing srt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "the video to caption"},
                "out": {"type": "string"},
                "timing": {"type": "string",
                           "description": "transcript JSON from transcribe with words=true"},
                "srt": {"type": "string", "description": "only for style=plain"},
                "style": {"type": "string", "enum": ["karaoke", "word", "plain"],
                          "default": "karaoke"},
                "words": {"type": "integer", "default": 4,
                          "description": "words on screen at once. Fewer means less text "
                                         "visible before it is spoken"},
                "size": {"type": "integer", "default": 76},
                "margin": {"type": "number", "default": 0.30,
                           "description": "bottom margin as a fraction of frame height"},
                "keep_punct": {"type": "boolean", "default": False,
                               "description": "keep trailing dots and commas"},
                "quality": {"type": "string", "enum": ["draft", "final"], "default": "final"},
            },
            "required": ["src", "out"],
        },
    },
    {
        "name": "pauses",
        "description": (
            "List the pauses in a clip and say what the picture was doing during each "
            "one. Silence is not automatically dead air — a speaker who stops talking "
            "while the slide changes is worth watching. Reports only; it never edits. "
            "Fix dead stretches by moving the clip bounds and cutting again, not by "
            "removing a piece from the middle, which shifts every later word timing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string"},
                "timing": {"type": "string",
                           "description": "transcript JSON from transcribe with words=true"},
                "min_pause": {"type": "number", "default": 0.6,
                              "description": "shortest gap counted as a pause, seconds"},
            },
            "required": ["src", "timing"],
        },
    },
]


def token_and_api():
    env = tr_tool.load_env(Path(".env"))
    token = os.environ.get("DICTATOR_TOKEN") or env.get("DICTATOR_TOKEN")
    if not token:
        raise RuntimeError(
            "No token. Send /app to @stereo_dictator_bot, then set DICTATOR_TOKEN "
            "in the environment or in a .env file."
        )
    api = os.environ.get("DICTATOR_API") or env.get("DICTATOR_API") or tr_tool.DEFAULT_API
    return token, api


def do_transcribe(a):
    token, api = token_and_api()
    data = tr_tool.transcribe(Path(a["src"]), token, api,
                              bool(a.get("diarize")), int(a.get("max_speakers", 4)),
                              words=bool(a.get("words")))
    out = Path(a["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    segs = data.get("segments", [])
    words = sum(len(s.get("words") or []) for s in segs)
    extra = f", {words} word timings" if words else ""
    return (f"{len(segs)} segments, {data.get('duration', 0):.0f}s of audio, "
            f"transcribed in {data.get('elapsed', 0):.0f}s{extra} -> {out}")


def do_cut(a):
    info = cut_tool.cut(Path(a["src"]), float(a["start"]), float(a["end"]),
                        Path(a["out"]), float(a.get("pad", 0.3)),
                        a.get("quality", "draft"))
    return (f"{info['quality']} clip {info['actual']['duration']}s "
            f"({info['bytes'] / 1048576:.1f} MB) -> {info['out']}")


def do_verify(a):
    token, api = token_and_api()
    expect = a.get("expect_text") or ""
    if a.get("transcript"):
        if a.get("start") is None or a.get("end") is None:
            raise RuntimeError("transcript needs start and end")
        expect = vf_tool.expected_text(Path(a["transcript"]), float(a["start"]), float(a["end"]))
    clip_data = tr_tool.transcribe(Path(a["clip"]), token, api)
    report = vf_tool.check(clip_data, expect, 1.0, 1.5, 0.6)
    report["clip"] = a["clip"]
    return json.dumps(report, ensure_ascii=False, indent=2)


def do_subtitles(a):
    data = json.loads(Path(a["transcript"]).read_text(encoding="utf-8"))
    body = srt_tool.build(data.get("segments", []), float(a.get("offset", 0.0)),
                          int(a.get("max_chars", 42)), 0.7)
    out = Path(a["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    return f"{body.count(' --> ')} captions -> {out}"


def do_reframe(a):
    out = Path(a.get("out") or "unused.mp4")
    info = rf_tool.reframe(Path(a["src"]), out, a.get("anchor"), "9:16",
                           a.get("quality", "draft"), bool(a.get("plan_only")),
                           a.get("mode", "crop"))
    src = info["source"]
    if a.get("plan_only"):
        return json.dumps(info, ensure_ascii=False, indent=2)
    return (f"{src['w']}x{src['h']} -> 1080x1920 ({info['method']}), "
            f"{info['bytes'] / 1048576:.1f} MB -> {out}")


def do_burn(a):
    timing = Path(a["timing"]) if a.get("timing") else None
    srt = Path(a["srt"]) if a.get("srt") else None
    info = burn_tool.burn(Path(a["src"]), Path(a["out"]), srt, timing,
                          int(a.get("size", 76)), "#FFFFFF", 5,
                          float(a.get("margin", 0.30)), "Arial", False,
                          a.get("quality", "final"), a.get("style", "karaoke"),
                          int(a.get("words", 4)), "#FFE94A", 112,
                          bool(a.get("keep_punct")))
    return (f"{info['events']} caption events, style {info['style']}, "
            f"{info['bytes'] / 1048576:.1f} MB -> {info['out']}")


def do_pauses(a):
    report = pause_tool.analyse(Path(a["src"]), Path(a["timing"]),
                                float(a.get("min_pause", pause_tool.MIN_PAUSE)))
    return json.dumps(report, ensure_ascii=False, indent=2)


HANDLERS = {"transcribe": do_transcribe, "cut": do_cut,
            "verify": do_verify, "subtitles": do_subtitles,
            "reframe": do_reframe, "burn": do_burn, "pauses": do_pauses}


# stdout принадлежит протоколу и никому больше. Держим настоящий stdout в стороне,
# а sys.stdout уводим в stderr: любой случайный print из импортированного модуля
# или из дочернего процесса иначе встанет посреди JSON-RPC и оборвёт соединение.
PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr


def send(msg):
    PROTOCOL_OUT.write(json.dumps(msg, ensure_ascii=False) + "\n")
    PROTOCOL_OUT.flush()


def handle(req):
    method, rid = req.get("method"), req.get("id")

    if method == "initialize":
        asked = (req.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": asked,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER,
        }}

    if method in ("notifications/initialized", "initialized"):
        return None                                  # уведомление — ответа не ждут

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": f"unknown tool: {name}"}],
                               "isError": True}}
        try:
            text = fn(args)
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": text}]}}
        except SystemExit as e:
            # Инструменты — обычные CLI-скрипты и на плохой аргумент зовут
            # sys.exit с внятным текстом. Здесь это не «завершить программу»,
            # а «вернуть ошибку»: SystemExit не наследует Exception, и без
            # отдельной ветки одна кривая команда агента гасила бы весь сервер.
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": str(e)}],
                               "isError": True}}
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            if os.environ.get("REELS_MCP_DEBUG"):
                detail += "\n" + traceback.format_exc()
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": detail}], "isError": True}}

    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "parse error"}})
            continue
        reply = handle(req)
        if reply is not None:
            send(reply)


if __name__ == "__main__":
    main()
