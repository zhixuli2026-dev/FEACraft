"""F06A/F06B：双协议 reasoning.delta、强度参数与 assistant 持久化分离。"""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = "http://localhost:8000/api/v1"
PORT = 8992


class ReasoningMock(BaseHTTPRequestHandler):
    def _sse(self, lines):
        body = "".join(f"data: {json.dumps(line, ensure_ascii=False)}\n\n" for line in lines).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        user = request["messages"][-1]["content"]
        if self.path == "/v1/chat/completions":
            if "plain" in user:
                assert "reasoning_effort" not in request, request
                self._sse([{"choices": [{"delta": {"content": "普通回答"}, "index": 0}]},
                           {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}])
            else:
                assert request.get("reasoning_effort") == "medium", request
                self._sse([
                    {"choices": [{"delta": {"reasoning_content": "检查输入"}, "index": 0}]},
                    {"choices": [{"delta": {"reasoning_content": "并规划。"}, "index": 0}]},
                    {"choices": [{"delta": {"content": "最终"}, "index": 0}]},
                    {"choices": [{"delta": {"content": "回答。"}, "index": 0}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]},
                ])
        elif self.path == "/v1/messages":
            assert request.get("thinking") == {"type": "enabled", "budget_tokens": 2048}, request
            assert request["max_tokens"] > 2048, request
            self._sse([
                {"type": "message_start", "message": {"id": "reasoning-mock"}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": "检查输入并规划。"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "text_delta", "text": "最终回答。"}},
                {"type": "content_block_stop", "index": 1},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ])
        else:
            self.send_response(404)
            self.end_headers()

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


def sse_collect(cid, body, token):
    req = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(req, timeout=30) as response:
        for line in response:
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("data:"):
                events.append(json.loads(text[5:].strip()))
    return events


mock = ThreadingHTTPServer(("127.0.0.1", PORT), ReasoningMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    email = f"reasoning-{time.time_ns()}@vibeflow.cn"
    token = call("POST", "/auth/register", {"email": email, "name": "reasoning-smoke", "password": "reasoning-pass-123"})["token"]
    workspace = call("POST", "/workspaces", {"name": f"reasoning-ws-{time.time_ns()}"}, token=token)
    case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"reasoning-{time.time_ns()}"}, token=token)
    case_id = case["id"]
    openai_cid = None
    openai_profile_id = None
    for provider in ("custom", "anthropic"):
        profile = call("POST", "/provider-profiles", {
            "provider": provider, "name": f"reasoning-{provider}", "base_url": f"http://127.0.0.1:{PORT}",
            "default_model": "mock-reasoning", "api_key": "mock-key"}, token=token)
        cid = call("POST", "/conversations", {
            "case_id": case_id, "title": f"reasoning-{provider}", "provider_profile_id": profile["id"],
            "model": "mock-reasoning"}, token=token)["id"]
        if provider == "custom":
            openai_cid = cid
            openai_profile_id = profile["id"]
        events = sse_collect(cid, {"content": "请推理", "provider_profile_id": profile["id"],
                                  "model": "mock-reasoning", "reasoning_effort": "medium"}, token)
        types = [event["type"] for event in events]
        assert "reasoning.delta" in types and "text.delta" in types, types
        assert types.index("reasoning.delta") < types.index("text.delta"), types
        reasoning = "".join(event.get("text", "") for event in events if event["type"] == "reasoning.delta")
        answer = "".join(event.get("text", "") for event in events if event["type"] == "text.delta")
        assert reasoning == "检查输入并规划。", (provider, reasoning)
        assert answer == "最终回答。", (provider, answer)
        conversation = call("GET", f"/conversations/{cid}", token=token)
        assistant = next(event["payload"] for event in conversation["events"]
                         if event["type"] == "message" and event["payload"].get("role") == "assistant")
        assert assistant["content"] == "最终回答。", assistant
        assert assistant.get("reasoning") == "检查输入并规划。", assistant
        assert "检查输入" not in assistant["content"], assistant
        print(f"1. {provider} reasoning 参数、事件顺序和持久化分离 [OK]")

    plain = sse_collect(openai_cid, {"content": "plain answer", "provider_profile_id": openai_profile_id,
                                     "model": "mock-reasoning"}, token)
    assert not any(event["type"] == "reasoning.delta" for event in plain), plain
    conversation = call("GET", f"/conversations/{openai_cid}", token=token)
    assistants = [event["payload"] for event in conversation["events"]
                  if event["type"] == "message" and event["payload"].get("role") == "assistant"]
    assert "reasoning" not in assistants[-1], assistants[-1]
    print("2. 默认 off 不发送强度、不产生或伪造 reasoning [OK]")
    print("\n=== F06 reasoning 双协议与持久化冒烟通过 ===")
finally:
    mock.shutdown()
