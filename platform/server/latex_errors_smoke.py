"""LaTeX 日志结构化、真实失败作业诊断 API 与算例隔离。"""
import json
import os
import time
import urllib.error
import urllib.request

from app.services.latex_errors import parse_latex_errors

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


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


log = """Normal output.
! Undefined control sequence.
l.12 \\badcmd
(./main.tex
! LaTeX Error: Invalid option.
l.5
LaTeX Warning: Reference `fig:x' undefined on input line 7.
"""
items = parse_latex_errors(log)
assert len(items) == 3, items
assert items[0]["severity"] == "error" and items[0]["line"] == 12 and items[0]["file"] == "./main.tex", items[0]
assert items[1]["line"] == 5 and items[1]["severity"] == "error", items[1]
assert items[2]["severity"] == "warning" and items[2]["line"] == 7, items[2]
assert "Undefined control sequence" in items[0]["message"]
print("1. 传统错误、源文件行号、上下文和 warning [OK]")

file_line_items = parse_latex_errors("./main.tex:4: Undefined control sequence.\nl.4 \\badcmd\n")
assert len(file_line_items) == 1, file_line_items
assert file_line_items[0]["file"] == "./main.tex", file_line_items[0]
assert file_line_items[0]["line"] == 4 and file_line_items[0]["severity"] == "error", file_line_items[0]
assert parse_latex_errors("normal text only") == []
print("2. -file-line-error 格式与普通日志不误报 [OK]")

token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"latex-errors-{time.time_ns()}"}, token=token)
assert case.get("id"), case
case_id = case["id"]
main = """\\documentclass{article}
\\begin{document}
Before the error.
\\vibeflowUndefinedCommand
\\end{document}
"""
written = call("PUT", f"/cases/{case_id}/file",
               {"path": "E-report/latex/main.tex", "content": main}, token=token)
assert written.get("ok"), written
job = call("POST", f"/cases/{case_id}/latex:build",
           {"main": "E-report/latex/main.tex", "recipe": "pdflatex"}, token=token)
assert job.get("job_id"), job
status = None
for _ in range(90):
    status = call("GET", f"/jobs/{job['job_id']}", token=token)
    if status.get("status") in ("succeeded", "failed", "cancelled"):
        break
    time.sleep(1)
assert status and status["status"] == "failed", status

diagnostics = call("GET", f"/cases/{case_id}/latex/errors?job_id={job['job_id']}", token=token)
assert diagnostics.get("job_id") == job["job_id"], diagnostics
assert diagnostics.get("status") == "failed", diagnostics
errors = [item for item in diagnostics.get("items", []) if item["severity"] == "error"]
assert any(item.get("line") == 4 and (item.get("file") or "").endswith("main.tex")
           and "Undefined control sequence" in item["message"] for item in errors), diagnostics
print(f"3. 真实 pdflatex 失败作业 #{job['job_id']} 返回 main.tex:4 结构化错误 [OK]")

other_case = call("GET", f"/cases/1/latex/errors?job_id={job['job_id']}", token=token)
assert other_case.get("_err") == 404, other_case
not_latex = call("POST", f"/cases/{case_id}/jobs",
                 {"type": "shell", "command": "echo NOT_LATEX", "runtime": "windows"}, token=token)
assert not_latex.get("id"), not_latex
wrong_type = call("GET", f"/cases/{case_id}/latex/errors?job_id={not_latex['id']}", token=token)
assert wrong_type.get("_err") == 400, wrong_type
print("4. 跨算例 job_id 404、非 LaTeX 作业 400 [OK]")
print("\n=== LaTeX 错误结构化与诊断 API 冒烟通过 ===")
