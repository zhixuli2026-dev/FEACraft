"""LLM 网关冒烟（无真实凭据版）：
本地 mock provider（同时讲 OpenAI 和 Anthropic 两种 SSE 协议）→ 注册 profile → 建会话 → 发消息 → 收规范化事件 → 验证落库
"""
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
MOCK_PORT = int(os.environ.get("VIBEFLOW_TEST_MOCK_PORT", "8990"))

# ---------- mock provider：两种协议各出一条 canned 流 ----------
class MockLLM(BaseHTTPRequestHandler):
    def _sse(self, lines):
        body = "".join(f"data: {json.dumps(l)}\n\n" for l in lines).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        assert req.get("stream") is True, "请求必须是 stream=true"
        if self.path == "/v1/chat/completions":     # OpenAI 协议
            self._sse([
                {"choices": [{"delta": {"content": "有限"}, "index": 0}]},
                {"choices": [{"delta": {"content": "体积法"}, "index": 0}]},
                {"choices": [{"delta": {"content": "是一种守恒离散格式。"}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]},
            ])
        elif self.path == "/v1/messages":            # Anthropic 协议
            assert req.get("max_tokens"), "anthropic 协议需要 max_tokens"
            self._sse([
                {"type": "message_start", "message": {"id": "msg_mock"}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "有限体积法"}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "把求解域剖成控制体。"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ])
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *a):  # 静默
        pass

mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockLLM)
threading.Thread(target=mock.serve_forever, daemon=True).start()
print(f"mock provider on :{MOCK_PORT}")

# ---------- 平台侧 ----------
def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token: req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data) as r:
        return json.loads(r.read())

def sse_collect(path, body, token):
    req = urllib.request.Request(B + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events, text = [], []
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("data:"):
                ev = json.loads(s[5:].strip())
                events.append(ev.get("type"))
                if ev.get("type") == "text.delta":
                    text.append(ev.get("text", ""))
    return events, "".join(text)

email = f"llm-{time.time_ns()}@vibeflow.cn"
tok = call("POST", "/auth/register", {"email": email, "name": "llm-smoke", "password": "llm-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"llm-ws-{time.time_ns()}"}, token=tok)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"llm-case-{time.time_ns()}"}, token=tok)
case_id = case["id"]

for provider, expect_text in [
    ("custom", "有限体积法是一种守恒离散格式。"),
    ("anthropic", "有限体积法把求解域剖成控制体。"),
]:
    p = call("POST", "/provider-profiles", {
        "provider": provider, "name": f"mock-{provider}",
        "base_url": f"http://127.0.0.1:{MOCK_PORT}",
        "default_model": "mock-model", "api_key": "mock-key"}, token=tok)
    cid = call("POST", "/conversations",
               {"case_id": case_id, "title": f"mock-{provider}",
                "provider_profile_id": p["id"], "model": "mock-model"}, token=tok)["id"]
    events, text = sse_collect(f"/conversations/{cid}/messages",
        {"content": "什么是有限体积法？", "provider_profile_id": p["id"], "model": "mock-model"}, tok)
    print(f"[{provider}] 事件: {events} | 回答: {text}")
    assert expect_text in text, f"{provider} 文本不符: {text!r}"
    assert "run.completed" in events or "stream.done" in events
    assert "message.completed" in events

    conv = call("GET", f"/conversations/{cid}", token=tok)
    roles = [e["payload"]["role"] for e in conv["events"] if e["type"] == "message"]
    assert roles == ["user", "assistant"], roles
    print(f"[{provider}] 事件落库 [OK] (user+assistant)")

print("\n=== LLM 网关冒烟通过（双协议 + 规范化 + 落库）===")
mock.shutdown()
