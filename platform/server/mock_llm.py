"""独立 mock LLM provider（供 E2E/演示/测试常驻运行）：
python mock_llm.py  — 监听 8990，同时讲 OpenAI /chat/completions 与 Anthropic /messages 两种 SSE 协议

工具测试场景：用户消息含「跑一下」→ 返回 run_shell_command 工具调用；
收到 tool 结果后的下一轮 → 返回包含工具输出的最终回答。
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8990


def _chunk_text(reply: str):
    """把回复切成多块 delta（模拟真实流式）"""
    return [reply[i:i + 4] for i in range(0, len(reply), 4)]


class MockLLM(BaseHTTPRequestHandler):
    def _sse(self, lines):
        body = "".join(f"data: {json.dumps(l)}\n\n" for l in lines).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _last_user(self, messages):
        text = ""
        tool_result = None
        for m in messages:
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):  # anthropic content blocks
                    for b in c:
                        if b.get("type") == "text":
                            text += b.get("text", "")
                        elif b.get("type") == "tool_result":
                            tool_result = b.get("content", "")
            elif m.get("role") == "tool":  # openai tool 消息
                tool_result = m.get("content", "")
        return text, tool_result

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        messages = req.get("messages", [])
        user_msg, tool_result = self._last_user(messages)

        if req.get("stream") is False:
            # 非流式（服务端内部调用：压缩/翻译）→ 一次性 JSON
            self._handle_nonstream(req, user_msg)
            return
        if self.path == "/v1/chat/completions":
            self._handle_openai(req, user_msg, tool_result)
        elif self.path == "/v1/messages":
            self._handle_anthropic(req, user_msg, tool_result)
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_nonstream(self, req, user_msg):
        reply = f"[mock-nonstream] 处理结果：{user_msg[:80]}"
        if self.path == "/v1/chat/completions":
            body = {"id": "chatcmpl-mock", "object": "chat.completion",
                    "model": req.get("model", "mock-model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": reply}}]}
        elif self.path == "/v1/messages":
            body = {"id": "msg_mock", "type": "message", "role": "assistant",
                    "model": req.get("model", "mock-model"),
                    "content": [{"type": "text", "text": reply}],
                    "stop_reason": "end_turn"}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- OpenAI ----------
    def _handle_openai(self, req, user_msg, tool_result):
        has_tools = bool(req.get("tools"))
        if tool_result is not None:
            reply = f"[mock] 工具已执行，输出：{tool_result.strip()[:100]}"
            self._sse([
                *[{"choices": [{"delta": {"content": ch}, "index": 0}]} for ch in _chunk_text(reply)],
                {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]},
            ])
        elif "跑一下" in user_msg and has_tools:
            self._sse([
                {"choices": [{"delta": {"role": "assistant", "content": "我来跑一下这个命令。"}, "index": 0}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_mock1",
                    "function": {"name": "run_shell_command", "arguments": ""}}]}, "index": 0}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0,
                    "function": {"arguments": json.dumps({"command": "echo MOCK_TOOL_OK"})}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]},
            ])
        elif "列出文件" in user_msg and has_tools:
            # 安全工具路径：list_files 免审批
            self._sse([
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_mock2",
                    "function": {"name": "list_files",
                    "arguments": json.dumps({"subdir": ""})}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]},
            ])
        elif "写一下" in user_msg and has_tools:
            # 提案工具路径：propose_file_update（危险→审批卡→diff 预览）
            self._sse([
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_mock3",
                    "function": {"name": "propose_file_update",
                    "arguments": json.dumps({"path": "0-caseDict/memory.md",
                        "content": "# Case Memory\n\n- 测试经验：MOCK 提案写入。\n",
                        "reason": "记录经验（diff 卡测试）"}, ensure_ascii=False)}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]},
            ])
        else:
            reply = f"[mock] 收到：{user_msg[:50]}。这是一个确定性测试回答。"
            self._sse([
                *[{"choices": [{"delta": {"content": ch}, "index": 0}]} for ch in _chunk_text(reply)],
                {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]},
            ])

    # ---------- Anthropic ----------
    def _handle_anthropic(self, req, user_msg, tool_result):
        has_tools = bool(req.get("tools"))
        if tool_result is not None:
            reply = f"[mock] 工具已执行，输出：{tool_result.strip()[:100]}"
            self._sse([
                {"type": "message_start", "message": {"id": "msg_mock"}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                *[{"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": ch}} for ch in _chunk_text(reply)],
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ])
        elif "跑一下" in user_msg and has_tools:
            self._sse([
                {"type": "message_start", "message": {"id": "msg_mock"}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "我来跑一下这个命令。"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "content_block_start", "index": 1,
                 "content_block": {"type": "tool_use", "id": "toolu_mock1",
                                   "name": "run_shell_command", "input": {}}},
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta",
                           "partial_json": json.dumps({"command": "echo MOCK_TOOL_OK"})}},
                {"type": "content_block_stop", "index": 1},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ])
        elif "列出文件" in user_msg and has_tools:
            # 安全工具路径：list_files 免审批
            self._sse([
                {"type": "message_start", "message": {"id": "msg_mock"}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "tool_use", "id": "toolu_mock2",
                                   "name": "list_files", "input": {}}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "input_json_delta",
                           "partial_json": json.dumps({"subdir": ""})}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ])
        elif "写一下" in user_msg and has_tools:
            # 提案工具路径（Anthropic 版）
            self._sse([
                {"type": "message_start", "message": {"id": "msg_mock"}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "tool_use", "id": "toolu_mock3",
                                   "name": "propose_file_update", "input": {}}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "input_json_delta",
                           "partial_json": json.dumps({"path": "0-caseDict/memory.md",
                               "content": "# Case Memory\n\n- 测试经验：MOCK 提案写入。\n",
                               "reason": "记录经验（diff 卡测试）"}, ensure_ascii=False)}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ])
        else:
            reply = f"[mock] 收到：{user_msg[:50]}。这是一个确定性测试回答。"
            self._sse([
                {"type": "message_start", "message": {"id": "msg_mock"}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                *[{"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": ch}} for ch in _chunk_text(reply)],
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ])

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"mock LLM provider on http://127.0.0.1:{PORT} (Ctrl+C 退出)")
    ThreadingHTTPServer(("127.0.0.1", PORT), MockLLM).serve_forever()
