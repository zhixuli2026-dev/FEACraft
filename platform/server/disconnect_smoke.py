"""F34：客户端断开后 provider 流立即取消，并释放会话运行注册。"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = "http://localhost:8000/api/v1"
PORT = 9012


class SlowMock(BaseHTTPRequestHandler):
    received = threading.Event()
    disconnected = threading.Event()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        json.loads(self.rfile.read(length) or b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            if self.server.fast:
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"done"},"index":0}]}\n\n')
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n')
                self.wfile.flush()
                return
            for index in range(100):
                body = f'data: {json.dumps({"choices": [{"delta": {"content": f"part-{index}", "index": 0}}]})}\n\n'.encode()
                self.wfile.write(body)
                self.wfile.flush()
                self.received.set()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.disconnected.set()

    def log_message(self, *args):
        pass


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


mock = ThreadingHTTPServer(("127.0.0.1", PORT), SlowMock)
mock.fast = False
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    email = f"disconnect-{time.time_ns()}@vibeflow.cn"
    token = call("POST", "/auth/register", {"email": email, "name": "disconnect-smoke", "password": "disconnect-pass-123"})["token"]
    workspace = call("POST", "/workspaces", {"name": f"disconnect-ws-{time.time_ns()}"}, token=token)
    case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"disconnect-{time.time_ns()}"}, token=token)
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "disconnect-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-disconnect", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case["id"], "title": "disconnect", "provider_profile_id": profile["id"],
        "model": "mock-disconnect"}, token=token)["id"]
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": "slow", "provider_profile_id": profile["id"],
                         "model": "mock-disconnect"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    response = urllib.request.urlopen(request, timeout=30)
    assert SlowMock.received.wait(3), "mock 未收到请求"
    response.readline()
    closed_at = time.monotonic()
    response.close()
    assert SlowMock.disconnected.wait(3), "客户端断开后 provider 连接未取消"
    elapsed = time.monotonic() - closed_at
    assert elapsed < 3, elapsed
    print(f"1. 客户端断开传播到 provider，耗时 {elapsed:.3f}s [OK]")

    for _ in range(30):
        state = call("POST", f"/conversations/{cid}/messages:cancel", {}, token=token)
        if state.get("ok") is False:
            break
        time.sleep(0.05)
    assert state.get("ok") is False, state
    mock.fast = True
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": "next", "provider_profile_id": profile["id"],
                         "model": "mock-disconnect"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    assert any(event["type"] == "run.completed" for event in events), events
    print("2. 运行注册释放后下一回合正常 [OK]")
    print("\n=== 客户端断开取消与恢复冒烟通过 ===")
finally:
    mock.shutdown()
