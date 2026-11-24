"""C08：SU2 8.5.0 官方 Windows OpenMP 真实 CFD golden。"""
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
EXAMPLES = Path(__file__).resolve().parent / "examples" / "su2"
CONFIG = (EXAMPLES / "inv_NACA0012.cfg").read_text().replace("ITER= 250", "ITER= 3").replace(
    "MESH_FILENAME= mesh_NACA0012_inv.su2", "MESH_FILENAME= C-run/su2/naca0012.su2")
MESH = (EXAMPLES / "mesh_NACA0012_inv.su2").read_text()


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
    for _ in range(150):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("SU2 作业超时")


email = f"su2-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "su2-smoke", "password": "su2-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"su2-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "su2-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"su2-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"su2-{time.time_ns()}"}, token=token)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "C-run/su2/naca0012.cfg", "content": CONFIG}, token=token).get("ok") is True
assert call("PUT", f"/cases/{case_id}/file", {"path": "C-run/su2/naca0012.su2", "content": MESH}, token=token).get("ok") is True
assert call("POST", f"/cases/{case_id}/su2:solve", {"config_path": "C-run/su2/naca0012.su2"}, token=token).get("_err") == 400
assert call("POST", f"/cases/{case_id}/su2:solve", {"config_path": "C-run/su2/naca0012.cfg"}, token=other).get("_err") == 404
submitted = call("POST", f"/cases/{case_id}/su2:solve", {"config_path": "C-run/su2/naca0012.cfg"}, token=token)
assert submitted["backend"] == "su2", submitted
job = wait_job(submitted["job_id"], token)
assert job["status"] == "succeeded", job
files = call("GET", f"/cases/{case_id}/tree", token=token)["entries"]
paths = {item["path"] for item in files}
assert "restart_flow.dat" in paths, sorted(paths)
assert "flow.vtu" in paths, sorted(paths)
print("SU2 8.5.0 official NACA0012 solve, restart_flow.dat and flow.vtu: OK", sorted(path for path in paths if path in {"restart_flow.dat", "flow.vtu", "surface_flow.csv", "history.csv"}))
print("\n=== SU2 真实后端与输出冒烟通过 ===")
