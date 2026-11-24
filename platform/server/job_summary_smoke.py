"""H06：作业结束自动写回 Job.parsed_summary。"""
import json
import os
import sys
import time
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(request, data, timeout=30) as response:
        return json.loads(response.read())


email = f"job-summary-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "job-summary", "password": "job-summary-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"job-summary-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"job-summary-{time.time_ns()}"}, token=token)
code = ("import sys;sys.stdout.write('Time = 0.1\\nCourant Number mean: 0.1 max: 0.5\\n"
        "smoothSolver: Solving for Ux, Initial residual = 0.1, Final residual = 0.01, No Iterations 2\\n"
        "ExecutionTime = 1.2 s\\nWarning: test warning\\nTime = 0.2\\nEnd\\n')")
command = f'"{sys.executable}" -c "{code}"'
job = call("POST", f"/cases/{case['id']}/jobs", {"type": "solve", "command": command, "runtime": "windows"}, token=token)
job_id = job["id"]
state = {}
for _ in range(100):
    state = call("GET", f"/jobs/{job_id}", token=token)
    if state["status"] in ("succeeded", "failed", "cancelled"):
        break
    time.sleep(0.1)
assert state["status"] == "succeeded", state
summary = state["parsed_summary"]
assert summary["schema"] == "vibeflow.solver-log.v1", summary
assert summary["summary"]["last_time"] == 0.2 and summary["summary"]["courant_max_last"] == 0.5, summary
assert summary["summary"]["execution_time_last_s"] == 1.2 and summary["summary"]["finished_cleanly"] is True, summary
assert summary["summary"]["warnings"] == 1 and summary["summary"]["fields"] == ["Ux"], summary
assert summary["residuals"]["Ux"] == [0.01], summary["residuals"]
print("1. 作业终态自动回写 schema/时刻/Courant/残差/警告/执行时间 [OK]")
print("\n=== Job.parsed_summary 自动结构化冒烟通过 ===")
