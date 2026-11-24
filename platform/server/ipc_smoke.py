"""F52/F38：有序 IPC、33ms 合并与 Session/Run/Request 生命周期。"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = "http://localhost:8000/api/v1"
PORT = 9019


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


class IpcMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if request["messages"][-1].get("content") == "fail":
            self.send_response(500); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(b'{"error":"mock request failure"}')
            return
        if request["messages"][-1]["role"] == "tool":
            events = [{"choices": [{"delta": {"content": "DONE"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        else:
            events = [
                {"choices": [{"delta": {"content": "A"}, "index": 0}]},
                {"choices": [{"delta": {"content": "B"}, "index": 0}]},
                {"choices": [{"delta": {"reasoning_content": "R"}, "index": 0}]},
                {"choices": [{"delta": {"content": "C"}, "index": 0}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "ipc_tool",
                    "function": {"name": "list_files", "arguments": "{}"}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]},
            ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if request["messages"][-1]["role"] != "tool":
            self.wfile.write(body[:body.find(b'data: {"choices": [{"delta": {"content": "C"}')])
            self.wfile.flush()
            time.sleep(0.1)
            self.wfile.write(body[body.find(b'data: {"choices": [{"delta": {"content": "C"}'):])
        else:
            self.wfile.write(body)

    def log_message(self, *args):
        pass


email = f"ipc-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "ipc-smoke", "password": "ipc-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"ipc-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"ipc-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), IpcMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "ipc-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-ipc", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {"case_id": case["id"], "title": "ipc",
                                          "provider_profile_id": profile["id"], "model": "mock-ipc"}, token=token)["id"]
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": "ipc", "provider_profile_id": profile["id"], "model": "mock-ipc"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    text = [event for event in events if event["type"] == "text.delta"]
    assert [event["text"] for event in text] == ["AB", "C", "DONE"], text
    reasoning = [event for event in events if event["type"] == "reasoning.delta"]
    assert len(reasoning) == 1 and reasoning[0]["text"] == "R", reasoning
    types = [event["type"] for event in events]
    assert types.index("text.delta") < types.index("reasoning.delta") < types.index("tool.state"), types
    assert types.index("tool.state") < types.index("tool.result") < len(types) - 1, types
    seq = [event["seq"] for event in events if "seq" in event]
    assert seq and seq == sorted(set(seq)) and len(seq) == len(set(seq)), seq
    run_ids = {event["run_id"] for event in events if "seq" in event}
    assert len(run_ids) == 1, run_ids
    run_id = next(iter(run_ids))
    detail = call("GET", f"/conversations/{cid}", token=token)
    requests = [event["payload"] for event in detail["events"] if event["type"] == "request_state"]
    assert len(requests) == 4 and [p["status"] for p in requests] == ["started", "completed"] * 2, requests
    assert [p["request_index"] for p in requests] == [0, 0, 1, 1], requests
    assert len({p["request_id"] for p in requests}) == 2, requests
    assert all(p["run_id"] == run_id for p in requests), requests
    assert all(p["started_at"] > 0 for p in requests), requests
    assert all(p["duration_ms"] >= 0 and p["ended_at"] >= p["started_at"]
               for p in requests if p["status"] == "completed"), requests
    print("1. 相邻 AB 合并、reasoning 顺序、C 在工具状态前 [OK]")
    print("2. 全 run seq 严格递增；两个 Request 独立 ID/序号并完成计时 [OK]")

    failed_cid = call("POST", "/conversations", {"case_id": case["id"], "title": "ipc-failure",
        "provider_profile_id": profile["id"], "model": "mock-ipc"}, token=token)["id"]
    failed_request = urllib.request.Request(f"{B}/conversations/{failed_cid}/messages", method="POST",
        data=json.dumps({"content": "fail", "provider_profile_id": profile["id"], "model": "mock-ipc"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(failed_request, timeout=30) as response:
        response.read()
    failed_detail = call("GET", f"/conversations/{failed_cid}", token=token)
    failed_requests = [event["payload"] for event in failed_detail["events"] if event["type"] == "request_state"]
    assert [p["status"] for p in failed_requests] == ["started", "failed"], failed_requests
    assert failed_requests[0]["request_id"] == failed_requests[1]["request_id"], failed_requests
    failed_runs = [event["payload"] for event in failed_detail["events"] if event["type"] == "run_state"]
    assert failed_runs[-1]["status"] == "failed", failed_runs
    print("3. provider 失败同时落 Request failed 与 Run failed，ID 不串联 [OK]")
    print("\n=== AgentHost 有序 IPC/33ms 合并冒烟通过 ===")
finally:
    mock.shutdown()
