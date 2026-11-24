"""LaTeX 编译链冒烟（WSL TeX Live）：写 main.tex（中文+公式）→ latexmk 作业 → PDF 产物校验"""
import json
import os
import time
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


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


tok = call("POST", "/auth/register", {"email": f"latex-{time.time_ns()}@example.com",
           "password": "latex-test-123", "name": "latex fixture"})["token"]
workspace = call("POST", "/workspaces", {"name": "latex fixture"}, token=tok)
case_id = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": "latex"}, token=tok)["id"]

MAIN = r"""\documentclass{ctexart}
\usepackage{amsmath}
\title{cavity 冒烟报告}
\author{VibeFlow}
\begin{document}
\maketitle
顶盖驱动方腔的连续方程见式~\eqref{eq:cont}：
\begin{equation}
\nabla \cdot \mathbf{u} = 0 \label{eq:cont}
\end{equation}
\end{document}
"""

call("PUT", f"/cases/{case_id}/file",
     {"path": "E-report/latex/main.tex", "content": MAIN}, token=tok)
print("1. main.tex 已写（ctexart+中文+equation+label）")

j = call("POST", f"/cases/{case_id}/latex:build", {"main": "E-report/latex/main.tex"},
         token=tok)
print(f"2. 构建作业 #{j['job_id']} 提交，目标 {j['pdf']}")

t0 = time.time()
while time.time() - t0 < 240:
    st = call("GET", f"/jobs/{j['job_id']}", token=tok)
    if st["status"] in ("succeeded", "failed", "cancelled"):
        break
    time.sleep(2.5)
log = call("GET", f"/jobs/{j['job_id']}/log?offset=0", token=tok)["chunk"]
assert st["status"] == "succeeded", log[-800:]
print(f"3. latexmk 成功（{st.get('exit_code', 0)}）")

# PDF 产物校验：存在 + %PDF 头
tree = call("GET", f"/cases/{case_id}/tree", token=tok)
pdfs = [e["path"] for e in tree["entries"] if e["path"] == "E-report/latex/build/main.pdf"]
assert pdfs, "build/main.pdf 未产出"
req = urllib.request.Request(
    f"{B}/cases/{case_id}/raw?path=E-report/latex/build/main.pdf",
    headers={"Authorization": f"Bearer {tok}"})
head = urllib.request.urlopen(req).read(8)
assert head.startswith(b"%PDF"), head
print(f"4. PDF 产物校验 [OK]（%PDF 头正确，{[e for e in tree['entries'] if 'latex/build' in e['path']][:3]}）")

# 配方/路径校验：非 tex 拒绝 + 坏配方拒绝
import urllib.error
for bad, expect in [({"main": "E-report/report.md"}, 400), ({"recipe": "latex"}, 400)]:
    try:
        call("POST", f"/cases/{case_id}/latex:build", bad, token=tok)
        raise AssertionError(f"应拒绝 {bad}")
    except urllib.error.HTTPError as e:
        assert e.code == expect, (bad, e.code)
print("5. 参数校验 [OK]（非 .tex / 坏配方均 400）")

print("\n=== LaTeX 编译链冒烟通过 ===")
