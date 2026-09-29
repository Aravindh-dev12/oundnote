"""Read-only checks against the installed Anna Executa; prints no meeting content."""

import argparse
import json
import re
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--exe", type=Path, required=True)
args = parser.parse_args()
results = []


def record(name, state, detail=""):
    results.append({"check": name, "status": state, "detail": detail})
    print(f"{state}: {name} {detail}", flush=True)


def rpc(method, params=None):
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    completed = subprocess.run(
        [str(args.exe)],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=140,
        check=True,
    )
    response = json.loads(completed.stdout)
    assert response["id"] == 1 and "error" not in response
    return response["result"]


def invoke(action, **kwargs):
    return rpc("invoke", {"tool": "library", "arguments": {"action": action, **kwargs}})


manifest = rpc("describe")
assert manifest["version"] == "0.1.2"
assert isinstance(manifest["tools"][0]["parameters"], list)
record("installed_protocol", "PASS", manifest["version"])
status = invoke("status")
assert status["success"] and status["data"]["connected"] and status["data"]["enabled"]
record("live_connection", "PASS")
listed = invoke("list")
assert listed["success"]
meetings = listed["data"]["meetings"]
record("meeting_list", "PASS", f"{len(meetings)} entries")
assert not invoke("delete")["success"]
assert not invoke("transcript", meeting_id=0)["success"]
record("invalid_and_write_operations_rejected", "PASS")
transcript = None
summary_seen = False
for meeting in meetings[:10]:
    mid = meeting["id"]
    if transcript is None:
        out = invoke("transcript", meeting_id=mid)
        if out["success"] and out["data"]["segments"]:
            transcript = out["data"]
            for segment in transcript["segments"]:
                assert segment["start_ms"] >= 0 and segment["end_ms"] >= segment["start_ms"]
                assert isinstance(segment["speaker"], str) and isinstance(segment["text"], str)
            record("live_transcript_and_timestamps", "PASS")
            cursor = transcript["next_cursor"]
            if cursor is not None:
                following = invoke("transcript", meeting_id=mid, cursor=cursor)
                assert following["success"]
                assert all(s["segment_index"] > cursor for s in following["data"]["segments"])
                record("live_transcript_second_page", "PASS")
            else:
                record("live_transcript_second_page", "SKIP", "Selected transcript fits one page")
    if not summary_seen:
        out = invoke("summary", meeting_id=mid)
        if out["success"]:
            note = out["data"]
            summary_seen = True
            assert note["meeting_id"] == mid and note["status"] == "completed"
            record("live_ai_notes", "PASS")
            if note["next_cursor"] is not None:
                second = invoke(
                    "summary", meeting_id=mid, summary_id=note["id"], cursor=note["next_cursor"]
                )
                assert second["success"] and second["data"]["id"] == note["id"]
                record("live_notes_second_page", "PASS")
            else:
                record("live_notes_second_page", "SKIP", "Selected notes fit one page")
    if transcript is not None and summary_seen:
        break
if transcript is None:
    record("live_transcript_and_search", "SKIP", "No transcript found in first ten meetings")
else:
    text = " ".join(s["text"] for s in transcript["segments"])
    words = re.findall(r"[^\W\d_]{5,}", text, flags=re.UNICODE)
    if words:
        query = max(words, key=len)
        out = invoke("keywords", query=query, meeting_id=transcript["meeting_id"])
        assert out["success"] and out["data"]["results"]
        assert all(r["meeting_id"] == transcript["meeting_id"] for r in out["data"]["results"])
        record("live_keyword_search", "PASS", "Found term taken from the transcript")
    rag = status["data"].get("rag") or {}
    if rag.get("enabled") and rag.get("chunks", 0) > 0:
        out = invoke("semantic", query="decisiones y tareas pendientes")
        if out["success"]:
            assert isinstance(out["data"]["results"], list)
            assert all(
                r["meeting_id"] > 0 and r["end_ms"] >= r["start_ms"] for r in out["data"]["results"]
            )
            record(
                "live_semantic_search",
                "PASS",
                f"{len(out['data']['results'])} results with provenance",
            )
        else:
            record(
                "live_semantic_search", "FAIL", "Backend returned an error; investigate separately"
            )
    else:
        record("live_semantic_search", "SKIP", "RAG disabled or index empty")
if not summary_seen:
    record("live_ai_notes", "SKIP", "No completed notes found in first ten meetings")
report = Path(__file__).with_name("acceptance-results.json")
report.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
if any(r["status"] == "FAIL" for r in results):
    raise SystemExit(1)
