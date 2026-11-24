"""工作区索引冒烟：重建 → 状态 → 搜索（名字+内容 snippet）→ 写文件增量 → 删除增量"""
import json
import time
import urllib.request

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


tok = call("POST", "/auth/login",
           {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case_id = next(c["id"] for c in call("GET", "/workspaces/1/cases", token=tok)
               if c["name"] == "cavity-demo")

# 1. 重建
r = call("POST", f"/cases/{case_id}/index:rebuild", {}, token=tok)
assert r["ok"] and r["entries"] > 20, r
print(f"1. 重建 [OK] {r['entries']} 条目（内容索引 {r['content_entries']}，{r['elapsed_ms']}ms）")

# 2. 状态
st = call("GET", f"/cases/{case_id}/index/status", token=tok)
assert st["indexed"] and st["entries"] == r["entries"]
print("2. 状态 [OK]")

# 3. 名字搜索
s = call("GET", f"/cases/{case_id}/search?q=caseDict", token=tok)
assert s["ready"] and any(h["path"] == "0-caseDict/caseDict" for h in s["hits"]), s["hits"][:3]
print(f"3. 名字搜索 [OK] q=caseDict → {len(s['hits'])} 命中")

# 4. 内容搜索（cavity 字典里有 workflowState）
s2 = call("GET", f"/cases/{case_id}/search?q=workflowState", token=tok)
assert any(h.get("via") == "content" or "caseDict" in h["path"] for h in s2["hits"]), s2["hits"][:3]
print(f"4. 内容搜索 [OK] q=workflowState → {len(s2['hits'])} 命中")

# 5. 增量：写新文件后立刻能搜到（免重建）
marker = f"MARKER_{int(time.time())}"
call("PUT", f"/cases/{case_id}/file",
     {"path": "Ref/index-probe.md", "content": f"增量测试 {marker}"}, token=tok)
s3 = call("GET", f"/cases/{case_id}/search?q={marker}", token=tok)
assert any("index-probe.md" in h["path"] for h in s3["hits"]), s3["hits"]
print("5. 写文件增量 [OK]（免重建即搜到）")

# 6. 删除增量
call("DELETE", f"/cases/{case_id}/file?path=Ref/index-probe.md", token=tok)
s4 = call("GET", f"/cases/{case_id}/search?q={marker}", token=tok)
assert not any("index-probe.md" in h["path"] for h in s4["hits"]), s4["hits"]
print("6. 删除增量 [OK]（搜不到了）")

print("\n=== 工作区索引冒烟通过 ===")
