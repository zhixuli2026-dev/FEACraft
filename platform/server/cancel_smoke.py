"""F01/F40/F07：停止当前回合、部分消息 cancelled、注册表释放与跨用户边界。"""
import json
import http.client
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = "http://localhost:8000/api/v1"
PORT = 8995


class DelayedMock(BaseHTTPRequestHandler):
    received = threading.Event()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        chunks = ["partial"] if not self.server.fast else ["done"]
        try:
            for index, text in enumerate(chunks):
                body = f"data: {json.dumps({'choices': [{'delta': {'content': text}, 'index': 0}]})}\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                if index == 0 and not self.server.fast:
                    self.received.set()
                    time.sleep(5)
            self.wfile.write(b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\",\"index\":0}]}\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def sse_collect(cid, body, token):
    req = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(req, timeout=30) as response:
        for line in response:
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("data:"):
                events.append(json.loads(text[5:].strip()))
    return events


mock = ThreadingHTTPServer(("127.0.0.1", PORT), DelayedMock)
mock.fast = False
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    email = f"cancel-{time.time_ns()}@vibeflow.cn"
    token = call("POST", "/auth/register", {"email": email, "name": "cancel-smoke", "password": "cancel-pass-123"})["token"]
    workspace = call("POST", "/workspaces", {"name": f"cancel-ws-{time.time_ns()}"}, token=token)
    case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"cancel-{time.time_ns()}"}, token=token)
    case_id = case["id"]
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "cancel-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-cancel", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case_id, "title": "cancel", "provider_profile_id": profile["id"],
        "model": "mock-cancel"}, token=token)["id"]
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("events", sse_collect(cid, {
        "content": "start slow run", "provider_profile_id": profile["id"], "model": "mock-cancel"}, token)))
    thread.start()
    assert DelayedMock.received.wait(5), "mock 未收到运行请求"
    started = time.time()
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
    connection.request("POST", f"/api/v1/conversations/{cid}/messages:cancel", body="{}",
                       headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    cancelled = json.loads(connection.getresponse().read())
    connection.close()
    cancel_latency = time.time() - started
    print(f"TIMING http_client_cancel={cancel_latency:.3f}s")
    thread.join(8)
    elapsed = time.time() - started
    print(f"TIMING cancel_api={cancel_latency:.3f}s total={elapsed:.3f}s")
    assert cancelled.get("ok") is True, cancelled
    assert not thread.is_alive(), "取消后 LLM 请求仍未结束"
    assert elapsed < 2, f"取消耗时 {elapsed:.2f}s"
    events = result["events"]
    assert any(event["type"] == "run.cancelled" for event in events), events
    conversation = call("GET", f"/conversations/{cid}", token=token)
    assistant = next(event["payload"] for event in conversation["events"]
                     if event["type"] == "message" and event["payload"].get("role") == "assistant")
    assert assistant["cancelled"] is True and assistant["content"] == "partial", assistant
    request_states = [event["payload"] for event in conversation["events"]
                      if event["type"] == "request_state" and
                      event["payload"].get("run_id") == assistant.get("run_id")]
    assert [p["status"] for p in request_states] == ["started", "cancelled"], request_states
    assert request_states[0]["request_id"] == request_states[1]["request_id"], request_states
    print(f"1. 停止后端运行 {elapsed:.2f}s、Run/Request cancelled、部分消息 cancelled=true [OK]")

    other = call("POST", "/auth/register", {"email": f"cancel-other-{time.time_ns()}@vibeflow.cn",
                                            "name": "cancel-other", "password": "other-pass-123"})
    foreign = call("POST", f"/conversations/{cid}/messages:cancel", {}, token=other["token"])
    assert foreign.get("_err") == 404, foreign
    repeated = call("POST", f"/conversations/{cid}/messages:cancel", {}, token=token)
    assert repeated.get("ok") is False, repeated
    print("2. 跨用户 404、无运行回合重复取消 false [OK]")

    mock.fast = True
    second = sse_collect(cid, {"content": "second run", "provider_profile_id": profile["id"],
                               "model": "mock-cancel"}, token)
    assert any(event["type"] == "run.completed" for event in second), second
    print("3. 取消注册表释放后下一回合可正常运行 [OK]")
    print("\n=== Agent 停止/取消冒烟通过 ===")
finally:
    mock.shutdown()
