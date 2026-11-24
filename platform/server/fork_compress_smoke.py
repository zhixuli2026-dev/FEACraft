"""分叉/压缩/翻译/作业状态落库 冒烟（确定性，用 mock LLM + 直连 Postgres 断言）

覆盖本轮新增：
1. 会话 fork（零拷贝）：分叉后事件流 = 父历史合并；分叉续聊不影响父会话
2. 会话 compress：LLM 摘要落 system 事件；后续对话正常续跑
3. /llm/translate：非流式补全双协议路径
4. 作业状态落库：跑完后 DB 行是 succeeded+exit_code=0（不再只靠内存注册表）
"""
import asyncio
import json
import time
import urllib.request

import asyncpg

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data) as r:
        return json.loads(r.read())


def sse_collect(path, body, token):
    req = urllib.request.Request(B + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("data:"):
                events.append(json.loads(s[5:].strip()))
    return events


email = f"fork-{time.time_ns()}@vibeflow.cn"
tok = call("POST", "/auth/register", {"email": email, "name": "fork-smoke", "password": "fork-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"fork-ws-{time.time_ns()}"}, token=tok)
case = call("POST", f"/workspaces/{workspace['id']}/cases",
            {"name": f"fork-case-{time.time_ns()}"}, token=tok)
case_id = case["id"]
p = call("POST", "/provider-profiles", {
    "provider": "custom", "name": "fork-mock", "base_url": "http://127.0.0.1:8990",
    "default_model": "mock-model", "api_key": "mock-key"}, token=tok)

# ---------- 1. fork ----------
cid = call("POST", "/conversations",
           {"case_id": case_id, "title": "fork源", "provider_profile_id": p["id"],
            "model": "mock-model"}, token=tok)["id"]
for i in range(3):
    sse_collect(f"/conversations/{cid}/messages",
                {"content": f"第{i+1}轮问题", "provider_profile_id": p["id"],
                 "model": "mock-model"}, tok)
src = call("GET", f"/conversations/{cid}", token=tok)
n_src = len(src["events"])
assert n_src == 21, f"3 轮对话含 Run/Request/checkpoint 应有 21 条事件，实际 {n_src}"

fork = call("POST", f"/conversations/{cid}/fork", {}, token=tok)
fid = fork["id"]
assert fork["forked_from_id"] == cid
fconv = call("GET", f"/conversations/{fid}", token=tok)
assert len(fconv["events"]) == n_src, \
    f"分叉应看到父会话全部 {n_src} 条事件（零拷贝合并），实际 {len(fconv['events'])}"
assert fconv["events"][0]["payload"].get("content") == "第1轮问题"
print(f"1. fork [OK] 分叉 #{fid} 合并父历史 {n_src} 条（零拷贝）")

# 分叉续聊不影响父会话
sse_collect(f"/conversations/{fid}/messages",
            {"content": "分叉分支提问", "provider_profile_id": p["id"],
             "model": "mock-model"}, tok)
assert len(call("GET", f"/conversations/{fid}", token=tok)["events"]) == n_src + 7
assert len(call("GET", f"/conversations/{cid}", token=tok)["events"]) == n_src
print("2. fork 隔离 [OK] 分叉续聊 +7（含 Run/Request 生命周期），父会话不变")

# ---------- 2. compress ----------
r = call("POST", f"/conversations/{fid}/compress",
         {"provider_profile_id": p["id"], "keep_last": 2}, token=tok)
assert r["ok"] and "[mock-nonstream]" in r["summary"], r
print(f"3. compress [OK] 压缩 {r['compressed_events']} 条，保留 {r['kept_events']} 条，"
      f"摘要: {r['summary'][:50]}…")

fconv2 = call("GET", f"/conversations/{fid}", token=tok)
summaries = [e for e in fconv2["events"]
             if e["type"] == "system" and e["payload"].get("kind") == "summary"]
assert len(summaries) == 1 and summaries[0]["payload"]["covers_max_id"] > 0
# 压缩后仍可续聊（_build_messages 裁剪历史 + 注入摘要不崩）
evs = sse_collect(f"/conversations/{fid}/messages",
                  {"content": "压缩后继续", "provider_profile_id": p["id"],
                   "model": "mock-model"}, tok)
assert any(e["type"] == "run.completed" for e in evs), [e["type"] for e in evs]
print("4. 压缩后续跑 [OK] 模型正常回答")

# 对话太短应拒绝压缩
cid_short = call("POST", "/conversations",
                 {"case_id": case_id, "title": "短会话", "provider_profile_id": p["id"],
                  "model": "mock-model"}, token=tok)["id"]
sse_collect(f"/conversations/{cid_short}/messages",
            {"content": "hi", "provider_profile_id": p["id"], "model": "mock-model"}, tok)
r2 = call("POST", f"/conversations/{cid_short}/compress",
          {"provider_profile_id": p["id"], "keep_last": 6}, token=tok)
assert r2["ok"] is False
print("5. 短会话拒压 [OK]")

# ---------- 3. translate（非流式双协议）----------
r = call("POST", "/llm/translate",
         {"text": "residual convergence", "target": "zh",
          "provider_profile_id": p["id"]}, token=tok)
assert r["ok"] and "[mock-nonstream]" in r["translation"]
print(f"6. translate [OK] {r['translation'][:50]}")

pa = call("POST", "/provider-profiles", {
    "provider": "anthropic", "name": "fork-mock-a", "base_url": "http://127.0.0.1:8990",
    "default_model": "mock-model", "api_key": "mock-key"}, token=tok)
ra = call("POST", "/llm/translate",
          {"text": "hello", "target": "zh", "provider_profile_id": pa["id"]}, token=tok)
assert ra["ok"] and "[mock-nonstream]" in ra["translation"]
print("7. translate(anthropic 协议) [OK]")

# ---------- 4. 作业状态落库 ----------
j = call("POST", f"/cases/{case_id}/jobs",
         {"type": "shell", "command": "echo PERSIST_OK"}, token=tok)
time.sleep(2.5)  # 等子进程收尾 + 落库
st = call("GET", f"/jobs/{j['id']}", token=tok)
assert st["status"] == "succeeded" and st["exit_code"] == 0, st

async def db_check(job_id: int):
    from app.config import get_settings
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchrow(
            "SELECT status, exit_code, ended_at FROM jobs WHERE id=$1", job_id)
    finally:
        await conn.close()

row = asyncio.run(db_check(j["id"]))
assert row["status"] == "succeeded" and row["exit_code"] == 0 and row["ended_at"], dict(row)
print(f"8. 作业状态落库 [OK] DB 行: status={row['status']} exit_code={row['exit_code']}")

# ---------- 5. 权限模式（full_access/ask/approve_writes）----------
cF = call("POST", "/conversations",
          {"case_id": case_id, "title": "full_access", "provider_profile_id": p["id"],
           "model": "mock-model", "permission_mode": "full_access"}, token=tok)["id"]
evF = sse_collect(f"/conversations/{cF}/messages",
                  {"content": "帮我跑一下 echo", "provider_profile_id": p["id"],
                   "model": "mock-model"}, tok)
typesF = [e["type"] for e in evF]
assert "approval.requested" not in typesF
assert any(e["type"] == "tool.result" and "MOCK_TOOL_OK" in e.get("result", "") for e in evF)
print("9. full_access 自动执行 [OK]")

cA = call("POST", "/conversations",
          {"case_id": case_id, "title": "ask", "provider_profile_id": p["id"],
           "model": "mock-model", "permission_mode": "ask"}, token=tok)["id"]
evA = sse_collect(f"/conversations/{cA}/messages",
                  {"content": "帮我跑一下 echo", "provider_profile_id": p["id"],
                   "model": "mock-model"}, tok)
typesA = [e["type"] for e in evA]
assert "tool.result" not in typesA and "approval.requested" not in typesA
print("10. ask 无工具 [OK]")

cD = call("POST", "/conversations",
          {"case_id": case_id, "title": "默认审批", "provider_profile_id": p["id"],
           "model": "mock-model"}, token=tok)["id"]
evD = sse_collect(f"/conversations/{cD}/messages",
                  {"content": "帮我跑一下 echo", "provider_profile_id": p["id"],
                   "model": "mock-model"}, tok)
assert any(e["type"] == "approval.requested" for e in evD)
print("11. approve_writes 默认审批 [OK]")

# ---------- 6. caseDict 块级编辑（表单编辑器后端；保注释/保状态） ----------
before = call("GET", f"/cases/{case_id}/caseDict", token=tok)
try:
    r = call("PUT", f"/cases/{case_id}/caseDict/block",
             {"block": "caseMeta", "entries": {"discipline": "fluid", "analysisType": "transient"}},
             token=tok)
    assert r["ok"], r
    after = call("GET", f"/cases/{case_id}/caseDict", token=tok)
    cm = after["parsed"]["caseMeta"]
    assert cm.get("discipline") == "fluid", cm
    assert after["workflow_state"] == before["workflow_state"]  # 顶级标量不动
    assert "//" in after["content"]                             # 注释保留
    mirror = call("GET", f"/cases/{case_id}", token=tok)
    mirror_meta = json.loads(mirror["case_meta"])
    assert mirror_meta.get("discipline") == "fluid", mirror
    assert mirror_meta.get("analysisType") == "transient", mirror
    assert mirror["workflow_state"] == after["workflow_state"], mirror
    assert mirror["dict_revision"] == after["dict_revision"], mirror
finally:
    call("PUT", f"/cases/{case_id}/caseDict/block",
         {"block": "caseMeta", "entries": {}}, token=tok)       # 还原
print("12. caseDict 块级编辑 [OK]（写入/读回/DB 镜像/注释保留/还原）")

print("\n=== fork/compress/translate/作业落库/权限模式/块编辑 冒烟全过 ===")
