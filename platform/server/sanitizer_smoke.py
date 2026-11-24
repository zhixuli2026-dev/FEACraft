"""F33：正文/reasoning 伪协议增量清洗、持久化/重放与 Ask 结构化工具拒绝。"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services.sanitizer import ProtocolSanitizer, sanitize_text

B = "http://localhost:8000/api/v1"
PORT = 9009


def filtered(chunks):
    sanitizer = ProtocolSanitizer()
    return "".join(sanitizer.feed(chunk) for chunk in chunks) + sanitizer.finish()


assert filtered(list("hello <tool_call>{'x':1}</tool_call> world")) == "hello  world"
assert filtered(["A<tool_call/>B<update_plan />C"]) == "ABC"
assert filtered(["A<|dsml|>unfinished", " sentinel"]) == "A"
assert filtered(["<note>ok</note><tool_callish>keep</tool_callish>"]) == "<note>ok</note><tool_callish>keep</tool_callish>"
assert filtered(["x<function_call>bad</function_call>y<tool_call>bad2</tool_call>z"]) == "xyz"
assert sanitize_text("reason<tool_call>secret</tool_call> visible") == "reason visible"
print("1. 逐字符/自闭合/无结束符/重复块清洗且普通相似标签保留 [OK]")


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


def sse(cid, text, profile_id, token):
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": text, "provider_profile_id": profile_id,
                         "model": "mock-sanitizer", "reasoning_effort": "medium"}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


class SanitizerMock(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append(request)
        last = request["messages"][-1]
        user = last.get("content", "")
        if self.path == "/v1/chat/completions":
            if user == "ask":
                events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "ask_tool",
                    "function": {"name": "list_files", "arguments": "{}"}}]}, "index": 0}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
            elif user == "second":
                events = [{"choices": [{"delta": {"content": "SECOND"}, "index": 0}]},
                          {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
            else:
                events = [
                    {"choices": [{"delta": {"reasoning_content": "<tool_"}, "index": 0}]},
                    {"choices": [{"delta": {"reasoning_content": "call>secret</tool_call>visible reasoning"}, "index": 0}]},
                    {"choices": [{"delta": {"content": "Answer <tool_"}, "index": 0}]},
                    {"choices": [{"delta": {"content": "call>{\"name\":\"run_shell_command\"}"}, "index": 0}]},
                    {"choices": [{"delta": {"content": "</tool_call> OK<|dsml|>UNFINISHED"}, "index": 0}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]},
                ]
        elif self.path == "/v1/messages":
            if user == "ask":
                events = [
                    {"type": "message_start", "message": {"id": "ask"}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "ask_tool", "name": "list_files", "input": {}}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                    {"type": "message_stop"},
                ]
            elif user == "second":
                events = [
                    {"type": "message_start", "message": {"id": "second"}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "SECOND"}},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                    {"type": "message_stop"},
                ]
            else:
                events = [
                    {"type": "message_start", "message": {"id": "sanitizer"}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "<tool_"}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "call>secret</tool_call>visible reasoning"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Answer <tool_"}},
                    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "call>{\"name\":\"run_shell_command\"}"}},
                    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "</tool_call> OK<|dsml|>UNFINISHED"}},
                    {"type": "content_block_stop", "index": 1},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                    {"type": "message_stop"},
                ]
        else:
            self.send_response(404); self.end_headers(); return
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


email = f"sanitizer-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "sanitizer-smoke", "password": "sanitizer-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"sanitizer-ws-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"sanitizer-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), SanitizerMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    for provider in ("custom", "anthropic"):
        profile = call("POST", "/provider-profiles", {
            "provider": provider, "name": f"sanitizer-{provider}", "base_url": f"http://127.0.0.1:{PORT}",
            "default_model": "mock-sanitizer", "api_key": "mock-key"}, token=token)
        cid = call("POST", "/conversations", {
            "case_id": case["id"], "title": f"sanitizer-{provider}", "provider_profile_id": profile["id"],
            "model": "mock-sanitizer"}, token=token)["id"]
        first = sse(cid, "first", profile["id"], token)
        streamed = "".join(event.get("text", "") for event in first if event["type"] == "text.delta")
        reasoning = "".join(event.get("text", "") for event in first if event["type"] == "reasoning.delta")
        assert "Answer" in streamed and "OK" in streamed and "tool_call" not in streamed and "dsml" not in streamed, streamed
        assert reasoning == "visible reasoning", reasoning
        detail = call("GET", f"/conversations/{cid}", token=token)
        assistant = next(event["payload"] for event in detail["events"] if event["type"] == "message" and event["payload"].get("role") == "assistant")
        assert "secret" not in assistant["reasoning"] and "tool_call" not in assistant["content"], assistant
        assert "dsml" not in assistant["content"] and "UNFINISHED" not in assistant["content"], assistant
        print(f"2. {provider} 流式正文/reasoning 与落库清洗 [OK]")

        sse(cid, "second", profile["id"], token)
        resent = json.dumps(SanitizerMock.seen[-1], ensure_ascii=False)
        assert "secret" not in resent and "UNFINISHED" not in resent, resent[-1000:]
        print(f"3. {provider} 历史重放/再次发送前清洗 [OK]")

        ask = call("POST", "/conversations", {
            "case_id": case["id"], "title": f"ask-{provider}", "provider_profile_id": profile["id"],
            "model": "mock-sanitizer", "permission_mode": "ask"}, token=token)["id"]
        rejected = sse(ask, "ask", profile["id"], token)
        assert any(event["type"] == "stream.error" and "Ask" in event.get("message", "") for event in rejected), rejected
        assert not any(event["type"] in ("tool.result", "tool.state") for event in rejected), rejected
        detail = call("GET", f"/conversations/{ask}", token=token)
        assert detail["pending_tools"] == [] and detail["tool_calls"] == [], detail
        print(f"4. {provider} Ask 结构化 tool_calls 拒绝执行且无工具卡 [OK]")
    print("\n=== 伪协议/sentinel/重放/Ask 清洗冒烟通过 ===")
finally:
    mock.shutdown()
