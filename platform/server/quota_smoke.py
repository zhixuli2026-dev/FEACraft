"""订阅配额冒烟：回填 → org/mine → 改 plan 配额 → 429 执法 → 还原

用 asyncpg 直接改 dev 用户套餐配额（压测执法点），跑完还原 free 默认。
"""
import asyncio
import json
import time
import urllib.error
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
    try:
        with urllib.request.urlopen(req, data) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "detail": json.loads(e.read()).get("detail", "")}


async def set_quota(key: str, value: int):
    conn = await asyncpg.connect("postgresql://simulogic:simulogic@127.0.0.1:5432/vibeflow")
    try:
        quotas = json.loads((await conn.fetchval("SELECT quotas FROM plans WHERE code='free'")) or "{}")
        quotas[key] = value
        await conn.execute("UPDATE plans SET quotas=$1 WHERE code='free'", json.dumps(quotas))
    finally:
        await conn.close()


tok = call("POST", "/auth/login",
           {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]

# 1. 回填+org/mine
r = call("GET", "/org/mine", token=tok)
assert r["plan"]["code"] == "free" and r["usage"]["quotas"]["job_concurrent"] >= 1, r
print(f"1. org/mine [OK] org={r['org']['name']} plan=free 用量={list(r['usage'].keys())}")

# 2. 作业并发配额执法：限额压到 0 → 提交即 429
asyncio.run(set_quota("job_concurrent", 0))
r2 = call("POST", "/cases/1/jobs", {"type": "shell", "command": "echo Q"}, token=tok)
assert r2.get("_err") == 429, r2
print("2. 作业配额 429 [OK]", r2["detail"][:40])
asyncio.run(set_quota("job_concurrent", 4))
r3 = call("POST", "/cases/1/jobs", {"type": "shell", "command": "echo Q_OK"}, token=tok)
assert r3.get("id"), r3
print("3. 还原后可提交 [OK]")

# 4. LLM 日额度执法：压到 0 → 发消息 429
asyncio.run(set_quota("llm_msgs_per_day", 0))
p = call("POST", "/provider-profiles", {"provider": "custom", "name": "quota-mock",
         "base_url": "http://127.0.0.1:8990", "default_model": "mock-model",
         "api_key": "mock-key"}, token=tok)
c = call("POST", "/conversations", {"case_id": 1, "title": "quota",
         "provider_profile_id": p["id"], "model": "mock-model"}, token=tok)
r4 = call("POST", f"/conversations/{c['id']}/messages",
          {"content": "hi", "provider_profile_id": p["id"], "model": "mock-model"}, token=tok)
# SSE 端点在配额拒绝时返回 429（非流式错误）
assert r4.get("_err") == 429, r4
print("4. LLM 日额度 429 [OK]")
asyncio.run(set_quota("llm_msgs_per_day", 500))

# 5. 存储配额：压到 0MB → 上传即 429
asyncio.run(set_quota("storage_mb", 0))
boundary = "----vfquota"
payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"q.txt\"\r\n\r\n"
           "hello\r\n--%s--\r\n" % boundary).encode()
req = urllib.request.Request(f"{B}/cases/1/upload?path=Ref/q.txt", method="POST", data=payload,
    headers={"Authorization": f"Bearer {tok}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
try:
    urllib.request.urlopen(req)
    raise AssertionError("应 429")
except urllib.error.HTTPError as e:
    assert e.code == 429, e.code
print("5. 存储配额 429 [OK]")
asyncio.run(set_quota("storage_mb", 2048))

# 6. viz 并发配额：独立 case，不删除或占用任何既有会话
viz_case = call("POST", "/workspaces/1/cases",
                {"name": f"quota-viz-{time.time_ns()}"}, token=tok)
viz_case_id = viz_case["id"]
for path in (
    "C-run/case-1/system/controlDict", "C-run/case-1/system/fvSchemes",
    "C-run/case-1/system/fvSolution", "C-run/case-1/constant/transportProperties",
    "C-run/case-1/constant/polyMesh/points", "C-run/case-1/constant/polyMesh/faces",
    "C-run/case-1/constant/polyMesh/owner", "C-run/case-1/constant/polyMesh/neighbour",
    "C-run/case-1/constant/polyMesh/boundary", "C-run/case-1/0/U", "C-run/case-1/0/p",
):
    source = call("GET", f"/cases/1/file?path={path}", token=tok)
    assert "content" in source, (path, source)
    call("PUT", f"/cases/{viz_case_id}/file", {"path": path, "content": source["content"]}, token=tok)
asyncio.run(set_quota("viz_concurrent", 0))
try:
    r6 = call("POST", f"/cases/{viz_case_id}/viz:sessions", {"run_dir": "C-run/case-1"}, token=tok)
    assert r6.get("_err") == 429, r6
    remaining = call("GET", f"/cases/{viz_case_id}/viz:sessions", token=tok)
    assert remaining == [], remaining
finally:
    asyncio.run(set_quota("viz_concurrent", 4))
print("6. viz 配额 429 且未创建会话 [OK]")
allowed = call("POST", f"/cases/{viz_case_id}/viz:sessions", {"run_dir": "C-run/case-1"}, token=tok)
assert allowed.get("alive"), allowed
assert call("DELETE", f"/viz:sessions/{allowed['id']}", token=tok).get("ok")
print("7. viz 配额还原后可创建并关闭会话 [OK]")

print("\n=== 订阅配额冒烟通过（org/mine + 四维执法+还原）===")
