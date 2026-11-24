"""SyncTeX 路径映射、真实 PDF→源码查询与算例边界。"""
import json
import os
import time
import urllib.error
import urllib.request

from app.services.synctex_service import map_synctex_input, parse_synctex_edit

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def call(method, path, body=None, token=None, raw=False):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=30) as response:
            payload = response.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(360):
        state = call("GET", f"/jobs/{job_id}", token=token)
        if state.get("status") in ("succeeded", "failed", "cancelled", "interrupted"):
            log = call("GET", f"/jobs/{job_id}/log?offset=0", token=token)["chunk"]
            return state, log
        time.sleep(0.5)
    raise TimeoutError(job_id)


parsed = parse_synctex_edit("Output:main.pdf\nInput:./main.tex\nLine:83\nColumn:4\n")
assert parsed == {"input": "./main.tex", "line": 83, "column": 4}, parsed
assert map_synctex_input(parsed["input"], "E-report/latex/build/main.pdf") == "E-report/latex/main.tex"
absolute = "/mnt/d/work/.vibeflow/latex-jobs/job-123/chapters/intro.tex"
assert map_synctex_input(absolute, "E-report/latex/build/main.pdf") == "E-report/latex/chapters/intro.tex"
for invalid in ("/etc/passwd.tex", "../../../outside.tex"):
    try:
        map_synctex_input(invalid, "E-report/latex/build/main.pdf")
        raise AssertionError(invalid)
    except ValueError:
        pass
print("1. SyncTeX 记录解析、快照/相对路径映射与越界拒绝 [OK]")

token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"synctex-{time.time_ns()}"}, token=token)
case_id = case["id"]
tex = ("\\documentclass{article}\n\\usepackage{hyperref}\n\\begin{document}\n"
       "\\section{Introduction}\nIntro text.\\newpage\n\\section{Results}\nUniqueResultText and conclusion.\n\\end{document}\n")
written = call("PUT", f"/cases/{case_id}/file", {"path": "E-report/latex/main.tex", "content": tex}, token=token)
assert written.get("ok"), written
job = call("POST", f"/cases/{case_id}/latex:build", {"main": "E-report/latex/main.tex", "recipe": "pdflatex"}, token=token)
state, log = wait_job(job["job_id"], token)
assert state["status"] == "succeeded", log[-2000:]
sidecar = call("GET", f"/cases/{case_id}/raw?path=E-report/latex/build/main.synctex.gz", token=token, raw=True)
assert isinstance(sidecar, bytes) and sidecar.startswith(b"\x1f\x8b"), sidecar
print("2. 真实构建发布 gzip SyncTeX sidecar [OK]")

result = call("POST", f"/cases/{case_id}/synctex:query",
              {"pdf": "E-report/latex/build/main.pdf", "page": 2, "x": 134, "y": 157}, token=token)
assert result.get("file") == "E-report/latex/main.tex", result
assert result["line"] == 7, result
print(f"3. PDF 第 2 页坐标映射到 {result['file']}:{result['line']} [OK]")

invalid = call("POST", f"/cases/{case_id}/synctex:query",
               {"pdf": "E-report/latex/build/main.pdf", "page": 0, "x": 100, "y": 100}, token=token)
assert invalid.get("_err") == 422, invalid
plain = call("PUT", f"/cases/{case_id}/file", {"path": "E-report/build/plain.pdf", "content": "%PDF-1.4\n"}, token=token)
assert plain.get("ok"), plain
missing = call("POST", f"/cases/{case_id}/synctex:query",
               {"pdf": "E-report/build/plain.pdf", "page": 1, "x": 100, "y": 100}, token=token)
assert missing.get("_err") == 409, missing
other = call("POST", "/auth/register", {"email": f"synctex-other-{time.time_ns()}@vibeflow.cn",
                                        "name": "sync-other", "password": "other-pass-123"})
other_token = other.get("token")
assert other_token, other
foreign = call("POST", f"/cases/{case_id}/synctex:query",
               {"pdf": "E-report/latex/build/main.pdf", "page": 2, "x": 134, "y": 157}, token=other_token)
assert foreign.get("_err") == 404, foreign
print("4. 非法页码 422、缺 sidecar 409、跨用户 404 [OK]")
print("\n=== SyncTeX PDF→源码冒烟通过 ===")
