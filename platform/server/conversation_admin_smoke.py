"""F46/F47：会话管理约束、记录清理与旧 Codex 缓存保留。"""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.db import SessionLocal, engine
from app.models import AgentEvent, Conversation, ToolCall
from sqlalchemy import func, select

B = "http://localhost:8000/api/v1"
PORT = 9013


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def sse(cid, text, profile_id, token, events_out=None):
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": text, "provider_profile_id": profile_id,
                         "model": "mock-admin"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    if events_out is not None:
        events_out.extend(events)
    return events


class AdminMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        slow = request["messages"][-1].get("content") == "slow"
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        try:
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"answer"},"index":0}]}\n\n')
            self.wfile.flush()
            if slow:
                time.sleep(5)
            self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n')
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args):
        pass


async def set_pending(cid, status):
    async with SessionLocal() as db:
        if status == "pending":
            db.add(ToolCall(id=f"{cid}:pending", conversation_id=cid, tool="list_files",
                            args="{}", status="pending"))
        else:
            row = await db.get(ToolCall, f"{cid}:pending")
            row.status = status
        await db.commit()
    await engine.dispose()


async def remaining(cid):
    async with SessionLocal() as db:
        conv = await db.get(Conversation, cid)
        events = await db.scalar(select(func.count(AgentEvent.id)).where(AgentEvent.conversation_id == cid))
        tools = await db.scalar(select(func.count(ToolCall.id)).where(ToolCall.conversation_id == cid))
    await engine.dispose()
    return conv, events, tools


email = f"conversation-admin-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "conversation-admin", "password": "admin-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"conversation-other-{time.time_ns()}@vibeflow.cn",
                                        "name": "conversation-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"conversation-admin-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"conversation-admin-{time.time_ns()}"}, token=token)
legacy_path = ".vibeflow/codex-sessions/legacy.json"
legacy_content = '{"format":"legacy-codex","keep":true}'
legacy_query = urllib.parse.quote(legacy_path, safe="")
assert call("PUT", f"/cases/{case['id']}/file", {"path": legacy_path, "content": legacy_content}, token=token).get("ok") is True


def assert_legacy_preserved():
    legacy = call("GET", f"/cases/{case['id']}/file?path={legacy_query}", token=token)
    assert legacy.get("content") == legacy_content, legacy


mock = ThreadingHTTPServer(("127.0.0.1", PORT), AdminMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "conversation-admin-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-admin", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {"case_id": case["id"], "title": "before",
                                          "provider_profile_id": profile["id"], "model": "mock-admin"}, token=token)["id"]
    renamed = call("PATCH", f"/conversations/{cid}", {"title": "after"}, token=token)
    assert renamed["title"] == "after", renamed
    assert call("PATCH", f"/conversations/{cid}", {"title": " "}, token=token).get("_err") == 422
    assert call("PATCH", f"/conversations/{cid}", {"title": "foreign"}, token=other).get("_err") == 404
    assert call("DELETE", f"/conversations/{cid}", token=other).get("_err") == 404
    assert call("GET", f"/conversations/{cid}", token=token)["title"] == "after"
    assert_legacy_preserved()
    print("1. 重命名、空标题和跨用户边界 [OK]")

    sse(cid, "first", profile["id"], token)
    fork = call("POST", f"/conversations/{cid}/fork", {}, token=token)
    assert call("DELETE", f"/conversations/{cid}", token=token).get("_err") == 409
    assert call("DELETE", f"/conversations/{fork['id']}", token=token).get("ok") is True
    print("2. 有 fork 时源会话保护，分支可先删 [OK]")

    events = []
    thread = threading.Thread(target=lambda: sse(cid, "slow", profile["id"], token, events))
    thread.start()
    time.sleep(0.2)
    assert call("DELETE", f"/conversations/{cid}", token=token).get("_err") == 409
    assert call("POST", f"/conversations/{cid}/messages:cancel", {}, token=token).get("ok") is True
    thread.join(3)
    assert not thread.is_alive(), "取消后慢回合未结束"
    assert call("DELETE", f"/conversations/{cid}", token=token).get("ok") is True
    assert call("GET", f"/conversations/{cid}", token=token).get("_err") == 404
    print("3. 活动回合禁删，取消结束后可删 [OK]")

    pending = call("POST", "/conversations", {"case_id": case["id"], "title": "pending",
                                              "provider_profile_id": profile["id"], "model": "mock-admin"}, token=token)["id"]
    asyncio.run(set_pending(pending, "pending"))
    assert call("DELETE", f"/conversations/{pending}", token=token).get("_err") == 409
    asyncio.run(set_pending(pending, "rejected"))
    assert call("DELETE", f"/conversations/{pending}", token=token).get("ok") is True
    conv, event_count, tool_count = asyncio.run(remaining(pending))
    assert conv is None and event_count == 0 and tool_count == 0, (conv, event_count, tool_count)
    print("4. 待审批禁删，终态后删除事件/工具记录 [OK]")

    assert_legacy_preserved()
    assert call("DELETE", f"/cases/{case['id']}/file?path={legacy_query}", token=token).get("ok") is True
    print("5. 会话重命名/删除不迁移、不修改旧 codex-sessions；测试缓存已清理 [OK]")
    print("\n=== 会话重命名/删除约束与旧 Codex 缓存保留冒烟通过 ===")
finally:
    mock.shutdown()
