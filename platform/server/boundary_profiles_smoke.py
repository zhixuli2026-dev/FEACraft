"""A07：时间 profile 生成器与真实 OpenFOAM dictionary 解析。"""
import json
import time
import urllib.error
import urllib.request

from app.services.boundary_profiles import constant, harmonic, pulse, ramp, table

B = "http://localhost:8000/api/v1"


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
    raise AssertionError("作业超时")


assert constant(1.5) == "constant 1.5;"
assert table([(1, 2), (0, 1)]) == "table ((0 1) (1 2));"
assert "sine" in harmonic(2, 3) and "frequency 3" in harmonic(2, 3)
assert "ramp" in ramp(0, 2, 1) and "square" in pulse(0, 1, 2, 5)
for invalid in (
    lambda: table([]), lambda: table([(0, 1), (0, 2)]),
    lambda: harmonic(1, 0), lambda: ramp(0, -1, 1), lambda: pulse(0, 1, 1, float("inf")),
):
    try:
        invalid()
        raise AssertionError("非法 profile 参数应拒绝")
    except ValueError:
        pass
print("1. 五类 profile、排序、重复/零频/负时长/Inf 边界 [OK]")

email = f"profiles-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "profiles-smoke", "password": "profiles-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"profiles-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"profiles-{time.time_ns()}"}, token=token)
case_id = case["id"]
dictionary = f'''boundaryField
{{
    inlet {{ type fixedValue; value {harmonic(2, 3)}; }}
    tableVar {{ type fixedValue; value {table([(0, 1), (1, 2)])}; }}
    rampVar {{ type fixedValue; value {ramp(0, 2, 1)}; }}
    pulseVar {{ type fixedValue; value {pulse(0, 1, 2, 5)}; }}
    constVar {{ type fixedValue; value {constant(1.5)}; }}
}}
'''
assert call("PUT", f"/cases/{case_id}/file", {"path": "B-mesh/profiles.dat", "content": dictionary}, token=token).get("ok") is True
command = "source /usr/lib/openfoam/openfoam2406/etc/bashrc && " + " && ".join(
    f"foamDictionary B-mesh/profiles.dat -entry boundaryField/{name}/value -value"
    for name in ("inlet", "tableVar", "rampVar", "pulseVar", "constVar"))
job = call("POST", f"/cases/{case_id}/jobs", {"type": "shell", "command": command, "runtime": "wsl"}, token=token)
result = wait_job(job["id"], token)
assert result["status"] == "succeeded", result
log = call("GET", f"/jobs/{job['id']}/log", token=token)["chunk"]
for expected in ("sine", "table", "ramp", "square", "constant 1.5"):
    assert expected in log, (expected, log)
print("2. OpenFOAM foamDictionary 实际解析五类 profile [OK]")
print("\n=== 时间边界 profile 生成与 OpenFOAM 语法冒烟通过 ===")
