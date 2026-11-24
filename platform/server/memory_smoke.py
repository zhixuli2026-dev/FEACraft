"""F36：长期记忆追加/有界遗忘与下一独立模型请求真实注入。"""
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services.memory import MAX_MEMORY_BYTES, TRUNCATION_MARKER, append_memory, compact_memory
from app.services.tools import tools_for_anthropic, tools_for_openai

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
PORT = 0
MARKER = "Use tolerance 1e-6 for the cavity verification."


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(request, data, timeout=30) as response:
        return json.loads(response.read())


def sse(cid, text, profile_id, token):
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": text, "provider_profile_id": profile_id,
                         "model": "mock-memory"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    chunks = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode().strip()
            if line.startswith("data:"):
                event = json.loads(line[5:].strip())
                if event["type"] == "text.delta":
                    chunks.append(event["text"])
    return "".join(chunks)


class MemoryMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        last = request["messages"][-1]
        if last["role"] == "tool":
            events = [{"choices": [{"delta": {"content": "saved"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        elif last.get("content") == "remember":
            args = json.dumps({"action": "append", "content": MARKER})
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "memory-1",
                "function": {"name": "update_memory", "arguments": args}}]}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        elif last.get("content") == "recall":
            system = request["messages"][0]["content"]
            answer = "MEMORY_PRESENT" if MARKER in system else "MEMORY_MISSING"
            events = [{"choices": [{"delta": {"content": answer}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        else:
            events = [{"choices": [{"delta": {"content": "ok"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


old = "old experience\n" * 10000 + "最新经验\n"
compacted = compact_memory(old)
assert len(compacted.encode("utf-8")) <= MAX_MEMORY_BYTES
assert compacted.startswith(TRUNCATION_MARKER) and compacted.endswith("最新经验\n")
combined = append_memory("first", "second")
assert combined == "first\n\nsecond\n"
for invalid in ("", " ", "x" * (16 * 1024 + 1), "bad\x00value"):
    try:
        append_memory("", invalid)
        raise AssertionError("非法记忆应拒绝")
    except ValueError:
        pass
assert any(tool["function"]["name"] == "update_memory" for tool in tools_for_openai())
assert any(tool["name"] == "update_memory" for tool in tools_for_anthropic())
print("1. 64 KiB 有界遗忘、UTF-8/最新条目、输入边界和双协议定义 [OK]")

email = f"memory-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "memory-smoke", "password": "memory-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"memory-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"memory-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), MemoryMock)
PORT = mock.server_port
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "memory-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-memory", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {"case_id": case["id"], "title": "memory",
        "provider_profile_id": profile["id"], "model": "mock-memory"}, token=token)["id"]
    assert sse(cid, "remember", profile["id"], token) == "saved"
    memory = call("GET", f"/cases/{case['id']}/file?path=0-caseDict%2Fmemory.md", token=token)
    assert MARKER in memory["content"], memory
    assert sse(cid, "recall", profile["id"], token) == "MEMORY_PRESENT"
    print("2. update_memory 实际写文件，下一独立模型请求系统上下文包含经验 [OK]")
    print("\n=== 长期记忆追加/遗忘/真实续跑注入冒烟通过 ===")
finally:
    mock.shutdown()
    mock.server_close()
