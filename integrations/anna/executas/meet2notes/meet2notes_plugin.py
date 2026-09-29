"""Anna development adapter; reuses this checkout's read-only gateway."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Source development reuses this checkout; PyInstaller bundles the same modules.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
from local_meeting_ai.mcp.gateway import Meet2NotesGateway


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["status", "list", "keywords", "semantic", "transcript", "summary"]
    query: str = Field(default="", max_length=1000)
    meeting_id: int | None = Field(default=None, ge=1)
    cursor: int = Field(default=-1, ge=-1)
    summary_id: int | None = Field(default=None, ge=1)


MANIFEST = {
    "name": "meet2notes-library",
    "display_name": "Meet2Notes Local Library",
    "version": "0.1.2",
    "description": "Read meetings from Meet2Notes on the same computer as Anna Local Agent.",
    "tools": [
        {
            "name": "library",
            "description": (
                "Search meetings and read paginated transcripts or AI notes. "
                "Semantic search requires an existing RAG index. Read-only."
            ),
            "parameters": [
                {
                    "name": "action",
                    "type": "string",
                    "required": True,
                    "description": "Operation to perform on the local meeting library.",
                    "enum": ["status", "list", "keywords", "semantic", "transcript", "summary"],
                },
                {
                    "name": "query",
                    "type": "string",
                    "required": False,
                    "default": "",
                    "description": "Search text; required for keywords and semantic.",
                },
                {
                    "name": "meeting_id",
                    "type": "integer",
                    "required": False,
                    "description": (
                        "Positive meeting ID, required for transcript and summary. "
                        "Omit otherwise."
                    ),
                },
                {
                    "name": "cursor",
                    "type": "integer",
                    "required": False,
                    "default": -1,
                    "description": (
                        "Use next_cursor from the previous page; "
                        "omit for the first page."
                    ),
                },
                {
                    "name": "summary_id",
                    "type": "integer",
                    "required": False,
                    "description": (
                        "AI note version ID. Omit for newest notes; "
                        "reuse returned id for pagination."
                    ),
                },
            ],
        }
    ],
}


async def invoke(raw, gateway=None):
    try:
        args = Arguments.model_validate(raw)
        if gateway is None:
            gateway = Meet2NotesGateway()
        if args.action == "status":
            result = await gateway.status()
        elif args.action == "list":
            result = await gateway.list_meetings(
                query=args.query or None, date_from=None, date_to=None, limit=50
            )
        elif args.action in {"keywords", "semantic"}:
            if not args.query.strip():
                raise ValueError("Enter a search query.")
            if args.action == "keywords":
                result = await gateway.find_in_transcripts(
                    args.query, meeting_id=args.meeting_id, limit=20
                )
            else:
                result = await gateway.search_meetings(
                    args.query, meeting_id=args.meeting_id, top_k=8
                )
        else:
            if args.meeting_id is None:
                raise ValueError("Select a meeting first.")
            if args.action == "transcript":
                result = await gateway.get_transcript(
                    args.meeting_id,
                    start_ms=None,
                    end_ms=None,
                    cursor=args.cursor,
                    segment_limit=50,
                    max_chars=20000,
                )
            else:
                result = await gateway.get_summary(
                    args.meeting_id,
                    summary_id=args.summary_id,
                    cursor=max(0, args.cursor),
                    max_chars=20000,
                )
        return {"success": True, "data": result.model_dump(mode="json")}
    except Exception as error:
        return {"success": False, "error": str(error)}


def handle(line):
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    if (
        not isinstance(req, dict)
        or req.get("jsonrpc") != "2.0"
        or not isinstance(req.get("method"), str)
    ):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid request"},
        }
    if "id" not in req:
        return None
    response = {"jsonrpc": "2.0", "id": req["id"]}
    method = req["method"]
    if method == "initialize":
        result = {
            "protocolVersion": "2.0",
            "server_info": {"name": "meet2notes", "version": "0.1.2"},
            "capabilities": {},
        }
    elif method == "describe":
        result = MANIFEST
    elif method == "health":
        result = {"status": "ready"}
    elif method == "invoke":
        params = req.get("params", {})
        if not isinstance(params, dict) or params.get("tool") != "library":
            return {**response, "error": {"code": -32602, "message": "Expected tool: library"}}
        result = asyncio.run(invoke(params.get("arguments", {})))
    else:
        return {**response, "error": {"code": -32601, "message": "Method not found"}}
    return {**response, "result": result}


def main():
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        if line.strip():
            response = handle(line)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
