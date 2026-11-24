"""VF_* 逐函数契约：真实 scenes watcher、场景状态、截图与错误恢复。"""
import json
import time
import urllib.error
import urllib.request

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None, raw=False):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=40) as response:
            content = response.read()
            return (response.status, content) if raw else json.loads(content)
    except urllib.error.HTTPError as error:
        content = error.read()
        return (error.code, content) if raw else {"_err": error.code, "detail": content.decode("utf-8", "replace")}


tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case_id = 1
original = call("GET", f"/cases/{case_id}/file?path=D-post/scenes.py", token=tok)["content"]
sid = None
marker = f"contract_done_{time.time_ns()}"


def write_script(content):
    result = call("PUT", f"/cases/{case_id}/file", {"path": "D-post/scenes.py", "content": content}, token=tok)
    assert result.get("ok"), result


def log_text():
    return call("GET", f"/cases/{case_id}/file?path=D-post/logs/viz_executor.log", token=tok).get("content", "")


def wait_log(marker, timeout=30):
    for _ in range(timeout):
        text = log_text()
        if marker in text:
            return text
        time.sleep(1)
    raise AssertionError(f"日志未出现 {marker!r}；末尾: {log_text()[-2000:]}")


try:
    session = call("POST", f"/cases/{case_id}/viz:sessions", {"run_dir": "C-run/case-1"}, token=tok)
    assert session.get("id"), session
    sid = session["id"]
    script = f"""VF_load('C-run/case-1')
VF_surface('VF_A', 'p')
VF_slice('VF_B', 'p', 'Y', 0.5)
VF_contour('VF_C', 'p', 0.5)
VF_stream('VF_D', 'U', 5)
VF_glyph('VF_E', 'U', 0.5)
VF_color('VF_A', 'Umag')
VF_time(0.2)
VF_reset_camera()
shot = VF_shot('figures/contract.png')
assert shot.endswith('contract.png')
expected = {{'VF_A', 'VF_B', 'VF_C', 'VF_D', 'VF_E'}}
assert expected.issubset(set(VF_list())), VF_list()
VF_remove('VF_A')
assert 'VF_A' not in VF_list()
VF_surface('{marker}')
"""
    write_script(script)
    text = wait_log(marker)
    for expected in ("+VF_A surface p", "+VF_B slice p Y@0.5", "+VF_C contour p iso=0.5",
                     "+VF_D stream seeds=5", "+VF_E glyph scale=0.5", "~VF_A color Umag",
                     "time -> 0.2", "-VF_A", "screenshot -> D-post/figures/contract.png"):
        assert expected in text, (expected, text[-2400:])
    status, image = call("GET", f"/cases/{case_id}/raw?path=D-post/figures/contract.png", token=tok, raw=True)
    assert status == 200 and image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) > 1000
    print("1. load/surface/slice/contour/stream/glyph/color/remove/time/reset_camera/shot/list 全通过 [OK]")

    write_script("VF_load('../')\n")
    text = wait_log("exec FAILED")
    assert "不是有效 OpenFOAM 算例目录" in text, text[-1600:]
    write_script(f"VF_surface('{marker}')\n")
    text = wait_log(marker)
    assert "exec ok" in text, text[-1600:]
    print("2. 非法 load 有明确错误，watcher 后续仍可执行 [OK]")
finally:
    call("PUT", f"/cases/{case_id}/file", {"path": "D-post/scenes.py", "content": original}, token=tok)
    if sid:
        call("DELETE", f"/viz:sessions/{sid}", token=tok)

print("\n=== VF_* 逐函数契约冒烟通过 ===")
