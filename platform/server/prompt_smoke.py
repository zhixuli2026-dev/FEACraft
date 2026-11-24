"""F18：固定追问模板与最终结构确实进入双协议系统提示。"""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = "http://localhost:8000/api/v1"
PORT = 9004


class PromptMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v1/chat/completions":
            text = req["messages"][0]["content"]
        else:
            text = req.get("system", "")
        missing = [marker for marker in ("Parameter name:", "Purpose:", "Explanation:", "Recommendation:",
                                         "状态更新", "待办", "下一步") if marker not in text]
        if missing:
            self.server.failures.append(missing)
            self.send_response(400)
            self.end_headers()
            self.wfile.write(",".join(missing).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.path == "/v1/chat/completions":
            self.wfile.write(b"data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"index\":0}]}\n\n")
            self.wfile.write(b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\",\"index\":0}]}\n\n")
        else:
            self.wfile.write(b"data: {\"type\":\"message_start\",\"message\":{\"id\":\"prompt\"}}\n\n")
            self.wfile.write(b"data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"ok\"}}\n\n")
            self.wfile.write(b"data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"}}\n\n")
            self.wfile.write(b"data: {\"type\":\"message_stop\"}\n\n")

    def log_message(self, *args):
        pass


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


mock = ThreadingHTTPServer(("127.0.0.1", PORT), PromptMock)
mock.failures = []
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    case = call("POST", "/workspaces/1/cases", {"name": f"prompt-{time.time_ns()}"}, token=token)
    for provider in ("custom", "anthropic"):
        profile = call("POST", "/provider-profiles", {
            "provider": provider, "name": f"prompt-{provider}", "base_url": f"http://127.0.0.1:{PORT}",
            "default_model": "mock-prompt", "api_key": "mock-key"}, token=token)
        cid = call("POST", "/conversations", {
            "case_id": case["id"], "title": f"prompt-{provider}", "provider_profile_id": profile["id"],
            "model": "mock-prompt"}, token=token)["id"]
        call("POST", f"/conversations/{cid}/messages", {
            "content": "需要哪些参数？", "provider_profile_id": profile["id"], "model": "mock-prompt"}, token=token)
        print(f"1. {provider} 固定追问模板和最终结构 [OK]")
    assert not mock.failures, mock.failures
    print("\n=== 固定追问模板冒烟通过 ===")
finally:
    mock.shutdown()
