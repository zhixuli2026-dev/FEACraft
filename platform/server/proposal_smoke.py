"""F26/F27：提案基线哈希、外部修改冲突与原子写入。"""
import hashlib
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services.tools import ToolExecutor

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
    with urllib.request.urlopen(request, data, timeout=30) as response:
        return json.loads(response.read())


def sse(path, token, body=None):
    request = urllib.request.Request(B + path, method="POST",
        data=json.dumps(body or {}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


email = f"proposal-{time.time_ns()}@vibeflow.cn"
registered = call("POST", "/auth/register", {"email": email, "name": "proposal-smoke", "password": "proposal-pass-123"})
token = registered["token"]
owner_id = registered["user"]["id"]
workspace = call("POST", "/workspaces", {"name": f"proposal-ws-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"proposal-{time.time_ns()}"}, token=token)
prefix = case.get("s3_prefix") or f"workspaces/{owner_id}/cases/{case['id']}"
executor = ToolExecutor(prefix)
call("PUT", f"/cases/{case['id']}/file", {"path": "D-post/baseline.txt", "content": "OLD"}, token=token)
assert executor._path("D-post/baseline.txt").is_file(), (prefix, str(executor._path("D-post/baseline.txt")), case)
args = executor.proposal_baseline("propose_file_update", {
    "path": "D-post/baseline.txt", "content": "NEW", "reason": "baseline"})
assert args["baseline_sha256"] == hashlib.sha256(b"OLD").hexdigest(), args
call("PUT", f"/cases/{case['id']}/file", {"path": "D-post/baseline.txt", "content": "EXTERNAL"}, token=token)
try:
    executor.apply_file_update(args["path"], args["content"], baseline_sha256=args["baseline_sha256"], enforce_baseline=True)
    raise AssertionError("外部修改后不应批准写入")
except ValueError as error:
    assert "外部修改" in str(error), error
current = call("GET", f"/cases/{case['id']}/file?path=D-post%2Fbaseline.txt", token=token)
assert current["content"] == "EXTERNAL", current
print("1. 基线后外部修改拒绝且保留外部内容 [OK]")

call("PUT", f"/cases/{case['id']}/file", {"path": "D-post/baseline.txt", "content": "OLD"}, token=token)
executor.apply_file_update(args["path"], args["content"], baseline_sha256=args["baseline_sha256"], enforce_baseline=True)
current = call("GET", f"/cases/{case['id']}/file?path=D-post%2Fbaseline.txt", token=token)
assert current["content"] == "NEW", current
assert not list(executor._path("D-post").glob(".baseline.txt.*")), "原子写临时文件未清理"
print("2. 未变化时原子写入成功且临时文件清理 [OK]")

text_args = executor.proposal_baseline("propose_text_replacement", {
    "path": "D-post/baseline.txt", "old_text": "NEW", "new_text": "TEXT", "reason": "text"})
executor.apply_text_replacement(text_args["path"], text_args["old_text"], text_args["new_text"],
                                baseline_sha256=text_args["baseline_sha256"], enforce_baseline=True)
current = call("GET", f"/cases/{case['id']}/file?path=D-post%2Fbaseline.txt", token=token)
assert current["content"] == "TEXT", current
missing = executor.proposal_baseline("propose_file_update", {
    "path": "D-post/new-file.txt", "content": "created", "reason": "new"})
assert missing["baseline_sha256"] is None, missing
call("PUT", f"/cases/{case['id']}/file", {"path": "D-post/new-file.txt", "content": "racer"}, token=token)
try:
    executor.apply_file_update(missing["path"], missing["content"],
                               baseline_sha256=missing["baseline_sha256"], enforce_baseline=True)
    raise AssertionError("不存在基线后新建文件也应冲突")
except ValueError as error:
    assert "外部修改" in str(error), error
print("3. 文本替换基线和不存在→存在的竞态均受控 [OK]")


class ProposalMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if request["messages"][-1]["role"] == "tool":
            events = [{"choices": [{"delta": {"content": "PROPOSAL_DONE"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        else:
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_proposal",
                "function": {"name": "propose_file_update", "arguments": json.dumps({
                    "path": "D-post/agent-baseline.txt", "content": "AGENT_NEW", "reason": "agent baseline"})}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


mock = ThreadingHTTPServer(("127.0.0.1", PORT), ProposalMock)
PORT = mock.server_address[1]
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "proposal-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-proposal", "api_key": "mock-key"}, token=token)
    call("PUT", f"/cases/{case['id']}/file", {"path": "D-post/agent-baseline.txt", "content": "AGENT_OLD"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case["id"], "title": "proposal", "provider_profile_id": profile["id"],
        "model": "mock-proposal"}, token=token)["id"]
    events = sse(f"/conversations/{cid}/messages", token, {
        "content": "proposal", "provider_profile_id": profile["id"], "model": "mock-proposal"})
    tool = next(event["tool_call"] for event in events if event["type"] == "approval.requested")
    assert tool["args"]["baseline_sha256"] == hashlib.sha256(b"AGENT_OLD").hexdigest(), tool
    call("PUT", f"/cases/{case['id']}/file", {"path": "D-post/agent-baseline.txt", "content": "AGENT_EXTERNAL"}, token=token)
    approved = sse(f"/conversations/{cid}/tool-calls/{tool['id']}:approve", token)
    result = next(event for event in approved if event["type"] == "tool.result")
    assert not result["ok"] and "CaseWriteConflict" in result["result"], result
    current = call("GET", f"/cases/{case['id']}/file?path=D-post%2Fagent-baseline.txt", token=token)
    assert current["content"] == "AGENT_EXTERNAL", current
    print("4. Agent 审批时二次校验基线，冲突不覆盖 [OK]")

    cid2 = call("POST", "/conversations", {
        "case_id": case["id"], "title": "proposal success", "provider_profile_id": profile["id"],
        "model": "mock-proposal"}, token=token)["id"]
    events = sse(f"/conversations/{cid2}/messages", token, {
        "content": "proposal", "provider_profile_id": profile["id"], "model": "mock-proposal"})
    tool = next(event["tool_call"] for event in events if event["type"] == "approval.requested")
    approved = sse(f"/conversations/{cid2}/tool-calls/{tool['id']}:approve", token)
    result = next(event for event in approved if event["type"] == "tool.result")
    assert result["ok"] and result["status"] == "completed", result
    current = call("GET", f"/cases/{case['id']}/file?path=D-post%2Fagent-baseline.txt", token=token)
    assert current["content"] == "AGENT_NEW", current
    print("5. Agent 基线未变时批准成功 [OK]")
    print("\n=== 提案基线/原子写/审批竞态冒烟通过 ===")
finally:
    mock.shutdown()
