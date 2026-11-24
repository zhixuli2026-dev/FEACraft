"""F19：read/list/search 直接执行与统一重型目录排除。"""
import json
import time
import urllib.error
import urllib.request

from app.services.tools import ToolExecutor

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
        with urllib.request.urlopen(req, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"readonly-tools-{time.time_ns()}"}, token=token)
prefix = case.get("s3_prefix") or f"workspaces/1/cases/{case['id']}"
for path, content in {
    "A-geo/note.txt": "needle alpha\n",
    "D-post/result.txt": "needle alpha\n",
    "node_modules/pkg/skip.txt": "needle hidden\n",
    "build/output.txt": "needle hidden\n",
    ".vibeflow/jobs/log.txt": "needle hidden\n",
}.items():
    written = call("PUT", f"/cases/{case['id']}/file", {"path": path, "content": content}, token=token)
    assert written.get("ok"), (path, written)
executor = ToolExecutor(prefix)
read = executor.execute("read_text_file", {"path": "A-geo/note.txt"})
assert read["ok"] and "needle alpha" in read["result"], read
listing = executor.execute("list_files", {})
assert listing["ok"], listing
assert "A-geo/note.txt" in listing["result"] and "D-post/result.txt" in listing["result"], listing["result"]
assert "node_modules" not in listing["result"] and "build/output.txt" not in listing["result"], listing["result"]
assert ".vibeflow/jobs/log.txt" not in listing["result"], listing["result"]
internal = executor.execute("list_files", {"include_internal": True})["result"]
assert ".vibeflow/jobs/log.txt" in internal, internal
assert "node_modules" not in internal and "build/output.txt" not in internal, internal
search = executor.execute("search_text", {"query": "needle"})["result"]
assert "A-geo/note.txt:1: needle alpha" in search and "D-post/result.txt:1: needle alpha" in search, search
assert "needle hidden" not in search and "node_modules" not in search and "build/output.txt" not in search, search
print("1. read/list/search 与统一 node_modules/build/.vibeflow 排除 [OK]")

scoped = executor.execute("list_files", {"subdir": "A-geo"})["result"]
assert "A-geo/note.txt" in scoped and "D-post/result.txt" not in scoped, scoped
scoped_search = executor.execute("search_text", {"query": "needle", "subdir": "A-geo"})["result"]
assert "A-geo/note.txt" in scoped_search and "D-post/result.txt" not in scoped_search, scoped_search
print("2. subdir 范围与 include_internal 只放开内部目录 [OK]")

missing = executor.execute("read_text_file", {"path": "../outside.txt"})
assert not missing["ok"], missing
missing_file = executor.execute("read_text_file", {"path": "A-geo/missing.txt"})["result"]
assert "文件不存在" in missing_file, missing_file
print("3. 越界和不存在路径 fail closed [OK]")
print("\n=== 只读工具与重型排除冒烟通过 ===")
