"""H19：真实历史作业在无 JobProcess 的 ASGI 进程中仍返回 DB ended_at。"""
import asyncio
from datetime import timezone
import json
import os
import time
import urllib.request

import httpx

from app.db import SessionLocal
from app.main import app
from app.models import Job

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


token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"job-history-{time.time_ns()}"}, token=token)
job = call("POST", f"/cases/{case['id']}/jobs", {
    "type": "shell", "runtime": "windows", "command": "echo JOB_HISTORY_OK",
}, token=token)
state = job
for _ in range(100):
    state = call("GET", f"/jobs/{job['id']}", token=token)
    if state["status"] not in ("queued", "running"):
        break
    time.sleep(0.1)
assert state["status"] == "succeeded", state
assert isinstance(state["ended_at"], (int, float)), state


async def check_history_process():
    async with SessionLocal() as db:
        persisted = await db.get(Job, job["id"])
        assert persisted is not None and persisted.ended_at is not None
        ended_at = persisted.ended_at
        expected = (ended_at if ended_at.tzinfo else ended_at.replace(tzinfo=timezone.utc)).timestamp()
        assert abs(state["ended_at"] - expected) < 1, "Live/history timestamps disagree"

    # 此测试进程没有运行该 job，registry 必然无 proc；路由必须回读 DB。
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://history-test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        detail = await client.get(f"/api/v1/jobs/{job['id']}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["ended_at"] == expected, detail.json()
        listed = await client.get(f"/api/v1/cases/{case['id']}/jobs", headers=headers)
        assert listed.status_code == 200, listed.text
        row = next(item for item in listed.json() if item["id"] == job["id"])
        assert row["ended_at"] == expected, row


asyncio.run(check_history_process())
print(f"Historical job ended_at from DB: OK (job={job['id']}, detail+list)")
