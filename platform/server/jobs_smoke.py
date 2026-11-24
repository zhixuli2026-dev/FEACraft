"""作业系统冒烟：提交快命令 → 收日志 → 完成；提交长命令 → 取消；SSE 实时流"""
import json
import time
import urllib.error
import urllib.request

B = "http://localhost:8000/api/v1"

def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token: req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data) as r:
        return json.loads(r.read())

tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
cases = call("GET", "/workspaces/1/cases", token=tok)
case_id = next(c["id"] for c in cases if c["name"] == "cavity-demo")

# 0. 通用 Jobs 也必须在 DB/进程副作用前拦截高危模式；echo 保证 RED 时也无破坏
before_ids = {item["id"] for item in call("GET", f"/cases/{case_id}/jobs", token=tok)}
try:
    call("POST", f"/cases/{case_id}/jobs",
         {"type": "shell", "command": "echo format C:", "runtime": "windows"}, token=tok)
    raise AssertionError("通用 Jobs 接受了高危命令模式")
except urllib.error.HTTPError as e:
    assert e.code == 400, e.code
    assert "高危命令" in e.read().decode("utf-8", "replace")
after_ids = {item["id"] for item in call("GET", f"/cases/{case_id}/jobs", token=tok)}
assert after_ids == before_ids, "被拒绝的高危命令不应留下 Job 记录"
print("0. Jobs 高危命令 400 且无 Job 记录 [OK]")

# 0b. WSL 变量必须在 source v2406 后展开，而非被默认 shell 提前展开
j_env = call("POST", f"/cases/{case_id}/jobs", {
    "type": "shell", "runtime": "wsl",
    "command": "source /usr/lib/openfoam/openfoam2406/etc/bashrc && printf 'WM_PROJECT_DIR=%s\\n' \"$WM_PROJECT_DIR\" && test \"$WM_PROJECT_DIR\" = \"/usr/lib/openfoam/openfoam2406\"",
}, token=tok)
for _ in range(45):
    time.sleep(1)
    env_st = call("GET", f"/jobs/{j_env['id']}", token=tok)
    if env_st["status"] not in ("queued", "running"):
        break
assert env_st["status"] == "succeeded", env_st
assert "WM_PROJECT_DIR=/usr/lib/openfoam/openfoam2406" in env_st["log_backlog"], env_st["log_backlog"]
print("0b. WSL source 后展开变量且使用 v2406 [OK]")

# 1. 快命令：cmd 下 echo + 列目录
j = call("POST", f"/cases/{case_id}/jobs",
         {"type": "shell", "command": "echo JOB_FAST_OK && dir 0-caseDict /b"}, token=tok)
print("1. submitted job", j["id"], j["status"])
time.sleep(3)
st = call("GET", f"/jobs/{j['id']}", token=tok)
print("   status:", st["status"], "exit:", st["exit_code"])
assert st["status"] == "succeeded", st
assert "JOB_FAST_OK" in st["log_backlog"] and "caseDict" in st["log_backlog"]

# 2. 增量日志接口
lg = call("GET", f"/jobs/{j['id']}/log?offset=0", token=tok)
assert "JOB_FAST_OK" in lg["chunk"]
lg2 = call("GET", f"/jobs/{j['id']}/log?offset={lg['offset']}", token=tok)
assert lg2["chunk"] == ""
print("2. 增量日志 ok（offset 续读为空）")

# 3. 长命令 → 取消（Windows cmd: ping 近似 sleep）
j2 = call("POST", f"/cases/{case_id}/jobs",
          {"type": "shell", "command": "ping -n 60 127.0.0.1"}, token=tok)
time.sleep(2)
st2 = call("GET", f"/jobs/{j2['id']}", token=tok)
assert st2["status"] == "running", st2["status"]
c = call("POST", f"/jobs/{j2['id']}:cancel", {}, token=tok)
print("3. cancel:", c)
time.sleep(2)
st3 = call("GET", f"/jobs/{j2['id']}", token=tok)
print("   after cancel:", st3["status"])
assert st3["status"] == "cancelled"

# 4. SSE 实时流（提交一个分多段输出的命令，边跑边收）
j3 = call("POST", f"/cases/{case_id}/jobs",
          {"type": "shell", "command": "echo PART1 && ping -n 3 127.0.0.1 >nul && echo PART2"}, token=tok)
req = urllib.request.Request(f"{B}/jobs/{j3['id']}/log-stream",
                             headers={"Authorization": f"Bearer {tok}"})
chunks, got_done = [], False
t0 = time.time()
with urllib.request.urlopen(req, timeout=30) as resp:
    for line in resp:
        s = line.decode("utf-8", "replace").strip()
        if s.startswith("data:"):
            ev = json.loads(s[5:].strip())
            if ev["type"] == "log":
                chunks.append(ev["text"])
            elif ev["type"] == "done":
                got_done = True
                break
full = "".join(chunks)
print(f"4. SSE 流: {len(chunks)} 块, done={got_done}, 耗时 {time.time()-t0:.1f}s")
assert "PART1" in full and "PART2" in full
assert got_done

# 5. 多工况派生（幂等：唯一名）
name = f"case-derive-{int(time.time()) % 100000}"
r = call("POST", f"/cases/{case_id}/runs:derive", {"base": "C-run/case-1", "name": name}, token=tok)
assert r["ok"] and r["files"] > 0, r
tree = call("GET", f"/cases/{case_id}/tree", token=tok)
paths = [e["path"] for e in tree["entries"] if e["path"].startswith(f"C-run/{name}/")]
assert any(p.endswith("system/controlDict") for p in paths), paths
for initial in (f"C-run/{name}/0/U", f"C-run/{name}/0/p"):
    assert initial in paths, f"派生必须保留初始场 {initial}；实际只有 {paths}"
for result in (f"C-run/{name}/0.1/U", f"C-run/{name}/1/U"):
    assert result not in paths, f"结果时刻不应复制: {result}"
assert f"C-run/{name}/case-1.foam" not in paths
assert not any(p.endswith("blockMesh.log") for p in paths)
print(f"5. 多工况派生 [OK] {name}: {r['files']} 文件（初始场保留/时刻日志跳过）")

# 6. 派生工况必须能实际运行，而非只复制目录
j = call("POST", f"/cases/{case_id}/jobs",
         {"type": "solve", "command": f"source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/{name} && icoFoam", "runtime": "wsl"}, token=tok)
t0 = time.time()
while time.time() - t0 < 90:
    st = call("GET", f"/jobs/{j['id']}", token=tok)
    if st["status"] in ("succeeded", "failed", "cancelled"):
        break
    time.sleep(1)
assert st["status"] == "succeeded", st
solve_log = call("GET", f"/jobs/{j['id']}/log?offset=0", token=tok)
assert "Version:  2406" in solve_log["chunk"], "派生求解未使用 v2406"
paths = [e["path"] for e in call("GET", f"/cases/{case_id}/tree", token=tok)["entries"]]
assert f"C-run/{name}/0.1/U" in paths, "派生工况没有产生新的 0.1 时刻"
print("6. 派生工况 icoFoam 可运行并产生新时刻 [OK]")

print("\n=== 作业系统冒烟通过（提交/日志增量/取消/SSE 实时流/多工况派生）===")
