"""LaTeX 表格生成器：转义、booktabs、边框、矩形与注入边界。"""
import json
import os
import urllib.error
import urllib.request

from app.services.latex_table import latex_escape, latex_table

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

text = latex_table(["A&B", "值"], [["x_1", "%$#\\{}~^"]], booktabs=True)
for marker in ("\\toprule", "\\midrule", "\\bottomrule", "A\\&B", "x\\_1", "\\%", "\\$", "\\#",
               "\\textbackslash{}", "\\{", "\\}", "\\textasciitilde{}", "\\textasciicircum{}"):
    assert marker in text, (marker, text)
assert text.splitlines()[4].endswith("\\\\"), text.splitlines()[4]
assert "\\write18" not in latex_escape(r"\write18{cmd}")
print("1. 特殊字符逐字转义、booktabs 和 LaTeX 行尾 [OK]")

bordered = latex_table(["左", "右"], [[1, 2], [3, 4]], booktabs=False, borders=True)
assert bordered.startswith("\\begin{tabular}{|l|l|}") and "\\hline" in bordered
aligned = latex_table(["A", "B"], [[1, 2]], align="cr")
assert "\\begin{tabular}{cr}" in aligned
print("2. 边框、align 与行格式 [OK]")

for headers, rows, kwargs in [
    ([], [], {}),
    (["A", "B"], [[1]], {}),
    (["A", "B"], [[1, 2]], {"align": "l"}),
    (["A", "B"], [[1, 2]], {"align": "xx"}),
    (["A", "B"], [[1, 2]], {"align": "lcr"}),
]:
    try:
        latex_table(headers, rows, **kwargs)
        raise AssertionError((headers, rows, kwargs))
    except ValueError:
        pass
print("3. 非矩形、空表头、错误 align 和行数边界 fail closed [OK]")

token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
response = call("POST", "/cases/1/latex:table", {
    "headers": ["A&B", "值"], "rows": [["x_1", "%$#"]],
    "booktabs": True, "borders": True, "align": "ll",
}, token=token)
assert "latex" in response, response
assert "\\toprule" in response["latex"] and "A\\&B" in response["latex"]
assert response["latex"].startswith("\\begin{tabular}{|l|l|}")
for body in ({"headers": ["A", "B"], "rows": [[1]], "booktabs": True, "borders": False, "align": None},
             {"headers": ["A", "B"], "rows": [[1, 2]], "booktabs": True, "borders": False, "align": "x"}):
    rejected = call("POST", "/cases/1/latex:table", body, token=token)
    assert rejected.get("_err") == 422, rejected
print("4. /latex:table 真实 API、转义、边框与非法输入 [OK]")
print("\n=== LaTeX 表格生成器冒烟通过 ===")
