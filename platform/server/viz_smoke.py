"""可视化会话冒烟（M2 第一刀）：
建会话 → kernel 就绪（trame 客户端 200）→ 列表存活 → 关闭 → 进程终止
另测：非法目录 400、并发上限 409、自动 .foam 桩文件
"""
import json
import os
from cae_fixtures import cavity_2d
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")


def call(method, path, body=None, token=None, raw=False):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data) as r:
            payload = r.read()
            return r.status if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        return e.code if raw else {"_err": e.code, "detail": json.loads(e.read()).get("detail")}


tok = call("POST", "/auth/login",
           {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"viz-{time.time_ns()}"}, token=tok)["id"]
case_id = call("POST", f"/workspaces/{workspace}/cases", {"name": "generated-viz-mesh"}, token=tok)["id"]
for path, content in cavity_2d().items():
    assert call("PUT", f"/cases/{case_id}/file", {"path": path, "content": content}, token=tok)["ok"]
mesh = call("POST", f"/cases/{case_id}/jobs", {"type": "mesh", "runtime": "wsl",
    "command": "source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && blockMesh"}, token=tok)
for _ in range(300):
    state = call("GET", f"/jobs/{mesh['id']}", token=tok)
    if state["status"] not in {"queued", "running"}:
        break
    time.sleep(0.1)
assert state["status"] == "succeeded", state



def register(email, password):
    r = call("POST", "/auth/register",
             {"email": email, "name": "viz-security", "password": password})
    if "token" in r:
        return r["token"]
    assert r.get("_err") == 409, r
    return call("POST", "/auth/login", {"email": email, "password": password})["token"]

# 非法目录被拒
r = call("POST", f"/cases/{case_id}/viz:sessions", {"run_dir": "C-run"}, token=tok)
assert r.get("_err") == 400, r
print("1. 非法目录 400 [OK]", r["detail"][:40])

# 正常建会话（黄金算例 C-run/case-1）
owned = []
try:
    s1 = call("POST", f"/cases/{case_id}/viz:sessions", {"run_dir": "C-run/case-1"}, token=tok)
    assert s1.get("alive") and s1.get("port"), s1
    owned.append(s1["id"])
    print(f"2. 会话 #{s1['id']} 就绪 [OK] port={s1['port']} url={s1['url']}")

    # trame 客户端可服务
    import urllib.request as u
    with u.urlopen(s1["url"], timeout=5) as resp:
        assert resp.status == 200 and b"trame" in resp.read().lower()
    print("3. trame 客户端 200 [OK]")

    # .foam 桩自动补
    tree = call("GET", f"/cases/{case_id}/tree", token=tok)
    assert any(e["path"] == "C-run/case-1/case-1.foam" for e in tree["entries"])
    print("4. .foam 桩自动创建 [OK]")

    # 会话列表
    lst = call("GET", f"/cases/{case_id}/viz:sessions", token=tok)
    assert any(x["id"] == s1["id"] and x["alive"] for x in lst)
    print(f"5. 列表存活 [OK] 共 {len(lst)} 个")

    # 其他用户不能经主后端 proxy 访问此会话
    other_tok = register(f"viz-security-{time.time_ns()}@vibeflow.cn", "viz-security-pass")
    proxy_url = B.removesuffix("/api/v1") + f"/viz-proxy/{s1['id']}/index.html?token={other_tok}"
    try:
        with urllib.request.urlopen(proxy_url, timeout=10) as resp:
            proxy_status = resp.status
    except urllib.error.HTTPError as e:
        proxy_status = e.code
    if proxy_status != 404:
        call("DELETE", f"/viz:sessions/{s1['id']}", token=tok)
        raise AssertionError(f"跨用户 viz proxy 应返回 404，实际 {proxy_status}")
    print("6. 跨用户 viz proxy 404 [OK]")

    owner_proxy = B.removesuffix("/api/v1") + f"/viz-proxy/{s1['id']}/index.html?token={tok}"
    with urllib.request.urlopen(owner_proxy, timeout=10) as resp:
        body = resp.read(4000).lower()
        assert resp.status == 200 and b"trame" in body, (resp.status, body[:100])
    print("7. 所有者 viz proxy 200 [OK]")
    time.sleep(1.1)
    activity = next(x for x in call("GET", f"/cases/{case_id}/viz:sessions", token=tok)
                    if x["id"] == s1["id"])
    assert activity["last_activity"] > s1["last_activity"], (activity, s1)
    print("7b. proxy 请求更新 last_activity [OK]")

    # 并发上限（第二个应成功，第三个 409）
    s2 = call("POST", f"/cases/{case_id}/viz:sessions", {"run_dir": "C-run/case-1"}, token=tok)
    assert s2.get("alive"), s2
    owned.append(s2["id"])
    assert s2["port"] != s1["port"]
    r3 = call("POST", f"/cases/{case_id}/viz:sessions", {"run_dir": "C-run/case-1"}, token=tok)
    assert r3.get("_err") == 409, r3
    print("6. 并发上限 409 [OK]")

    # 关闭
    assert call("DELETE", f"/viz:sessions/{s1['id']}", token=tok)["ok"]
    assert call("DELETE", f"/viz:sessions/{s2['id']}", token=tok)["ok"]
    import time
    time.sleep(1)
    lst2 = call("GET", f"/cases/{case_id}/viz:sessions", token=tok)
    assert not lst2
    import socket as sk
    with sk.socket() as c:
        assert c.connect_ex(("127.0.0.1", s1["port"])) != 0, "端口应已释放"
    print("7. 关闭后进程终止+端口释放 [OK]")

    print("\n=== 可视化会话冒烟通过 ===")

finally:
    for sid in owned:
        closed = call("DELETE", f"/viz:sessions/{sid}", token=tok)
        assert closed.get("ok") or closed.get("_err") == 404, closed
