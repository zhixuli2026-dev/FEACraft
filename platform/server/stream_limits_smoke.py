"""F34：用小阈值验证双协议流上限和错误脱敏，避免大型测试夹具。"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services import llm
from app.services.redaction import redact_text

PORT = 9011


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        model = request["model"]
        if model == "redact":
            body = b'Bearer abc123 api_key=sk-test123456 token: "tok_secret" ' + b'x' * 200
            self.send_response(401); self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if self.path == "/v1/chat/completions":
            if model == "tool":
                events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call",
                    "function": {"name": "list_files", "arguments": "x" * 80}}]}, "index": 0}]}]
            else:
                count = 8 if model == "stream" else 1
                events = []
                for _ in range(count):
                    delta = {"content": "y" * 40}
                    if model == "text":
                        delta["reasoning_content"] = "r" * 40
                    events.append({"choices": [{"delta": delta, "index": 0}]})
        else:
            if model == "tool":
                events = [{"type": "content_block_start", "index": 0,
                           "content_block": {"type": "tool_use", "id": "call", "name": "list_files", "input": {}}},
                          {"type": "content_block_delta", "index": 0,
                           "delta": {"type": "input_json_delta", "partial_json": "x" * 80}}]
            else:
                events = [{"type": "content_block_delta", "index": 0,
                           "delta": {"type": "thinking_delta", "thinking": "r" * 40}},
                          {"type": "content_block_delta", "index": 1,
                           "delta": {"type": "text_delta", "text": "y" * 40}}]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


async def collect(model, protocol="openai"):
    endpoint = f"http://127.0.0.1:{PORT}/v1/chat/completions" if protocol == "openai" else f"http://127.0.0.1:{PORT}/v1/messages"
    function = llm.stream_chat_openai if protocol == "openai" else llm.stream_chat_anthropic
    return [event async for event in function(endpoint=endpoint, api_key="mock-key", model=model,
                                              messages=[], tools=None, reasoning_effort="medium")]


async def main():
    original = {name: getattr(llm, name) for name in (
        "MAX_MODEL_STREAM_BYTES", "MAX_TEXT_REASONING_BYTES", "MAX_SSE_EVENT_BYTES",
        "MAX_TOOL_CALL_BYTES", "ERROR_PREVIEW_BYTES")}
    try:
        llm.MAX_SSE_EVENT_BYTES = 16
        events = await collect("sse")
        assert events[-1]["type"] == "stream.error" and "SSE event" in events[-1]["message"], events
        print("1. 单 SSE event 上限 [OK]")

        llm.MAX_SSE_EVENT_BYTES = original["MAX_SSE_EVENT_BYTES"]
        llm.MAX_TOOL_CALL_BYTES = 16
        for protocol in ("openai", "anthropic"):
            events = await collect("tool", protocol)
            assert events[-1]["type"] == "stream.error" and "tool call" in events[-1]["message"], (protocol, events)
        print("2. 双协议单 tool call 上限 [OK]")

        llm.MAX_TOOL_CALL_BYTES = original["MAX_TOOL_CALL_BYTES"]
        llm.MAX_TEXT_REASONING_BYTES = 32
        for protocol in ("openai", "anthropic"):
            events = await collect("text", protocol)
            assert events[-1]["type"] == "stream.error" and "reasoning" in events[-1]["message"], (protocol, events)
        print("3. 双协议正文+reasoning 合计上限 [OK]")

        llm.MAX_TEXT_REASONING_BYTES = original["MAX_TEXT_REASONING_BYTES"]
        llm.MAX_MODEL_STREAM_BYTES = 64
        events = await collect("stream")
        assert events[-1]["type"] == "stream.error" and "64 MiB" in events[-1]["message"], events
        print("4. 模型流传输总量上限 [OK]")

        llm.MAX_MODEL_STREAM_BYTES = original["MAX_MODEL_STREAM_BYTES"]
        llm.ERROR_PREVIEW_BYTES = 32
        events = await collect("redact")
        detail = events[-1]["detail"]
        assert len(detail.encode("utf-8")) <= 32, detail
        for secret in ("abc123", "sk-test123456", "tok_secret"):
            assert secret not in detail, detail
        print("5. 错误正文有界且 Bearer/API key/token 脱敏 [OK]")
    finally:
        for name, value in original.items():
            setattr(llm, name, value)


server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    text = redact_text('Bearer abc123 secret="sec_value" sk-abcd1234', ("explicit",), limit=200)
    assert text == 'Bearer *** secret=*** ***', text
    assert llm.MAX_RESPONSE_BODY_BYTES == 10 * 1024 * 1024
    asyncio.run(main())
    print("\n=== 模型流/事件/tool call/错误预览上限冒烟通过 ===")
finally:
    server.shutdown()
