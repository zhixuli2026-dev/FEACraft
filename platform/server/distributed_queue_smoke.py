"""J14：两个真实 arq worker 的 dequeue、日志、DB 状态与取消。"""
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


def register(label):
    return call("POST", "/auth/register", {"email": f"arq-{label}-{time.time_ns()}@vibeflow.cn",
                "name": label, "password": "arq-pass-123"})["token"]


def wait_job(job_id, token, terminal=("succeeded", "failed", "cancelled")):
    for _ in range(300):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in terminal:
            return job
        time.sleep(0.1)
    raise AssertionError("分布式作业超时")


if os.environ.get("VIBEFLOW_TEST_DISTRIBUTED") != "1":
    print("默认单进程环境：J14 分布式专项由 VIBEFLOW_TEST_DISTRIBUTED=1 显式启用 [SKIP]")
    raise SystemExit(0)


token = register("owner")
other = register("other")
workspace = call("POST", "/workspaces", {"name": f"arq-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"arq-{time.time_ns()}"}, token=token)
case_id = case["id"]
jobs = []
for index in range(4):
    submitted = call("POST", f"/cases/{case_id}/jobs",
                     {"command": f"python -c \"import time; time.sleep(0.2); print('DIST_OK_{index}')\""}, token=token)
    assert submitted.get("status") == "queued", submitted
    jobs.append(submitted["id"])
for job_id in jobs:
    job = wait_job(job_id, token)
    assert job["status"] == "succeeded", job
    assert job["log_backlog"].count("DIST_OK_") == 1, job
long_job = call("POST", f"/cases/{case_id}/jobs", {"command": 'python -c "import time; time.sleep(30)"'}, token=token)
for _ in range(200):
    current = call("GET", f"/jobs/{long_job['id']}", token=token)
    if current.get("status") == "running":
        break
    time.sleep(0.05)
else:
    raise AssertionError(current)
assert call("POST", f"/jobs/{long_job['id']}:cancel", {}, token=other).get("_err") == 404
result = call("POST", f"/jobs/{long_job['id']}:cancel", {}, token=token)
assert result["ok"] is True and result["status"] == "cancelled", result
job = wait_job(long_job["id"], token)
assert job["status"] == "cancelled", job
print("J14 two arq workers, exactly-once dequeue/log/DB and Redis cancel: OK")
print("\n=== J14 Redis/arq 分布式作业冒烟通过 ===")
