"""F20：web_search provider adapter、runId 隔离、共享预算与取消；默认 mock 不联网。"""
import asyncio
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services.websearch import MockSearchProvider, WebSearchManager


async def main():
    manager = WebSearchManager(provider=MockSearchProvider(delay=0.01), budget=2)
    cancel = asyncio.Event()
    first = await manager.execute("run-a", 101, " cavity ", cancel, max_results=2)
    second = await manager.execute("run-a", 101, "mesh quality", cancel, max_results=2)
    assert first["ok"] and second["ok"], (first, second)
    assert first["run_id"] == second["run_id"] == "run-a"
    assert "provider=mock" in first["result"] and "非实时联网" in first["result"], first
    assert first["result"].count("https://example.invalid/") == 2, first["result"]
    snapshot = manager.snapshot("run-a")
    assert snapshot["queries"] == 2 and snapshot["remaining"] == 0, snapshot
    exhausted = await manager.execute("run-a", 101, "over budget", cancel)
    assert not exhausted["ok"] and "共享搜索预算" in exhausted["result"], exhausted
    print("1. 同一 runId 共享预算且超限 fail closed [OK]")

    other = await manager.execute("run-b", 202, "independent", asyncio.Event())
    assert other["run_id"] == "run-b" and manager.snapshot("run-b")["remaining"] == 1
    assert manager.snapshot("run-a")["conversation_id"] == 101
    assert manager.snapshot("run-b")["conversation_id"] == 202
    print("2. 不同 runId/会话隔离 [OK]")

    slow = WebSearchManager(provider=MockSearchProvider(delay=10), budget=1)
    slow_cancel = asyncio.Event()
    started = time.monotonic()
    pending = asyncio.create_task(slow.execute("run-cancel", 303, "slow query", slow_cancel))
    await asyncio.sleep(0.03)
    slow_cancel.set()
    cancelled = await asyncio.wait_for(pending, timeout=1)
    assert not cancelled["ok"] and cancelled["status"] == "cancelled", cancelled
    assert time.monotonic() - started < 1
    assert slow.snapshot("run-cancel")["status"] == "cancelled"
    print("3. 后台 provider 可被取消且不等待慢任务 [OK]")

    invalid = await manager.execute("run-invalid", 404, "", asyncio.Event())
    assert not invalid["ok"] and "query" in invalid["result"], invalid
    print("4. 空查询拒绝 [OK]")
    manager.finish("run-a")
    assert manager.snapshot("run-a") is None
    print("\n=== web_search adapter 直接测试通过（未联网）===")


asyncio.run(main())


B = "http://localhost:8000/api/v1"
PORT = 9005


class AgentSearchMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if request["messages"][-1]["role"] == "tool":
            events = [{"choices": [{"delta": {"content": "SEARCH_DONE"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        else:
            assert any(tool["function"]["name"] == "web_search" for tool in request.get("tools", []))
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_search",
                "function": {"name": "web_search", "arguments": json.dumps({"query": "cavity", "max_results": 2})}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


def sse_collect(cid, body, token):
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("data:"):
                events.append(json.loads(text[5:].strip()))
    return events


mock = ThreadingHTTPServer(("127.0.0.1", PORT), AgentSearchMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    email = f"web-search-{time.time_ns()}@vibeflow.cn"
    token = call("POST", "/auth/register", {"email": email, "name": "web-search-smoke", "password": "web-search-pass-123"})["token"]
    workspace = call("POST", "/workspaces", {"name": f"web-search-ws-{time.time_ns()}"}, token=token)
    case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"web-search-{time.time_ns()}"}, token=token)
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "web-search-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-search", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case["id"], "title": "web search", "provider_profile_id": profile["id"],
        "model": "mock-search"}, token=token)["id"]
    events = sse_collect(cid, {"content": "search", "provider_profile_id": profile["id"],
                               "model": "mock-search"}, token)
    states = [event for event in events if event["type"] == "tool.state"]
    assert [event["status"] for event in states[:2]] == ["queued", "running"], states
    result = next(event for event in events if event["type"] == "tool.result")
    assert result["ok"] and result["status"] == "completed" and result["run_id"], result
    assert "provider=mock" in result["result"] and "非实时联网" in result["result"], result
    answer = "".join(event.get("text", "") for event in events if event["type"] == "text.delta")
    assert "SEARCH_DONE" in answer, events
    detail = call("GET", f"/conversations/{cid}", token=token)
    persisted = next(event["payload"] for event in detail["events"] if event["type"] == "tool_result")
    assert persisted["run_id"] == result["run_id"], (persisted, result)
    assert any(row["status"] == "executed" for row in detail["tool_calls"]), detail["tool_calls"]
    print("5. Agent web_search 工具、runId 持久化与真实续跑 [OK]")
    print("\n=== web_search adapter/runId/预算/取消/Agent 集成冒烟通过（未联网）===")
finally:
    mock.shutdown()

