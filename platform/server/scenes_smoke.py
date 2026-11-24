"""scenes.py 执行上下文：__file__/VIBEFLOW_PARAVIEW_SCRIPT 与 ViewSize 禁止。"""
import json
import time
import urllib.error
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
    try:
        with urllib.request.urlopen(req, data, timeout=40) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case_id = 1
original = call("GET", f"/cases/{case_id}/file?path=D-post/scenes.py", token=tok)["content"]
sid = None


def write_script(content):
    result = call("PUT", f"/cases/{case_id}/file", {"path": "D-post/scenes.py", "content": content}, token=tok)
    assert result.get("ok"), result


def log_text():
    return call("GET", f"/cases/{case_id}/file?path=D-post/logs/viz_executor.log", token=tok).get("content", "")


def wait_log(marker, timeout=20):
    for _ in range(timeout):
        text = log_text()
        if marker in text:
            return text
        time.sleep(1)
    raise AssertionError(f"日志未出现 {marker!r}；末尾: {log_text()[-1600:]}")


try:
    session = call("POST", f"/cases/{case_id}/viz:sessions", {"run_dir": "C-run/case-1"}, token=tok)
    assert session.get("id"), session
    sid = session["id"]
    marker = f"VF_ctx_{time.time_ns()}"
    write_script(f"assert __file__ == VIBEFLOW_PARAVIEW_SCRIPT\nassert VIBEFLOW_PARAVIEW_SCRIPT.replace('\\\\', '/').endswith('/D-post/scenes.py')\nVF_surface('{marker}')\n")
    text = wait_log(marker)
    assert "exec ok" in text, text[-1600:]
    print("1. __file__ 与 VIBEFLOW_PARAVIEW_SCRIPT 注入并由 watcher 执行 [OK]")

    write_script("ViewSize(800, 600)\n")
    text = wait_log("ViewSize 不允许")
    assert "exec FAILED" in text, text[-1600:]
    print("2. ViewSize 在 exec 前明确禁止并记录失败 [OK]")

    write_script(f"VF_surface('{marker}')\n")
    text = wait_log(marker)
    assert "exec ok" in text, text[-1600:]
    print("3. 禁止错误后命名空间仍可继续增量执行 [OK]")
finally:
    call("PUT", f"/cases/{case_id}/file", {"path": "D-post/scenes.py", "content": original}, token=tok)
    if sid:
        call("DELETE", f"/viz:sessions/{sid}", token=tok)

print("\n=== scenes.py 执行上下文冒烟通过 ===")
