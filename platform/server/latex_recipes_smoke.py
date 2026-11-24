"""LaTeX 三 recipe 与 biber 端到端编译验证。"""
import json
import os
import time
import urllib.error
import urllib.request

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
        with urllib.request.urlopen(req, data, timeout=90) as response:
            payload = response.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(240):
        time.sleep(1)
        state = call("GET", f"/jobs/{job_id}", token=token)
        if state["status"] in ("succeeded", "failed", "cancelled"):
            return state
    raise TimeoutError(job_id)


def build(case_id, recipe, main, token):
    result = call("POST", f"/cases/{case_id}/latex:build", {"main": main, "recipe": recipe}, token=token)
    assert result.get("job_id"), (recipe, result)
    state = wait_job(result["job_id"], token)
    log = call("GET", f"/jobs/{result['job_id']}/log?offset=0", token=token)["chunk"]
    assert state["status"] == "succeeded", (recipe, state, log[-2000:])
    pdf = call("GET", f"/cases/{case_id}/raw?path={result['pdf']}", token=token, raw=True)
    assert pdf.startswith(b"%PDF"), (recipe, result)
    return result["pdf"], log


def main():
    token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    for recipe in ("xelatex", "pdflatex", "lualatex"):
        case = call("POST", "/workspaces/1/cases", {"name": f"latex-{recipe}-{time.time_ns()}"}, token=token)
        case_id = case["id"]
        tex = "\\documentclass{article}\n\\usepackage{amsmath}\n\\begin{document}\nRecipe test $E=mc^2$.\n\\end{document}\n"
        result = call("PUT", f"/cases/{case_id}/file", {"path": "E-report/latex/main.tex", "content": tex}, token=token)
        assert result.get("ok"), result
        build(case_id, recipe, "E-report/latex/main.tex", token)
        print(f"1. {recipe} 独立 PDF 编译 [OK]")

    case = call("POST", "/workspaces/1/cases", {"name": f"latex-biber-{time.time_ns()}"}, token=token)
    case_id = case["id"]
    tex = "\\documentclass{article}\n\\usepackage[backend=biber,style=authoryear]{biblatex}\n\\addbibresource{refs.bib}\n\\begin{document}\nCitation \\cite{demo}.\n\\printbibliography\n\\end{document}\n"
    bib = "@article{demo, author={Doe, Jane}, title={A Test}, journal={Journal}, year={2026}}\n"
    for path, content in (("E-report/latex/main.tex", tex), ("E-report/latex/refs.bib", bib)):
        result = call("PUT", f"/cases/{case_id}/file", {"path": path, "content": content}, token=token)
        assert result.get("ok"), (path, result)
    _, log = build(case_id, "xelatex", "E-report/latex/main.tex", token)
    bbl = call("GET", f"/cases/{case_id}/raw?path=E-report/latex/build/main.bbl", token=token, raw=True).decode("utf-8", "replace")
    assert "demo" in bbl, bbl[-1000:]
    assert "biber" in log.lower(), log[-2000:]
    print("2. biber 生成 .bbl 且日志可追踪 [OK]")
    print("\n=== LaTeX 三 recipe 与 biber 冒烟通过 ===")


main()
