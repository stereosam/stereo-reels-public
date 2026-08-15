#!/usr/bin/env python3
"""MCP server exposing the reels pipeline as tools any agent can call.

Without this, an agent has to shell out and parse stdout. With it, the same four
operations are typed tools with schemas: the agent gets told what arguments exist
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

import cut as cut_tool          # noqa: E402
import srt as srt_tool          # noqa: E402
import transcribe as tr_tool    # noqa: E402
import verify as vf_tool        # noqa: E402

SERVER = {"name": "stereo-reels", "version": "1.0.0"}
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
                              bool(a.get("diarize")), int(a.get("max_speakers", 4)))
    out = Path(a["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    segs = data.get("segments", [])
    return (f"{len(segs)} segments, {data.get('duration', 0):.0f}s of audio, "
            f"transcribed in {data.get('elapsed', 0):.0f}s -> {out}")


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


HANDLERS = {"transcribe": do_transcribe, "cut": do_cut,
            "verify": do_verify, "subtitles": do_subtitles}


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
