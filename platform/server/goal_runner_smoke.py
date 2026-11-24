"""F50：显式 Goal 自动续轮、完成/预算/取消停止条件。"""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.db import SessionLocal, engine
from app.routers.agent import _append_goal_revision
from app.services.tools import goal_payload

B = "http://localhost:8000/api/v1"
PORT = 9017
LOOP = asyncio.new_event_loop()


def run_async(awaitable):
    return LOOP.run_until_complete(awaitable)


def seed_goal(cid, objective, max_turns, max_tools=10):
    async def run():
        async with SessionLocal() as db:
            goal = goal_payload({"objective": objective, "acceptance_criteria": ["mock accepted"],
                                 "budget": {"max_turns": max_turns, "max_tool_calls": max_tools}})
            await _append_goal_revision(db, cid, goal)
            await db.commit()
    run_async(run())


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


def goal_run(cid, token, events_out=None):
    request = urllib.request.Request(f"{B}/conversations/{cid}/goal:run", method="POST", data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    if events_out is not None:
        events_out.extend(events)
    return events


class GoalRunnerMock(BaseHTTPRequestHandler):
    finish_calls = 0
    received = threading.Event()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        messages = request["messages"]
        user = messages[-1].get("content", "")
        if "cancel" in user:
            type(self).received.set()
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            try:
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"working"},"index":0}]}\n\n'); self.wfile.flush()
                time.sleep(5)
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n')
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        if messages[-1]["role"] == "tool":
            events = [{"choices": [{"delta": {"content": "GOAL_FINISHED"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        elif "finish" in user and type(self).finish_calls >= 1:
            args = {"objective": "finish", "status": "completed", "stop_reason": "mock accepted",
                    "budget": {"max_turns": 3, "max_tool_calls": 10}}
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "goal_finish_tool",
                "function": {"name": "update_goal", "arguments": json.dumps(args)}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        else:
            events = [{"choices": [{"delta": {"content": "WORKING"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        if "finish" in user:
            type(self).finish_calls += 1
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


email = f"goal-runner-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "goal-runner", "password": "goal-runner-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"goal-runner-other-{time.time_ns()}@vibeflow.cn",
                                        "name": "goal-runner-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"goal-runner-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"goal-runner-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), GoalRunnerMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "goal-runner-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-goal-runner", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {"case_id": case["id"], "title": "finish",
                                          "provider_profile_id": profile["id"], "model": "mock-goal-runner"}, token=token)["id"]
    seed_goal(cid, "finish", 3)
    events = goal_run(cid, token)
    assert GoalRunnerMock.finish_calls == 3, (GoalRunnerMock.finish_calls, events)
    assert [event["turn"] for event in events if event["type"] == "goal.progress"] == [1, 2], events
    detail = call("GET", f"/conversations/{cid}", token=token)
    assert detail["current_goal"]["status"] == "completed" and detail["current_goal"]["revision"] == 2, detail["current_goal"]
    states = [event for event in detail["events"] if event["type"] == "run_state" and event["payload"]["status"] == "completed"]
    assert len(states) == 2, states
    assert call("POST", f"/conversations/{cid}/goal:run", {}, token=token).get("_err") == 409
    assert call("POST", f"/conversations/{cid}/goal:run", {}, token=other).get("_err") == 404
    print("1. 两轮自动续跑到模型验收完成，第三轮不执行 [OK]")

    exhausted = call("POST", "/conversations", {"case_id": case["id"], "title": "exhausted",
                                                "provider_profile_id": profile["id"], "model": "mock-goal-runner"}, token=token)["id"]
    seed_goal(exhausted, "exhaust", 1)
    goal_run(exhausted, token)
    goal = call("GET", f"/conversations/{exhausted}", token=token)["current_goal"]
    assert goal["status"] == "blocked" and "轮次预算 1" in goal["stop_reason"], goal
    print("2. 单轮预算耗尽 blocked 且不伪造完成 [OK]")

    cancelled = call("POST", "/conversations", {"case_id": case["id"], "title": "cancel",
                                                "provider_profile_id": profile["id"], "model": "mock-goal-runner"}, token=token)["id"]
    seed_goal(cancelled, "cancel", 5)
    events = []
    thread = threading.Thread(target=lambda: goal_run(cancelled, token, events))
    thread.start()
    assert GoalRunnerMock.received.wait(3), "慢 Goal 未启动"
    assert call("POST", f"/conversations/{cancelled}/messages:cancel", {}, token=token).get("ok") is True
    thread.join(3)
    assert not thread.is_alive(), "取消后 Goal runner 未停止"
    goal = call("GET", f"/conversations/{cancelled}", token=token)["current_goal"]
    assert goal["status"] == "cancelled", goal
    print("3. 用户取消同时停止当前回合和后续续轮 [OK]")
    print("\n=== Goal 自动续轮/预算/取消冒烟通过 ===")
finally:
    mock.shutdown()
    run_async(engine.dispose())
    LOOP.close()
