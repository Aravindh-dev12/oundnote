"""Exercise the installed binary against an isolated loopback HTTP fixture."""

import argparse
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--exe", required=True)
args = parser.parse_args()
state = {"enabled": False}
content = "Synthetic notes. " * 3000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/api/mcp/status":
            payload = {"enabled": state["enabled"]}
        elif self.path.startswith("/api/summaries/7"):
            payload = {
                "id": 7,
                "meeting_id": 2,
                "transcription_id": 3,
                "provider": "fixture",
                "model": "fixture",
                "status": "completed",
                "created_at": "2026-09-28",
                "content_markdown": content,
            }
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
env = {**os.environ, "M2N_MCP_BASE_URL": f"http://127.0.0.1:{server.server_port}"}


def call(arguments):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "invoke",
        "params": {"tool": "library", "arguments": arguments},
    }
    p = subprocess.run(
        [args.exe],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=25,
        check=True,
    )
    return json.loads(p.stdout)["result"]


try:
    out = call({"action": "list"})
    assert not out["success"] and "disabled" in out["error"]
    print("PASS: installed binary rejects access when MCP is disabled")
    state["enabled"] = True
    chunks = []
    cursor = 0
    while True:
        out = call({"action": "summary", "meeting_id": 2, "summary_id": 7, "cursor": cursor})
        assert out["success"]
        page = out["data"]
        chunks.append(page["content_markdown"])
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert "".join(chunks) == content and len(chunks) > 1
    print(f"PASS: installed binary reconstructs long notes across {len(chunks)} pages without loss")
finally:
    server.shutdown()
    server.server_close()
    thread.join()
out = call({"action": "status"})
assert out["success"] and not out["data"]["connected"]
assert out["data"]["error_code"] == "meet2notes_not_running"
out = call({"action": "list"})
assert not out["success"] and "not reachable" in out["error"]
print("PASS: disconnected backend gives actionable status and read errors")
