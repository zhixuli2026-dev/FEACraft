"""F38/F49：当前/旧 JSONL 导入、只读历史恢复、原子错误与安全边界。"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

B = "http://localhost:8000/api/v1"


def request(method, path, body=None, token=None, raw=False, response_bytes=False):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = "application/x-ndjson" if raw else "application/json"
    req = urllib.request.Request(B + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            content = response.read()
            return response.status, content if response_bytes else json.loads(content)
    except urllib.error.HTTPError as error:
        content = error.read()
        if response_bytes:
            return error.code, content
        try:
            return error.code, json.loads(content)
        except json.JSONDecodeError:
            return error.code, {"detail": content.decode("utf-8", "replace")}


def call(method, path, body=None, token=None):
    status, result = request(method, path, body, token)
    if status >= 400:
        return {"_err": status, **(result if isinstance(result, dict) else {})}
    return result


email = f"import-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "import-smoke", "password": "import-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"import-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "import-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"import-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"import-{time.time_ns()}"}, token=token)

lines = [
    {"schema": "vibeflow.agent.session.v1", "session": {
        "title": "Imported history", "model": "legacy-model", "provider_profile_id": 999,
        "permission_mode": "full_access", "workingDirectory": "D:/should/not/be/used"},
     "tool_calls": [{"id": "old-pending", "status": "pending", "args": "{\"api_key\":\"not-imported\"}"}]},
    {"type": "message", "eventId": "old-message", "id": 41,
     "payload": {"role": "user", "content": "LEGACY_MESSAGE"}},
    {"type": "system", "payload": {"kind": "summary", "text": "LEGACY_SUMMARY", "covers_max_id": 41}},
    {"type": "tool", "payload": {"id": "legacy-tool", "name": "list_files", "result": "LEGACY_TOOL_RESULT"}},
    {"type": "request", "payload": {"request_id": "legacy-request", "status": "completed"}},
    {"type": "tool_state", "payload": {"tool_call_id": "pending-tool", "tool": "propose_file_update",
                                      "args": {"path": "D-post/should-not-write.txt"}, "status": "awaiting_approval"}},
    {"type": "goal", "payload": {"goal_id": "old-goal", "revision": 1, "objective": "do-not-resume",
                                 "acceptance_criteria": [], "budget": {"max_turns": 2, "max_tool_calls": 2},
                                 "status": "active"}},
    {"schema": "vibeflow.agent.event.v1", "type": "run_state", "payload": {
        "run_id": "legacy-run", "status": "started", "started_at": 100}},
    {"type": "message", "payload": {"role": "assistant", "content": "IMPORTED_INTERRUPTED_ANSWER",
                                    "run_id": "legacy-run"}},
    {"type": "run_checkpoint", "payload": {"run_id": "legacy-run", "content": "IMPORTED_PARTIAL", "reasoning": ""}},
    {"type": "request_state", "payload": {"run_id": "legacy-run", "request_id": "legacy-request-active",
                                          "request_index": 0, "status": "started", "started_at": 100}},
]
payload = ("\n".join(json.dumps(line) for line in lines) + "\n").encode()
status, imported = request("POST", f"/conversations:import?case_id={case['id']}", payload, token, raw=True)
assert status == 200, imported
cid = imported["id"]
assert imported["provider_profile_id"] is None and imported["permission_mode"] == "approve_writes", imported
assert imported["model"] == "legacy-model" and imported["imported_events"] == len(lines) + 1, imported
detail = call("GET", f"/conversations/{cid}", token=token)
assert detail["pending_tools"] == [] and detail["current_goal"]["status"] == "cancelled", detail
assert any(e["type"] == "message" and e["payload"].get("content") == "LEGACY_MESSAGE" for e in detail["events"])
legacy_message = next(e for e in detail["events"] if e["payload"].get("content") == "LEGACY_MESSAGE")
legacy_summary = next(e for e in detail["events"] if e["payload"].get("text") == "LEGACY_SUMMARY")
assert legacy_summary["payload"]["covers_max_id"] == legacy_message["id"], (legacy_summary, legacy_message)
assert any(e["type"] == "tool_result" and e["payload"].get("result") == "LEGACY_TOOL_RESULT" for e in detail["events"])
assert any(e["type"] == "request" and e["payload"].get("request_id") == "legacy-request" for e in detail["events"])
assert any(e["type"] == "tool_state" and e["payload"].get("status") == "interrupted" for e in detail["events"])
last_run = [e for e in detail["events"] if e["type"] == "run_state"][-1]
assert last_run["payload"]["status"] == "interrupted" and last_run["payload"]["imported_status"] == "started", last_run
last_request = [e for e in detail["events"] if e["type"] == "request_state"][-1]
assert last_request["payload"]["status"] == "interrupted" and last_request["payload"]["request_id"] == "legacy-request-active", last_request
assert last_request["payload"]["duration_ms"] >= 0, last_request
assert call("POST", f"/conversations/{cid}/goal:run", {}, token=token).get("_err") == 409
assert call("POST", f"/conversations/{cid}/tool-calls/{cid}:pending-tool:approve", {}, token=token).get("_err") == 404
interrupted_answer = next(e for e in detail["events"] if e["payload"].get("content") == "IMPORTED_INTERRUPTED_ANSWER")
assert call("POST", f"/conversations/{cid}/fork", {"head_event_id": interrupted_answer["id"]}, token=token).get("_err") == 409
print("1. 当前/旧 message/tool/request 可读，活动 Goal/审批/Run 不恢复执行，中断回答禁分支 [OK]")

status, exported = request("GET", f"/conversations/{cid}/export", token=token, response_bytes=True)
assert status == 200 and b"vibeflow.agent.session.v1" in exported and b"LEGACY_MESSAGE" in exported
status, roundtrip = request("POST", f"/conversations:import?case_id={case['id']}", exported, token, raw=True)
assert status == 200, roundtrip
roundtrip_detail = call("GET", f"/conversations/{roundtrip['id']}", token=token)
assert any(e["payload"].get("content") == "LEGACY_MESSAGE" for e in roundtrip_detail["events"])
roundtrip_message = next(e for e in roundtrip_detail["events"] if e["payload"].get("content") == "LEGACY_MESSAGE")
roundtrip_summary = next(e for e in roundtrip_detail["events"] if e["payload"].get("text") == "LEGACY_SUMMARY")
assert roundtrip_summary["payload"]["covers_max_id"] == roundtrip_message["id"]
assert roundtrip_detail["pending_tools"] == []
legacy_no_ids = b'{"type":"system","payload":{"kind":"summary","text":"NO_ID_SUMMARY","covers_max_id":999}}\n' \
                b'{"type":"message","payload":{"role":"user","content":"NO_ID_HISTORY"}}\n'
status, no_ids = request("POST", f"/conversations:import?case_id={case['id']}", legacy_no_ids, token, raw=True)
assert status == 200, no_ids
no_id_detail = call("GET", f"/conversations/{no_ids['id']}", token=token)
no_id_summary = next(e for e in no_id_detail["events"] if e["payload"].get("text") == "NO_ID_SUMMARY")
assert "covers_max_id" not in no_id_summary["payload"], no_id_summary
print("2. 自有导出回读，摘要数据库 ID 重映射；旧文件缺映射时保留完整历史 [OK]")

before = len(call("GET", f"/conversations?caseId={case['id']}", token=token))
invalid_payloads = [
    b'{"schema":"unknown.v9","type":"message","payload":{}}\n',
    b'{"type":"message","payload":{"role":"system","content":"bad"}}\n',
    b'{"type":"message","payload":{"role":"user","content":"bad","value":NaN}}\n',
    b'{not-json}\n', b'', b'\n'.join(
        b'{"type":"request","payload":{}}' for _ in range(6001)),
]
for invalid in invalid_payloads:
    status, _ = request("POST", f"/conversations:import?case_id={case['id']}", invalid, token, raw=True)
    assert status == 400, status
assert len(call("GET", f"/conversations?caseId={case['id']}", token=token)) == before
status, _ = request("POST", f"/conversations:import?case_id={case['id']}", b'{"type":"request","payload":{}}\n', other, raw=True)
assert status == 404
status, _ = request("POST", f"/conversations:import?case_id={case['id']}", b" " * (16 * 1024 * 1024 + 1), token, raw=True)
assert status == 413
assert len(call("GET", f"/conversations?caseId={case['id']}", token=token)) == before
print("3. 非法 schema/角色/NaN/JSON/超量/跨用户/16 MiB 上限失败且原子零残留 [OK]")
print("\n=== Agent JSONL 导入兼容与安全边界冒烟通过 ===")
