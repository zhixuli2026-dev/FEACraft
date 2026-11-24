"""MPI C06A/B：串行基线 → decomposePar → 2 子域 → reconstructPar → 场比较。"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from cae_fixtures import cavity_2d

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
FLOAT_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(120):
        time.sleep(1)
        state = call("GET", f"/jobs/{job_id}", token=token)
        if state["status"] in ("succeeded", "failed", "cancelled"):
            return state
    raise TimeoutError(job_id)


def floats(text):
    section = text.split("internalField", 1)[1].split("boundaryField", 1)[0]
    return [float(x) for x in FLOAT_RE.findall(section)]


def relative_diff(a, b):
    assert len(a) == len(b) and a, (len(a), len(b))
    denominator = max(1.0, max(abs(x) for x in a))
    return max(abs(x - y) for x, y in zip(a, b)) / denominator


tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
assert tok
workspace = call("POST", "/workspaces", {"name": f"mpi-{time.time_ns()}"}, token=tok)["id"]
cases = []
for label in ("serial", "parallel"):
    case_id = call("POST", f"/workspaces/{workspace}/cases", {"name": label}, token=tok)["id"]
    cases.append(case_id)
    for path, content in cavity_2d().items():
        saved = call("PUT", f"/cases/{case_id}/file", {"path": path, "content": content}, token=tok)
        assert "_err" not in saved, saved
    mesh = call("POST", f"/cases/{case_id}/jobs", {"type": "mesh", "runtime": "wsl",
        "command": "source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && blockMesh"}, token=tok)
    assert mesh.get("id"), mesh
    assert wait_job(mesh["id"], tok)["status"] == "succeeded"
serial_case_id, case_id = cases

# 串行基线
serial = call("POST", f"/cases/{serial_case_id}/jobs", {
    "type": "solve", "runtime": "wsl", "run_dir": "C-run/case-1",
    "command": "source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && icoFoam",
}, token=tok)
assert serial.get("id"), serial
serial_state = wait_job(serial["id"], tok)
assert serial_state["status"] == "succeeded", serial_state
serial_u = call("GET", f"/cases/{serial_case_id}/file?path=C-run/case-1/0.1/U", token=tok)["content"]
call("PUT", f"/cases/{case_id}/file", {"path": "Ref/serial-U.txt", "content": serial_u}, token=tok)
print("1. 串行 icoFoam 基线 [OK]")

# MPI 编排
assert not call("GET", f"/cases/{case_id}/file:version?path=C-run/case-1/0.1/U", token=tok)["exists"]
result = call("POST", f"/cases/{case_id}/mpi:run", {"subdomains": 2, "run_dir": "C-run/case-1"}, token=tok)
assert result.get("job_id"), result
parallel_state = wait_job(result["job_id"], tok)
assert parallel_state["status"] == "succeeded", parallel_state
children=parallel_state['attempt']['child_processes']['children']
native_children=[c for c in children.values() if c['purpose']=='mpi_run']
assert len(native_children)==1,native_children
native_identity=native_children[0]['identity']['native']['process']
assert native_identity['uid']!=0,native_identity
cleanup=parallel_state['attempt']['process_stop']
assert cleanup['verified'],cleanup
native_cleanup=native_children[0]['stop']
assert native_cleanup['verified'] and native_cleanup['native_state']=='exited',native_cleanup
status = call("GET", f"/cases/{case_id}/mpi:status?run_dir=C-run/case-1", token=tok)
assert status["complete"] and status["subdomains"] == 2, status
assert len(status["processor_dirs"]) == 2, status
parallel_u = call("GET", f"/cases/{case_id}/file?path=C-run/case-1/0.1/U", token=tok)["content"]
diff = relative_diff(floats(serial_u), floats(parallel_u))
assert diff <= 1e-5, f"串并行 U 场相对差异 {diff:.3g} 超过 1e-5"
print(f"2. 2 子域 MPI+重构，processor=2，U 场相对差异 {diff:.3g} [OK]")

# 参数和路径负向
assert call("POST", f"/cases/{case_id}/mpi:run", {"subdomains": 0}, token=tok).get("_err") in (400, 422)
assert call("POST", f"/cases/{case_id}/mpi:run", {"subdomains": 2, "run_dir": "../secret"}, token=tok).get("_err") == 400
print("3. 子域数量与路径负向 [OK]")

print("\n=== MPI 串并行与编排冒烟通过 ===")
