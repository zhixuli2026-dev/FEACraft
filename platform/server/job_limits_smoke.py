"""H18：作业墙钟与日志资源上限真实进程验证。"""
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
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(150):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("资源限制作业超时")


auth = call("POST", "/auth/register", {"email": f"limits-{time.time_ns()}@vibeflow.cn",
            "name": "limits", "password": "limits-pass-123"})
token = auth["token"]
workspace = call("POST", "/workspaces", {"name": f"limits-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"limits-{time.time_ns()}"}, token=token)
case_id = case["id"]
started = time.monotonic()
job = call("POST", f"/cases/{case_id}/jobs", {"command": 'python -c "import time; time.sleep(30)"',
           "timeout_seconds": 0.2}, token=token)
job = wait_job(job["id"], token)
assert job["status"] == "failed" and time.monotonic() - started < 8, job
assert "墙钟上限" in job["log_backlog"], job
job = call("POST", f"/cases/{case_id}/jobs", {"command": 'python -c "print(\'X\'*10000)"',
           "max_log_bytes": 1024}, token=token)
job = wait_job(job["id"], token)
assert job["status"] == "failed", job
assert "日志超过 1024 字节" in job["log_backlog"], job
log = call("GET", f"/jobs/{job['id']}/log", token=token)
assert log["offset"] <= 1024, log
job = call("POST", f"/cases/{case_id}/jobs", {"command": "echo NORMAL_JOB_OK"}, token=token)
job = wait_job(job["id"], token)
assert job["status"] == "succeeded" and "NORMAL_JOB_OK" in job["log_backlog"], job
print("H18 wall timeout, log byte cap and normal job: OK")
print("\n=== H18 作业资源限制冒烟通过 ===")
