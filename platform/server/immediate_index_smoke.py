"""H09A：caseDict、上传与 Agent 审批写入后，无 watcher 立即可搜索。"""
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = "http://localhost:8000/api/v1"
PORT = 9022
MARKER = f"IMMEDIATE_INDEX_{time.time_ns()}"


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    with urllib.request.urlopen(request, data, timeout=30) as response:
        return json.loads(response.read())


def sse(path, token, body=None):
    request = urllib.request.Request(B + path, method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


def search(case_id, marker, token, expected_path):
    result = call("GET", f"/cases/{case_id}/search?{urllib.parse.urlencode({'q': marker})}", token=token)
    assert any(hit["path"] == expected_path for hit in result["hits"]), (marker, result)


registered = call("POST", "/auth/register", {
    "email": f"immediate-index-{time.time_ns()}@vibeflow.cn",
    "name": "immediate-index", "password": "immediate-index-pass"})
token = registered["token"]
workspace = call("POST", "/workspaces", {"name": f"immediate-index-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"immediate-index-{time.time_ns()}"}, token=token)
case_id = case["id"]
call("POST", f"/cases/{case_id}/index:rebuild", {}, token=token)

current = call("GET", f"/cases/{case_id}/caseDict", token=token)
call("PUT", f"/cases/{case_id}/caseDict", {"content": current["content"] + f"\n// {MARKER}_CASEDICT\n"}, token=token)
search(case_id, f"{MARKER}_CASEDICT", token, "0-caseDict/caseDict")
print("1. caseDict 写入即时索引（无 WS/等待）[OK]")

boundary = f"----vibeflow{time.time_ns()}"
path = "Ref/upload-immediate.txt"
content = f"{MARKER}_UPLOAD".encode()
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"upload.txt\"\r\n"
        "Content-Type: text/plain\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
request = urllib.request.Request(B + f"/cases/{case_id}/upload?{urllib.parse.urlencode({'path': path})}",
    data=body, method="POST", headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": f"multipart/form-data; boundary={boundary}"})
with urllib.request.urlopen(request, timeout=30) as response:
    assert json.loads(response.read())["ok"]
search(case_id, f"{MARKER}_UPLOAD", token, path)
print("2. multipart 上传即时索引（无 WS/等待）[OK]")


class ImmediateMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if request["messages"][-1]["role"] == "tool":
            events = [{"choices": [{"delta": {"content": "IMMEDIATE_DONE"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        else:
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_immediate",
                "function": {"name": "propose_file_update", "arguments": json.dumps({
                    "path": "Ref/agent-immediate.txt", "content": f"{MARKER}_AGENT", "reason": "index"})}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


mock = ThreadingHTTPServer(("127.0.0.1", PORT), ImmediateMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "immediate-index-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-immediate-index", "api_key": "mock-key"}, token=token)
    conversation = call("POST", "/conversations", {
        "case_id": case_id, "title": "immediate index", "provider_profile_id": profile["id"],
        "model": "mock-immediate-index"}, token=token)
    events = sse(f"/conversations/{conversation['id']}/messages", token, {
        "content": "write", "provider_profile_id": profile["id"], "model": "mock-immediate-index"})
    tool = next(event["tool_call"] for event in events if event["type"] == "approval.requested")
    approved = sse(f"/conversations/{conversation['id']}/tool-calls/{tool['id']}:approve", token)
    result = next(event for event in approved if event["type"] == "tool.result")
    assert result["ok"], result
    search(case_id, f"{MARKER}_AGENT", token, "Ref/agent-immediate.txt")
    print("3. Agent 审批写入即时索引（无 WS/等待）[OK]")
finally:
    mock.shutdown()

print("\n=== caseDict/上传/Agent 即时索引冒烟通过 ===")
