"""F23：update_goal 校验、Session 目标事件、revision 与下一回合恢复。"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services.tools import ToolExecutor

B = "http://localhost:8000/api/v1"
PORT = 9007


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


def sse_collect(cid, text, profile_id, token):
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": text, "provider_profile_id": profile_id,
                         "model": "mock-goal"}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


email = f"goal-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "goal-smoke", "password": "goal-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"goal-ws-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"goal-{time.time_ns()}"}, token=token)
executor = ToolExecutor(case.get("s3_prefix") or f"workspaces/{workspace['id']}/cases/{case['id']}")
valid = executor.execute("update_goal", {"objective": "direct goal", "acceptance_criteria": ["done"],
                                         "budget": {"max_turns": 2, "max_tool_calls": 4}})
assert valid["ok"] and "direct goal" in valid["result"] and "max_turns=2" in valid["result"], valid
for args in ({"objective": "bad status", "status": "running"},
             {"objective": "missing reason", "status": "completed"},
             {"objective": "bad budget", "budget": {"max_turns": 0}}):
    rejected = executor.execute("update_goal", args)
    assert not rejected["ok"], (args, rejected)
print("1. update_goal 合法结构、状态/停止原因/预算校验 [OK]")


class GoalMock(BaseHTTPRequestHandler):
    systems = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if request["messages"][-1]["role"] == "tool":
            events = [{"choices": [{"delta": {"content": "GOAL_DONE"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        else:
            type(self).systems.append(request["messages"][0]["content"])
            second = request["messages"][-1]["content"] == "second"
            args = ({"objective": "second goal", "status": "completed", "stop_reason": "验收完成",
                     "budget": {"max_turns": 4, "max_tool_calls": 8}} if second else
                    {"objective": "first goal", "acceptance_criteria": ["residual < 1e-6"],
                     "budget": {"max_turns": 3, "max_tool_calls": 7}})
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_goal_second" if second else "call_goal",
                "function": {"name": "update_goal", "arguments": json.dumps(args, ensure_ascii=False)}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        body = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


mock = ThreadingHTTPServer(("127.0.0.1", PORT), GoalMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "goal-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-goal", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case["id"], "title": "goal", "provider_profile_id": profile["id"],
        "model": "mock-goal"}, token=token)["id"]
    first = sse_collect(cid, "first", profile["id"], token)
    updated = next(event for event in first if event["type"] == "goal.updated")
    assert updated["goal"]["revision"] == 1 and updated["goal"]["status"] == "active", updated
    detail = call("GET", f"/conversations/{cid}", token=token)
    goal = detail["current_goal"]
    assert goal["objective"] == "first goal" and goal["acceptance_criteria"] == ["residual < 1e-6"], goal
    assert goal["budget"] == {"max_turns": 3, "max_tool_calls": 7}, goal
    assert len([event for event in detail["events"] if event["type"] == "goal"]) == 1
    print("2. goal.updated/current_goal/预算/验收条件/首 revision 持久化 [OK]")

    second = sse_collect(cid, "second", profile["id"], token)
    assert next(event for event in second if event["type"] == "goal.updated")["goal"]["revision"] == 2
    detail = call("GET", f"/conversations/{cid}", token=token)
    assert detail["current_goal"]["status"] == "completed", detail["current_goal"]
    assert detail["current_goal"]["stop_reason"] == "验收完成", detail["current_goal"]
    assert len([event for event in detail["events"] if event["type"] == "goal"]) == 2
    assert any("first goal" in system and "max_turns=3" in system for system in GoalMock.systems[1:]), GoalMock.systems
    print("3. 第二 revision、终态原因和下一回合系统提示恢复 [OK]")
    print("\n=== update_goal/Session 目标持久化冒烟通过（无自动循环）===")
finally:
    mock.shutdown()
