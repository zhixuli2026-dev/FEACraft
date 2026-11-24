"""D15：ParaView 5.11.2 pvpython/pvbatch 真实 API 与 server-manager golden。"""
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")
SCRIPT = (Path(__file__).resolve().parent / "examples" / "paraview" / "golden.py").read_text(encoding="utf-8")


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
        "email": f"pv-{label}-{time.time_ns()}@vibeflow.cn", "name": label, "password": "pv-pass-123"})
    return result["token"]


def wait_job(job_id, token):
    for _ in range(200):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("ParaView 作业超时")


def get_raw(case_id, path, token):
    request = urllib.request.Request(B + f"/cases/{case_id}/raw?path={path}",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


token = register("owner")
other = register("other")
workspace = call("POST", "/workspaces", {"name": f"pv-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"pv-{time.time_ns()}"}, token=token)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "D-post/paraview_golden.py", "content": SCRIPT}, token=token)["ok"]
assert call("POST", f"/cases/{case_id}/paraview:run",
            {"script_path": "D-post/paraview_golden.py"}, token=other).get("_err") == 404
assert call("POST", f"/cases/{case_id}/paraview:run",
            {"script_path": "D-post/paraview-sphere.vtp"}, token=token).get("_err") == 400
assert call("POST", f"/cases/{case_id}/paraview:run",
            {"script_path": "D-post/paraview_golden.py", "backend": "bad"}, token=token).get("_err") == 400
for backend in ("pvpython", "pvbatch"):
    submitted = call("POST", f"/cases/{case_id}/paraview:run",
                     {"script_path": "D-post/paraview_golden.py", "backend": backend}, token=token)
    assert submitted.get("backend") == backend, submitted
    job = wait_job(submitted["job_id"], token)
    assert job["status"] == "succeeded", job
    assert "PARAVIEW_SM_OK 226" in job["log_backlog"], job
    entries = call("GET", f"/cases/{case_id}/tree", token=token)["entries"]
    outputs = {item["path"]: item["size"] for item in entries}
    assert outputs["D-post/paraview-sphere.vtp"] > 10000, outputs
    assert outputs["D-post/paraview-sphere.png"] > 10000, outputs
    vtp = get_raw(case_id, "D-post/paraview-sphere.vtp", token)
    assert b"<VTKFile" in vtp and b"226" in vtp, vtp[:500]
    png = get_raw(case_id, "D-post/paraview-sphere.png", token)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", png[:20]
    print(f"1. {backend} server-manager/VTP/PNG/API [OK]")
print("D15 ParaView 5.11.2 pvpython+pvbatch golden: OK")
print("\n=== D15 ParaView 离线兼容冒烟通过 ===")
