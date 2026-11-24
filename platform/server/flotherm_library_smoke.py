"""I09：Flotherm 辅助库复制、wmake、安装和 CDLL 调用 golden。"""
import json
import os
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def register(label):
    return call("POST", "/auth/register", {"email": f"flib-{label}-{time.time_ns()}@vibeflow.cn",
                "name": label, "password": "flib-pass-123"})["token"]


def wait_job(job_id, token):
    for _ in range(300):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("辅助库编译超时")


token = register("owner")
other = register("other")
workspace = call("POST", "/workspaces", {"name": f"flib-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"flib-{time.time_ns()}"}, token=token)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/helper/flothermHelper.C",
            "content": 'extern "C" double vfFlothermPowerScale() { return 1.0; }\n'}, token=token)["ok"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/helper/Make/files",
            "content": "flothermHelper.C\n\nLIB = $(PWD)/libflothermHelper\n"}, token=token)["ok"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/helper/Make/options",
            "content": "EXE_INC =\nLIB_LIBS =\n"}, token=token)["ok"]
assert call("POST", f"/cases/{case_id}/flotherm:build-library", {"source_dir": "Ref/helper"}, token=other).get("_err") == 404
submitted = call("POST", f"/cases/{case_id}/flotherm:build-library", {"source_dir": "Ref/helper"}, token=token)
assert submitted.get("source_dir") == "UDF/flotherm-helper", submitted
job = wait_job(submitted["job_id"], token)
assert job["status"] == "succeeded", job
assert "libflothermHelper.so" in job["log_backlog"], job
assert call("POST", f"/cases/{case_id}/flotherm:build-library", {"source_dir": "Ref/helper"}, token=token).get("_err") == 409
command = ("python3 -c 'import os,ctypes; "
           "p=os.path.abspath(\"UDF/flotherm-helper/libflothermHelper.so\"); "
           "lib=ctypes.CDLL(p); lib.vfFlothermPowerScale.restype=ctypes.c_double; assert lib.vfFlothermPowerScale()==1.0; print(\"FLOTHERM_LIBRARY_OK\",p)'")
verify = call("POST", f"/cases/{case_id}/jobs", {"command": command, "runtime": "wsl"}, token=token)
loaded = wait_job(verify["id"], token)
assert loaded["status"] == "succeeded" and "FLOTHERM_LIBRARY_OK" in loaded["log_backlog"], loaded
print("I09 source copy, wmake libso in disposable case and CDLL call: OK")
print("\n=== I09 Flotherm 辅助库冒烟通过 ===")
