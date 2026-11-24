"""A10：case 内 UDF D1–D5 scaffold→wmake/CDLL→icoFoam 全流程。"""
import json
import os
import time
import urllib.error
import urllib.request

from cae_fixtures import cavity_2d

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(300):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.2)
    raise AssertionError("UDF 作业超时")


email = f"udf-workflow-{time.time_ns()}@vibeflow.cn"
registration = call("POST", "/auth/register", {"email": email, "name": "udf-workflow", "password": "udf-workflow-pass"})
token = registration["token"]
user_id = registration["user"]["id"]
other = call("POST", "/auth/register", {"email": f"udf-workflow-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "udf-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"udf-workflow-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"udf-workflow-{time.time_ns()}"}, token=token)
case_id = case["id"]
print("UDF FIXTURE", user_id, workspace["id"], case_id)
for path, content in cavity_2d().items():
    if path.endswith("controlDict"):
        content += '\nfunctions\n{\n    // PRESERVE_EXISTING_FUNCTIONS_BLOCK\n}\n'
    result = call("PUT", f"/cases/{case_id}/file", {"path": path.replace("case-1/", "case-udf/"), "content": content}, token=token)
    assert "_err" not in result, result
mesh = call("POST", f"/cases/{case_id}/jobs", {"type": "mesh", "runtime": "wsl",
    "command": "source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-udf && blockMesh"}, token=token)
assert mesh.get("id"), mesh
assert wait_job(mesh["id"], token)["status"] == "succeeded"

scaffold = call("POST", f"/cases/{case_id}/udf/scaffold", {"name": "myFunctionObject", "objective": "首次执行输出 P0_UDF_OK"}, token=token)
assert scaffold["stages"] == ["D1", "D2", "D3"] and len(scaffold["files"]) == 9, scaffold
assert call("POST", f"/cases/{case_id}/udf/scaffold", {"name": "myFunctionObject", "objective": "x"}, token=token).get("_err") == 409
assert call("POST", f"/cases/{case_id}/udf/scaffold", {"name": "myFunctionObject", "objective": "x"}, token=other).get("_err") == 404
for path in ("D1_REQUIREMENTS.md", "D2_DESIGN.md", "D3_IMPLEMENTATION.md", "D4_BUILD.md", "D5_INTEGRATION.md"):
    assert call("GET", f"/cases/{case_id}/file?path=UDF%2FmyFunctionObject%2F{path}", token=token).get("content")
print("1. D1–D3 文档/源码生成、不覆盖、归属 [OK]")

build = call("POST", f"/cases/{case_id}/udf:build", {"name": "myFunctionObject"}, token=token)
build_job = wait_job(build["job_id"], token)
assert build_job["status"] == "succeeded", build_job
print("2. D4 wmake libso + ctypes.CDLL 动态加载 [OK]")

control_check = call("GET", f"/cases/{case_id}/file?path=C-run%2Fcase-udf%2Fsystem%2FcontrolDict", token=token)
assert "controlDict" in control_check.get("content", "") or "startTime" in control_check.get("content", ""), control_check
integrated = call("POST", f"/cases/{case_id}/udf:integrate", {"name": "myFunctionObject", "run_dir": "C-run/case-udf"}, token=token)
assert integrated.get("job_id"), integrated
integrated_job = wait_job(integrated["job_id"], token)
assert integrated_job["status"] == "succeeded", integrated_job
log = call("GET", f"/jobs/{integrated_job['id']}/log", token=token)["chunk"]
assert "P0_UDF_OK" in log, log[-2000:]
assert call("GET", f"/cases/{case_id}/file?path=C-run/case-udf/0.1/U", token=token).get("content"), "D5 未生成新时刻 U"
control = call("GET", f"/cases/{case_id}/file?path=C-run/case-udf/system/controlDict", token=token)["content"]
assert "PRESERVE_EXISTING_FUNCTIONS_BLOCK" in control and control.count("functions\n{") == 1, control
assert call("POST", f"/cases/{case_id}/udf:integrate", {"name": "myFunctionObject", "run_dir": "C-run/case-udf"}, token=token).get("_err") == 409
print("3. D5 controlDict 注册、icoFoam 执行 P0_UDF_OK 和新时刻产物 [OK]")
print("\n=== case 内 UDF D1–D5 全流程冒烟通过 ===")
