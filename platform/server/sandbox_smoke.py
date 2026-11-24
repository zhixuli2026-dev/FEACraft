"""J16/F58：真实 wsl-sandbox 作业、隔离、无网络与产物回收。"""
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
    return call("POST", "/auth/register", {"email": f"sandbox-{label}-{time.time_ns()}@vibeflow.cn",
                "name": label, "password": "sandbox-pass-123"})["token"]


def wait_job(job_id, token):
    for _ in range(200):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("沙箱作业超时")


token = register("owner")
other = register("other")
workspace = call("POST", "/workspaces", {"name": f"sandbox-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"sandbox-{time.time_ns()}"}, token=token)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/secret.txt", "content": "CASE_ONLY"}, token=token)["ok"]
assert call("POST", f"/cases/{case_id}/jobs", {"command": "id", "runtime": "wsl-sandbox"}, token=other).get("_err") == 404
command = ("id && cat Ref/secret.txt && test ! -d /mnt && test ! -d /home && test ! -d /root && "
           "printf SANDBOX_JOB_OK > D-post/result.txt")
submitted = call("POST", f"/cases/{case_id}/jobs", {"command": command, "runtime": "wsl-sandbox"}, token=token)
job = wait_job(submitted["id"], token)
assert job["status"] == "succeeded", job
assert "uid=65534(nobody)" in job["log_backlog"], job
assert call("GET", f"/cases/{case_id}/file?path=D-post/result.txt", token=token)["content"] == "SANDBOX_JOB_OK"

escape = call("POST", f"/cases/{case_id}/jobs",
              {"command": "cat /mnt/d/vibeflow_reverse/platform/server/.env", "runtime": "wsl-sandbox"}, token=token)
job = wait_job(escape["id"], token)
assert job["status"] == "failed" and "No such file" in job["log_backlog"], job
network = call("POST", f"/cases/{case_id}/jobs",
               {"command": "timeout 2 bash -c 'echo > /dev/tcp/1.1.1.1/80'", "runtime": "wsl-sandbox"}, token=token)
job = wait_job(network["id"], token)
assert job["status"] == "failed" and "Network is unreachable" in job["log_backlog"], job
assert call("POST", f"/cases/{case_id}/jobs", {"command": "id", "runtime": "docker"}, token=token).get("_err") == 400
print("J16 nobody, pivot_root, no host mounts, no network, staging output and ownership: OK")
print("\n=== J16 WSL namespace 硬隔离冒烟通过 ===")
