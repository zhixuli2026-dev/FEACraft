"""I01：Flotherm inspect 与真实异步 convert 作业 API。"""
import json
import os
import time
import urllib.error
import urllib.request

from app.services.flotherm_ir import build_ir

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
    result = call("POST", "/auth/register", {
        "email": f"flotherm-{label}-{time.time_ns()}@vibeflow.cn", "name": label,
        "password": "flotherm-pass-123"})
    return result["token"]


def wait_job(job_id, token):
    for _ in range(200):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("Flotherm 转换超时")


token = register("owner")
other = register("other")
workspace = call("POST", "/workspaces", {"name": f"flotherm-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"flotherm-{time.time_ns()}"}, token=token)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/demo.ecxml", "content": "<root><bbox>0 0 0 1 1 1</bbox></root>"}, token=token)["ok"]
inspection = call("POST", f"/cases/{case_id}/flotherm:inspect", {"paths": ["Ref/demo.ecxml"]}, token=token)
assert inspection["ok"] and inspection["kind"] == "ECXML", inspection
ir = build_ir("Ref/demo.ecxml", b"demo", [{"id": "chip", "type": "box", "material": "silicon",
              "mapping": "converted", "power_w": 5,
              "shape": {"kind": "box", "origin": [0, 0, 0], "dimensions": [1, 1, 1]}}],
              {"silicon": {"conductivity_w_mk": 130, "rho_cp": 1000}})
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/flotherm-ir.json", "content": json.dumps(ir)}, token=token)["ok"]
assert call("POST", f"/cases/{case_id}/flotherm:convert",
            {"ir_path": "Ref/flotherm-ir.json"}, token=other).get("_err") == 404
submitted = call("POST", f"/cases/{case_id}/flotherm:convert",
                 {"ir_path": "Ref/flotherm-ir.json", "output_dir": "C-run/flotherm"}, token=token)
assert submitted.get("job_id"), submitted
job = wait_job(submitted["job_id"], token)
assert job["status"] == "succeeded", job
assert "FLOTHERM_CONVERT_OK" in job["log_backlog"], job
paths = {item["path"] for item in call("GET", f"/cases/{case_id}/tree", token=token)["entries"]}
for path in ("C-run/flotherm/system/blockMeshDict", "C-run/flotherm/constant/fvOptions",
             "C-run/flotherm/flotherm-ir.json", "C-run/flotherm/conversion-coverage.json",
             "C-run/flotherm/Allrun.serial", "C-run/flotherm/preflight.json"):
    assert path in paths, path
assert call("POST", f"/cases/{case_id}/flotherm:convert",
            {"ir_path": "Ref/flotherm-ir.json", "output_dir": "C-run/flotherm"}, token=token).get("_err") == 409
print("I01 inspect, async convert, log/output open, cross-user and nonempty protection: OK")
print("\n=== I01 Flotherm 转换任务冒烟通过 ===")
