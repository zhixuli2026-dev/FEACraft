"""LaTeX 生命周期：源快照、禁 shell escape、超时、最新发布和失败保留。"""
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
        with urllib.request.urlopen(req, data, timeout=30) as response:
            payload = response.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token, timeout=180):
    for _ in range(timeout * 2):
        state = call("GET", f"/jobs/{job_id}", token=token)
        if state.get("status") in ("succeeded", "failed", "cancelled", "interrupted"):
            log = call("GET", f"/jobs/{job_id}/log?offset=0", token=token)["chunk"]
            return state, log
        time.sleep(0.5)
    raise TimeoutError(job_id)


def write_main(case_id, source, token):
    result = call("PUT", f"/cases/{case_id}/file",
                  {"path": "E-report/latex/main.tex", "content": source}, token=token)
    assert result.get("ok"), result


def start_build(case_id, token):
    result = call("POST", f"/cases/{case_id}/latex:build",
                  {"main": "E-report/latex/main.tex", "recipe": "pdflatex"}, token=token)
    assert result.get("job_id"), result
    return result


def pdf(case_id, token):
    result = call("GET", f"/cases/{case_id}/raw?path=E-report/latex/build/main.pdf", token=token, raw=True)
    assert isinstance(result, bytes) and result.startswith(b"%PDF"), result
    return result


def no_temp_dirs(case_id, token):
    paths = [item["path"] for item in call("GET", f"/cases/{case_id}/tree", token=token)["entries"]]
    assert not any(path.startswith(".vibeflow/latex-jobs/") for path in paths), paths
    assert not any(".latex-compile-" in path for path in paths), paths
    assert not any("shell-escape-escaped" in path for path in paths), paths


def document(title, marker, extra=""):
    return ("\\documentclass{article}\n\\pdfinfo{/Title (" + title + ")}\n"
            "\\begin{document}\n\\typeout{BUILD_SOURCE_" + marker + "}\n" + extra +
            "\nLifecycle " + marker + ".\n\\end{document}\n")


def main():
    token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    case = call("POST", "/workspaces/1/cases", {"name": f"latex-lifecycle-{time.time_ns()}"}, token=token)
    case_id = case["id"]
    attack = "\\immediate\\write18{kpsewhich article.cls > shell-escape-escaped}\n"
    write_main(case_id, document("BASELINE", "BASELINE", attack), token)
    baseline_job = start_build(case_id, token)
    baseline_state, baseline_log = wait_job(baseline_job["job_id"], token)
    assert baseline_state["status"] == "succeeded", baseline_log[-3000:]
    assert baseline_state["command"]=="Registered latexmk", baseline_state["command"]
    assert "-no-shell-escape" in baseline_log, baseline_log[-3000:]
    baseline_pdf = pdf(case_id, token)
    assert b"BASELINE" in baseline_pdf
    no_temp_dirs(case_id, token)
    print(f"1. 禁 shell escape、独立临时目录清理和成功发布 [OK] job={baseline_job['job_id']}")

    write_main(case_id, "\\documentclass{article}\n\\begin{document}\n\\badVibeFlowCommand\n\\end{document}\n", token)
    failed_job = start_build(case_id, token)
    failed_state, _ = wait_job(failed_job["job_id"], token)
    assert failed_state["status"] == "failed", failed_state
    assert pdf(case_id, token) == baseline_pdf, "失败构建覆盖了上一份成功 PDF"
    no_temp_dirs(case_id, token)
    print("2. 失败构建不覆盖上一份成功 PDF [OK]")

    stale_case = call("POST", "/workspaces/1/cases", {"name": f"latex-stale-{time.time_ns()}"}, token=token)["id"]
    slow = "\\newcount\\vfn\n\\vfn=0\n\\loop\\advance\\vfn by1\\ifnum\\vfn<30000000\\repeat\n"
    write_main(stale_case, document("OLDER", "OLDER", slow), token)
    older = start_build(stale_case, token)
    write_main(stale_case, document("NEWER", "NEWER"), token)
    newer = start_build(stale_case, token)
    newer_state, newer_log = wait_job(newer["job_id"], token)
    assert newer_state["status"] == "succeeded", newer_log[-2000:]
    assert b"NEWER" in pdf(stale_case, token)
    older_state, older_log = wait_job(older["job_id"], token)
    assert older_state["status"] == "succeeded", older_log[-2000:]
    assert "BUILD_SOURCE_OLDER" in older_log, "旧作业读取了后续保存的源文件，而非独立快照"
    final_pdf = pdf(stale_case, token)
    assert b"NEWER" in final_pdf and b"OLDER" not in final_pdf, "过期作业覆盖了最新 PDF"
    no_temp_dirs(stale_case, token)
    print(f"3. 源快照与过期结果丢弃 [OK] older={older['job_id']} newer={newer['job_id']}")

    timeout_case = call("POST", "/workspaces/1/cases", {"name": f"latex-timeout-{time.time_ns()}"}, token=token)["id"]
    infinite = "\\newcount\\vft\n\\vft=0\n\\loop\\ifnum\\vft<1\\repeat\n"
    write_main(timeout_case, document("TIMEOUT", "TIMEOUT", infinite), token)
    timeout_job = start_build(timeout_case, token)
    timeout_state, timeout_log = wait_job(timeout_job["job_id"], token, timeout=150)
    assert timeout_state["status"] == "failed", (timeout_state, timeout_log[-1000:])
    assert timeout_state["exit_code"] == 124, timeout_state
    diagnostics=call('GET',f'/cases/{timeout_case}/latex/errors?job_id={timeout_job["job_id"]}',token=token)
    assert any(item.get('context')=='native_compile_timeout' for item in diagnostics['items']),diagnostics
    no_temp_dirs(timeout_case, token)
    print(f"4. 真实 120 秒超时、exit=124 与清理 [OK] job={timeout_job['job_id']}")
    print("\n=== LaTeX 生命周期冒烟通过 ===")


main()
