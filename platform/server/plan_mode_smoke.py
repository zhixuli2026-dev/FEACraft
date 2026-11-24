"""F51：Plan 只读模式、危险调用拒绝与批准后恢复。"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
PORT = 0


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


def sse(cid, text, profile_id, token, path="messages"):
    request = urllib.request.Request(f"{B}/conversations/{cid}/{path}", method="POST",
        data=json.dumps({"content": text, "provider_profile_id": profile_id,
                         "model": "mock-plan"}, ensure_ascii=False).encode() if path == "messages" else b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


class PlanMock(BaseHTTPRequestHandler):
    tools_seen = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).tools_seen.append([tool["function"]["name"] for tool in request.get("tools", [])])
        if request["messages"][-1]["role"] == "tool":
            events = [{"choices": [{"delta": {"content": "PLAN_DONE"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        elif request["messages"][-1]["content"] == "safe":
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "plan_safe",
                "function": {"name": "list_files", "arguments": "{}"}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        elif request["messages"][-1]["content"] == "memory":
            args = json.dumps({"action": "append", "content": "PLAN_MEMORY_FORBIDDEN"})
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "plan_memory",
                "function": {"name": "update_memory", "arguments": args}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        else:
            args = {"path": "D-post/plan-forbidden.txt", "content": "PLAN_APPROVED", "reason": "plan approval"}
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "plan_write",
                "function": {"name": "propose_file_update", "arguments": json.dumps(args)}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


email = f"plan-mode-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "plan-mode", "password": "plan-mode-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"plan-mode-other-{time.time_ns()}@vibeflow.cn",
                                        "name": "plan-mode-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"plan-mode-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"plan-mode-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), PlanMock)
PORT = mock.server_port
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "plan-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-plan", "api_key": "mock-key"}, token=token)
    created = call("POST", "/conversations", {"case_id": case["id"], "title": "plan",
                                               "provider_profile_id": profile["id"], "model": "mock-plan",
                                               "permission_mode": "plan"}, token=token)
    assert created.get("id"), created
    cid = created["id"]
    safe = sse(cid, "safe", profile["id"], token)
    assert any(event["type"] == "tool.result" and event["tool"] == "list_files" and event["ok"] for event in safe), safe
    assert all("run_shell_command" not in tools and "propose_file_update" not in tools and
               "update_memory" not in tools for tools in PlanMock.tools_seen), PlanMock.tools_seen
    print("1. Plan 只读工具可执行，危险工具和记忆写入不下发 [OK]")

    forbidden = sse(cid, "write", profile["id"], token)
    assert any(event["type"] == "stream.error" and "Plan" in event.get("message", "") for event in forbidden), forbidden
    assert not any(event["type"] in ("tool.result", "tool.state") for event in forbidden), forbidden
    missing = call("GET", f"/cases/{case['id']}/file?path=D-post%2Fplan-forbidden.txt", token=token)
    assert missing.get("_err") == 404, missing
    print("2. provider 违规危险提案也拒绝且零副作用 [OK]")

    memory_forbidden = sse(cid, "memory", profile["id"], token)
    assert any(event["type"] == "stream.error" and "Plan" in event.get("message", "")
               for event in memory_forbidden), memory_forbidden
    memory = call("GET", f"/cases/{case['id']}/file?path=0-caseDict%2Fmemory.md", token=token)
    assert "PLAN_MEMORY_FORBIDDEN" not in memory.get("content", ""), memory
    print("2b. provider 违规 update_memory 同样拒绝且不改长期记忆 [OK]")

    assert call("PATCH", f"/conversations/{cid}/permission", {"permission_mode": "approve_writes"}, token=other).get("_err") == 404
    assert call("PATCH", f"/conversations/{cid}/permission", {"permission_mode": "invalid"}, token=token).get("_err") == 422
    approved = call("PATCH", f"/conversations/{cid}/permission", {"permission_mode": "approve_writes"}, token=token)
    assert approved["permission_mode"] == "approve_writes", approved
    pending = sse(cid, "write", profile["id"], token)
    tool = next(event["tool_call"] for event in pending if event["type"] == "approval.requested")
    executed = sse(cid, "", profile["id"], token, path=f"tool-calls/{tool['id']}:approve")
    assert any(event["type"] == "tool.result" and event["ok"] for event in executed), executed
    content = call("GET", f"/cases/{case['id']}/file?path=D-post%2Fplan-forbidden.txt", token=token)["content"]
    assert content == "PLAN_APPROVED", content
    print("3. 用户批准后恢复审批流并真实写入 [OK]")
    print("\n=== Plan 只读/批准恢复冒烟通过 ===")
finally:
    mock.shutdown()
    mock.server_close()
