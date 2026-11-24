"""F43/F45：完成回答分支、历史边界与活动状态/文件修改隔离。"""
import asyncio
import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from app.db import SessionLocal, engine
from app.models import ToolCall
from app.routers.agent import _append_event

B = "http://localhost:8000/api/v1"
PORT = 9014


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def sse(cid, text, profile_id, token):
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": text, "provider_profile_id": profile_id,
                         "model": "mock-branch"}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


class BranchMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        user = request["messages"][-1]["content"]
        body = "".join([
            f"data: {json.dumps({'choices': [{'delta': {'content': f'answer:{user}', 'index': 0}}]})}\n\n",
            'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n',
        ]).encode()
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


async def add_cancelled(cid):
    async with SessionLocal() as db:
        await _append_event(db, cid, "message", {"role": "assistant", "content": "",
            "tool_calls": [{"id": "intermediate", "name": "list_files", "args": {}}]})
        await _append_event(db, cid, "message", {"role": "assistant", "content": "partial", "cancelled": True})
        await db.commit()
    await engine.dispose()


async def add_pending_goal_and_boundary(cid):
    async with SessionLocal() as db:
        args = {"path": "D-post/branch-proposal.txt", "content": "SHOULD_NOT_APPLY"}
        db.add(ToolCall(id=f"{cid}:branch-proposal", conversation_id=cid,
                        tool="propose_file_update", args=json.dumps(args), status="pending"))
        await _append_event(db, cid, "tool_state", {
            "tool_call_id": "branch-proposal", "tool": "propose_file_update",
            "args": args, "status": "awaiting_approval"})
        await _append_event(db, cid, "goal", {
            "goal_id": f"session-{cid}", "revision": 1, "objective": "source-only-goal",
            "acceptance_criteria": [], "budget": {"max_turns": 2, "max_tool_calls": 2},
            "status": "active", "stop_reason": None})
        await _append_event(db, cid, "message", {
            "role": "assistant", "content": "branch-safe-boundary"})
        await db.commit()
    await engine.dispose()


email = f"branch-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "branch-smoke", "password": "branch-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"branch-other-{time.time_ns()}@vibeflow.cn",
                                        "name": "branch-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"branch-ws-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"branch-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), BranchMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "branch-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-branch", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case["id"], "title": "branch source", "provider_profile_id": profile["id"],
        "model": "mock-branch"}, token=token)["id"]
    for text in ("A1", "A2", "A3"):
        sse(cid, text, profile["id"], token)
    detail = call("GET", f"/conversations/{cid}", token=token)
    source_count = len(detail["events"])
    assistants = [event for event in detail["events"] if event["type"] == "message" and event["payload"].get("role") == "assistant"]
    assert len(assistants) == 3 and all(event["id"] for event in assistants), assistants
    boundary = assistants[1]
    boundary_count = next(index for index, event in enumerate(detail["events"]) if event["id"] == boundary["id"]) + 1
    fork = call("POST", f"/conversations/{cid}/fork", {"head_event_id": boundary["id"]}, token=token)
    assert fork.get("id"), fork
    branch = call("GET", f"/conversations/{fork['id']}", token=token)
    assert len(branch["events"]) == boundary_count, branch["events"]
    assert branch["events"][-1]["id"] == boundary["id"], branch["events"][-1]
    assert branch["events"][-1]["payload"]["content"] == "answer:A2", branch["events"][-1]
    assert all("A3" not in event["payload"].get("content", "") for event in branch["events"])
    assert branch["pending_tools"] == []
    assert len(call("GET", f"/conversations/{cid}", token=token)["events"]) == source_count
    print(f"1. 第二个完成回答边界前 {boundary_count} 事件，原会话 {source_count} 事件不变 [OK]")

    branch_before = len(call("GET", f"/conversations/{fork['id']}", token=token)["events"])
    sse(fork["id"], "branch", profile["id"], token)
    assert len(call("GET", f"/conversations/{fork['id']}", token=token)["events"]) == branch_before + 7
    assert len(call("GET", f"/conversations/{cid}", token=token)["events"]) == source_count
    user_event = next(event for event in detail["events"] if event["payload"].get("role") == "user")
    assert call("POST", f"/conversations/{cid}/fork", {"head_event_id": user_event["id"]}, token=token).get("_err") == 409
    assert call("POST", f"/conversations/{cid}/fork", {"head_event_id": boundary["id"]}, token=other).get("_err") == 404
    print("2. 分支续聊独立，用户事件/跨用户边界拒绝 [OK]")

    asyncio.run(add_cancelled(cid))
    cancelled_detail = call("GET", f"/conversations/{cid}", token=token)
    cancelled = cancelled_detail["events"][-1]
    assert call("POST", f"/conversations/{cid}/fork", {"head_event_id": cancelled["id"]}, token=token).get("_err") == 409
    intermediate = cancelled_detail["events"][-2]
    assert call("POST", f"/conversations/{cid}/fork", {"head_event_id": intermediate["id"]}, token=token).get("_err") == 409
    conversations = call("GET", f"/conversations?caseId={case['id']}", token=token)
    assert len(conversations) == 2, conversations  # 被拒绝的分支不能留下半提交记录
    print("3. 取消回答和仅 tool_calls 的中间消息都不是完成分支点，拒绝分支零残留 [OK]")

    marker_path = "D-post/branch-proposal.txt"
    marker = "ORIGINAL_BRANCH_FILE"
    assert call("PUT", f"/cases/{case['id']}/file", {"path": marker_path, "content": marker}, token=token).get("ok") is True
    asyncio.run(add_pending_goal_and_boundary(cid))
    source = call("GET", f"/conversations/{cid}", token=token)
    safe_boundary = next(event for event in source["events"]
                         if event["payload"].get("content") == "branch-safe-boundary")
    isolated = call("POST", f"/conversations/{cid}/fork", {"head_event_id": safe_boundary["id"]}, token=token)
    isolated_detail = call("GET", f"/conversations/{isolated['id']}", token=token)
    assert isolated_detail["case_id"] == case["id"] and isolated_detail["pending_tools"] == [], isolated_detail
    assert isolated_detail["current_goal"] is None, isolated_detail
    assert any(event["type"] == "tool_state" and event["payload"].get("status") == "awaiting_approval"
               for event in isolated_detail["events"]), isolated_detail["events"]
    approve = call("POST", f"/conversations/{isolated['id']}/tool-calls/{cid}:branch-proposal:approve", {}, token=token)
    assert approve.get("_err") == 404, approve
    assert call("POST", f"/conversations/{isolated['id']}/goal:run", {}, token=token).get("_err") == 404
    preserved = call("GET", f"/cases/{case['id']}/file?path={marker_path}", token=token)
    assert preserved.get("content") == marker, preserved
    source_after = call("GET", f"/conversations/{cid}", token=token)
    assert len(source_after["pending_tools"]) == 1 and source_after["current_goal"]["status"] == "active", source_after
    assert call("DELETE", f"/cases/{case['id']}/file?path={marker_path}", token=token).get("ok") is True
    print("4. 历史审批只读、Goal/活动 ToolCall 不复制，源状态与共享工作目录文件不变 [OK]")
    print("\n=== 完成回答分支与状态/文件边界冒烟通过 ===")
finally:
    mock.shutdown()
