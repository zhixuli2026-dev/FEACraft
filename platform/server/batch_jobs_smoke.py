"""C12：多工况批量队列、并发上限、状态和取消真实 API 回归。"""
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
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_batch(case_id, batch_id, token, timeout=20):
    deadline = time.time() + timeout
    max_running = 0
    saw_bounded_queue = False
    while time.time() < deadline:
        batch = call("GET", f"/cases/{case_id}/job-batches/{batch_id}", token=token)
        counts = batch["counts"]
        max_running = max(max_running, counts["running"])
        saw_bounded_queue |= counts["running"] > 0 and counts["queued"] > 0
        if batch["status"] in {"succeeded", "failed", "cancelled"}:
            return batch, max_running, saw_bounded_queue
        time.sleep(0.03)
    raise AssertionError("批量作业超时")


email = f"batch-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "batch-smoke", "password": "batch-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"batch-other-{time.time_ns()}@vibeflow.cn",
                                        "name": "batch-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"batch-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"batch-{time.time_ns()}"}, token=token)
case_id = case["id"]
commands = ['python -c "import time; time.sleep(0.8); print(\'BATCH_OK\')"'] * 5
submitted = call("POST", f"/cases/{case_id}/jobs:batch",
                 {"commands": commands, "runtime": "windows", "max_concurrent": 2}, token=token)
assert submitted["status"] == "queued", submitted
assert submitted["counts"] == {"queued": 5, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0, "interrupted": 0}, submitted
batch_id = submitted["id"]
assert call("GET", f"/cases/{case_id}/job-batches/{batch_id}", token=other).get("_err") == 404
batch, max_running, saw_queue = wait_batch(case_id, batch_id, token)
assert batch["status"] == "succeeded", batch
assert batch["counts"]["succeeded"] == 5, batch
assert 1 <= max_running <= 2, (max_running, batch)
assert saw_queue, batch
listed = call("GET", f"/cases/{case_id}/job-batches", token=token)
assert any(item["id"] == batch_id and item["counts"]["succeeded"] == 5 for item in listed), listed

foreign_workspace = call("POST", "/workspaces", {"name": f"batch-foreign-{time.time_ns()}"}, token=other)
foreign_case = call("POST", f"/workspaces/{foreign_workspace['id']}/cases",
                    {"name": f"batch-foreign-{time.time_ns()}"}, token=other)
foreign_job = call("POST", f"/cases/{foreign_case['id']}/jobs",
                   {"command": "echo FOREIGN_BATCH_JOB", "runtime": "windows"}, token=other)
manifest_path = f".vibeflow/job-batches/{batch_id}.json"
manifest = json.loads(call("GET", f"/cases/{case_id}/file?path={manifest_path}", token=token)["content"])
manifest["job_ids"].append(foreign_job["id"])
assert call("PUT", f"/cases/{case_id}/file", {"path": manifest_path, "content": json.dumps(manifest)}, token=token).get("ok") is True
scoped = call("GET", f"/cases/{case_id}/job-batches/{batch_id}", token=token)
assert foreign_job["id"] not in {job["id"] for job in scoped["jobs"]}, scoped
assert scoped["total"] == 5, scoped

cancel_commands = ['python -c "import time; time.sleep(30)"'] * 4
cancelled = call("POST", f"/cases/{case_id}/jobs:batch",
                 {"commands": cancel_commands, "runtime": "windows", "max_concurrent": 1}, token=token)
cancel_id = cancelled["id"]
assert call("POST", f"/cases/{case_id}/job-batches/{cancel_id}:cancel", {}, token=other).get("_err") == 404
for _ in range(100):
    current = call("GET", f"/cases/{case_id}/job-batches/{cancel_id}", token=token)
    if current["counts"]["running"] == 1 and current["counts"]["queued"] == 3:
        break
    time.sleep(0.03)
else:
    raise AssertionError(current)
result = call("POST", f"/cases/{case_id}/job-batches/{cancel_id}:cancel", {}, token=token)
assert result["ok"] is True, result
batch, _, _ = wait_batch(case_id, cancel_id, token)
assert batch["status"] == "cancelled", batch
assert batch["counts"]["cancelled"] == 4, batch
time.sleep(0.3)
after = call("GET", f"/cases/{case_id}/job-batches/{cancel_id}", token=token)
assert after["counts"]["cancelled"] == 4, after

supervised = call("POST", f"/cases/{case_id}/jobs:batch",
                  {"commands": cancel_commands[:3], "runtime": "windows", "max_concurrent": 1}, token=token)
supervised_id = supervised["id"]
for _ in range(100):
    current = call("GET", f"/cases/{case_id}/job-batches/{supervised_id}", token=token)
    if current["counts"]["running"] == 1:
        break
    time.sleep(0.03)
else:
    raise AssertionError(current)
result = call("POST", "/runtime/processes:cancel", {"case_id": case_id}, token=token)
assert result["cancelled"].get("batch", 0) >= 1, result
batch, _, _ = wait_batch(case_id, supervised_id, token)
assert batch["status"] == "cancelled" and batch["counts"]["cancelled"] == 3, batch
active = call("GET", f"/runtime/processes?case_id={case_id}", token=token)["processes"]
assert not any(item["kind"] == "batch" for item in active), active

before = len(call("GET", f"/cases/{case_id}/jobs", token=token))
invalid = call("POST", f"/cases/{case_id}/jobs:batch",
               {"commands": ["echo ok", "format C:"], "runtime": "windows"}, token=token)
assert invalid.get("_err") == 400, invalid
assert len(call("GET", f"/cases/{case_id}/jobs", token=token)) == before
print("C12 batch concurrency=2, persistent status/list, cancel and validation: OK")
print("\n=== C12 多工况批量队列冒烟通过 ===")
