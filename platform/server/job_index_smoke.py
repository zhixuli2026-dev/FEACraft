"""H08：已有 FTS 索引的独立算例，作业产物在收尾后无需 watcher 即可搜索。"""
import json
import os
import time
import urllib.request

B = os.environ.get('VIBEFLOW_TEST_BASE', 'http://localhost:8000/api/v1')


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data, timeout=30) as response:
        return json.loads(response.read())


token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"job-index-{time.time_ns()}"}, token=token)
case_id = case["id"]
rebuilt = call("POST", f"/cases/{case_id}/index:rebuild", {}, token=token)
assert rebuilt["ok"] and rebuilt["entries"] > 0, rebuilt

marker = f"JOB_INDEX_{time.time_ns()}"
path = "Ref/job-index-marker.txt"
job = call("POST", f"/cases/{case_id}/jobs", {
    "type": "shell", "runtime": "windows",
    "command": f"echo {marker}> {path}",
}, token=token)

status = job
for _ in range(30):
    status = call("GET", f"/jobs/{job['id']}", token=token)
    if status["status"] not in ("queued", "running"):
        break
    time.sleep(0.25)
assert status["status"] == "succeeded", status

# 此 case 没有 /ws/events 订阅；持久终态消费者负责把新文件写入 FTS。
hits = []
for _ in range(40):
    result = call("GET", f"/cases/{case_id}/search?q={marker}", token=token)
    hits = result["hits"]
    if any(hit["path"] == path for hit in hits):
        break
    time.sleep(0.25)
assert any(hit["path"] == path for hit in hits), hits
print(f"Job finish reindex: OK (case={case_id}, job={job['id']}, path={path})")
