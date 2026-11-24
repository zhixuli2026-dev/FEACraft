"""Agent 工具循环冒烟（确定性，用 mock LLM）：
场景A 危险工具审批流：「跑一下」→ tool_calls(run_shell_command) → approval.requested →
  挂起 → approve → 执行（cmd echo MOCK_TOOL_OK）→ 续跑 → 最终回答含输出
场景B 安全工具免审批：「列出文件」→ list_files 自动执行 → 循环续跑 → 最终回答含文件列表
"""
import json
import os
import time
import urllib.request

B = os.environ.get('VIBEFLOW_TEST_BASE',"http://localhost:8000/api/v1")

def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token: req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data) as r:
        return json.loads(r.read())

def sse_collect(path, body, token, method="POST"):
    req = urllib.request.Request(B + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("data:"):
                events.append(json.loads(s[5:].strip()))
    return events

email = f"agent-tools-{time.time_ns()}@vibeflow.cn"
tok = call("POST", "/auth/register", {"email": email, "name": "agent-tools-smoke", "password": "agent-tools-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"agent-tools-ws-{time.time_ns()}"}, token=tok)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"agent-tools-case-{time.time_ns()}"}, token=tok)
case_id = case["id"]

for provider in ["custom", "anthropic"]:
    print(f"\n========== provider={provider} ==========")
    p = call("POST", "/provider-profiles", {
        "provider": provider, "name": f"tools-{provider}",
        "base_url": os.environ.get('VIBEFLOW_TEST_LLM_BASE',"http://127.0.0.1:8990"),
        "default_model": "mock-model", "api_key": "mock-key"}, token=tok)

    # ---- 场景A：危险工具审批 ----
    cid = call("POST", "/conversations",
               {"case_id": case_id, "title": f"审批-{provider}",
                "provider_profile_id": p["id"], "model": "mock-model"}, token=tok)["id"]
    events = sse_collect(f"/conversations/{cid}/messages",
        {"content": "帮我跑一下 echo 命令", "provider_profile_id": p["id"], "model": "mock-model"}, tok)
    types = [e["type"] for e in events]
    print("A.消息流:", types)
    assert "approval.requested" in types, "缺 approval.requested"
    assert types[-1] == "stream.done" and events[-1].get("reason") == "awaiting_approval"
    tc = next(e["tool_call"] for e in events if e["type"] == "approval.requested")
    assert tc["tool"] == "run_shell_command" and "echo MOCK_TOOL_OK" in tc["args"]["command"]
    print(f"A.审批请求: {tc['tool']} {tc['args']}")

    # 批准 → 续跑（SSE 流）
    events2 = sse_collect(f"/conversations/{cid}/tool-calls/{tc['id']}:approve", {}, tok)
    approved_results = [e for e in events2 if e["type"] == "tool.result" and e.get("approved")]
    assert len(approved_results) == 1 and "MOCK_TOOL_OK" in approved_results[0]["result"], events2
    assert approved_results[0]["status"] == "completed", approved_results
    text2 = "".join(e.get("text", "") for e in events2 if e["type"] == "text.delta")
    print("A.续跑回答:", text2[:120])
    assert "MOCK_TOOL_OK" in text2, f"续跑回答应包含工具输出: {text2!r}"

    # DB 落库检查
    conv = call("GET", f"/conversations/{cid}", token=tok)
    etypes = [e["type"] for e in conv["events"]]
    tool_results = [e for e in conv["events"] if e["type"] == "tool_result"]
    assert tool_results and tool_results[0]["payload"].get("approved") is True
    assert tool_results[0]["payload"].get("status") == "completed"
    assert any(row["id"] == tc["id"] and row["status"] == "executed" for row in conv["tool_calls"]), conv["tool_calls"]
    assert any(e["type"] == "message" and e["payload"].get("tool_calls") for e in conv["events"])
    print("A.落库 [OK] 事件序列:", etypes)
    assert not conv["pending_tools"], "审批后不应再有 pending"

    # ---- 场景B：安全工具免审批 ----
    cid2 = call("POST", "/conversations",
                {"case_id": case_id, "title": f"安全-{provider}",
                 "provider_profile_id": p["id"], "model": "mock-model"}, token=tok)["id"]
    events3 = sse_collect(f"/conversations/{cid2}/messages",
        {"content": "列出文件看看", "provider_profile_id": p["id"], "model": "mock-model"}, tok)
    types3 = [e["type"] for e in events3]
    text3 = "".join(e.get("text", "") for e in events3 if e["type"] == "text.delta")
    tool_ev = [e for e in events3 if e["type"] == "tool.result"]
    state_ev = [e for e in events3 if e["type"] == "tool.state"]
    assert [e["status"] for e in state_ev[:2]] == ["queued", "running"], state_ev
    safe_detail = call("GET", f"/conversations/{cid2}", token=tok)
    assert any(row["status"] == "executed" for row in safe_detail["tool_calls"]), safe_detail["tool_calls"]
    persisted_states = [e["payload"]["status"] for e in safe_detail["events"] if e["type"] == "tool_state"]
    assert persisted_states[:2] == ["queued", "running"], persisted_states
    print("B.消息流:", types3)
    assert "approval.requested" not in types3, "安全工具不应触发审批"
    assert tool_ev and tool_ev[0]["tool"] == "list_files" and tool_ev[0]["ok"]
    assert "caseDict" in tool_ev[0]["result"], "list_files 应看到脚手架文件"
    assert "run.completed" in types3 or "stream.done" in types3
    print("B.list_files 直接执行 [OK]，结果含 caseDict")
    cid3=call('POST','/conversations',{'case_id':case_id,'title':'full-access owned shell',
        'provider_profile_id':p['id'],'model':'mock-model','permission_mode':'full_access'},token=tok)['id']
    automatic=sse_collect(f'/conversations/{cid3}/messages',{'content':'帮我跑一下 echo 命令','provider_profile_id':p['id'],'model':'mock-model'},tok)
    assert not any(event['type']=='approval.requested' for event in automatic),automatic
    actual=next(event for event in automatic if event['type']=='tool.result')
    assert actual['ok'] and 'MOCK_TOOL_OK' in actual['result'],actual
    detail=call('GET',f'/conversations/{cid3}',token=tok)
    native=call('GET',f"/conversations/{cid3}/tool-calls/{detail['tool_calls'][0]['id']}/execution",token=tok)
    assert native['phase']=='succeeded' and native['cleanup_verified'] is True and len(native['children']['children'])==1,native
    print('C.full_access auto tool uses the same durable native ownership [OK]')

print("\n=== Agent 工具循环冒烟通过（审批流 + 免审批流，双协议）===")
