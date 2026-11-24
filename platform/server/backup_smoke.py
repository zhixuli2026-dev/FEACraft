"""J12：workspace 真实备份/恢复、ID 重映射、活动拒绝与坏包回滚。"""
import asyncio
import base64
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from app.db import SessionLocal, engine
from app.models import ToolCall
from app.routers.agent import _append_event

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


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


def raw(method, path, token):
    request = urllib.request.Request(B + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def upload(path, data, token):
    boundary = f"----vf{uuid.uuid4().hex}"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"backup.zip\"\r\n"
            "Content-Type: application/zip\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(B + path, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(100):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("作业超时")


async def seed_conversation(case_id, token):
    cid = call("POST", "/conversations", {"case_id": case_id, "title": "backup-source",
                                         "model": "backup-model"}, token=token)["id"]
    async with SessionLocal() as db:
        await _append_event(db, cid, "message", {"role": "user", "content": "BACKUP_USER"})
        await _append_event(db, cid, "message", {"role": "assistant", "content": "BACKUP_ANSWER"})
        db.add(ToolCall(id=f"{cid}:backup-tool", conversation_id=cid, tool="list_files",
                        args="{}", status="executed", result="BACKUP_TOOL"))
        await db.commit()
    await engine.dispose()
    detail = call("GET", f"/conversations/{cid}", token=token)
    head = next(event for event in detail["events"] if event["payload"].get("content") == "BACKUP_ANSWER")
    fork = call("POST", f"/conversations/{cid}/fork", {"head_event_id": head["id"]}, token=token)
    return cid, head["id"], fork["id"]


email = f"backup-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "backup-smoke", "password": "backup-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"backup-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "backup-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"backup-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": "backup-case"}, token=token)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "D-post/marker.txt", "content": "BACKUP_FILE_MARKER"}, token=token).get("ok") is True
job = call("POST", f"/cases/{case_id}/jobs", {"type": "solve", "command": "cmd /c exit 0", "runtime": "windows"}, token=token)
assert wait_job(job["id"], token)["status"] == "succeeded"
cid, head_id, fork_id = asyncio.run(seed_conversation(case_id, token))
backup = raw("GET", f"/workspaces/{workspace['id']}/backup", token)
assert backup[:2] == b"PK" and b"mock-key" not in backup and b"key_ciphertext" not in backup
assert call("GET", f"/workspaces/{workspace['id']}/backup", token=other).get("_err") == 404
print("1. 空闲 workspace 生成含哈希备份，无密钥/跨用户泄露 [OK]")

restored = upload("/workspaces:restore", backup, token)
assert restored.get("cases"), restored
assert restored["cases"] == 1 and restored["jobs"] == 1 and restored["conversations"] == 2, restored
restored_case = call("GET", f"/workspaces/{restored['workspace_id']}/cases", token=token)[0]
assert restored_case["id"] != case_id and restored_case["name"] == "backup-case"
marker = call("GET", f"/cases/{restored_case['id']}/file?path=D-post%2Fmarker.txt", token=token)
assert marker["content"] == "BACKUP_FILE_MARKER"
restored_jobs = call("GET", f"/cases/{restored_case['id']}/jobs", token=token)
assert len(restored_jobs) == 1 and restored_jobs[0]["id"] != job["id"]
restored_log_key = restored_jobs[0]["log_path"]
assert restored_log_key.endswith(f"job-{restored_jobs[0]['id']}.log")
restored_log = call("GET", f"/jobs/{restored_jobs[0]['id']}/log", token=token)
assert restored_log["status"] == "succeeded"
restored_convs = call("GET", f"/conversations?caseId={restored_case['id']}", token=token)
assert len(restored_convs) == 2 and all(c["provider_profile_id"] is None for c in restored_convs)
restored_fork = next(c for c in restored_convs if c["forked_from_id"] is not None)
assert restored_fork["forked_from_id"] != cid
restored_source = next(c for c in restored_convs if c["forked_from_id"] is None)
exported = raw("GET", f"/conversations/{restored_source['id']}/export", token).decode()
assert "BACKUP_ANSWER" in exported and "BACKUP_TOOL" in exported
exported_fork = raw("GET", f"/conversations/{restored_fork['id']}/export", token).decode()
restored_head = next(event for event in call("GET", f"/conversations/{restored_source['id']}", token=token)["events"]
                     if event["payload"].get("content") == "BACKUP_ANSWER")["id"]
assert "BACKUP_ANSWER" in exported_fork
assert json.loads(exported_fork.splitlines()[0])["session"]["branch_head_event_id"] == restored_head != head_id
print("2. 新 workspace/case/job/conversation ID、文件/日志/分支/工具记录恢复 [OK]")

before = len(call("GET", "/workspaces", token=token))
with zipfile.ZipFile(io.BytesIO(backup)) as source:
    manifest = json.loads(source.read("manifest.json"))
manifest["files"][0]["data"] = base64.b64encode(b"corrupted").decode()
bad = io.BytesIO()
with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("manifest.json", json.dumps(manifest))
rejected = upload("/workspaces:restore", bad.getvalue(), token)
assert rejected.get("_err") == 400 and len(call("GET", "/workspaces", token=token)) == before
print("3. 坏 hash 恢复 400 且 DB 无残留 [OK]")

long_job = call("POST", f"/cases/{case_id}/jobs", {"type": "shell", "command": "python -c \"import time;time.sleep(10)\"", "runtime": "windows"}, token=token)
time.sleep(0.2)
active_reject = call("GET", f"/workspaces/{workspace['id']}/backup", token=token)
assert active_reject.get("_err") == 409, active_reject
call("POST", f"/jobs/{long_job['id']}:cancel", {}, token=token)
wait_job(long_job["id"], token)
print("4. 活动作业拒绝备份，取消后可继续 [OK]")
print("\n=== workspace 备份/恢复与回滚演练冒烟通过 ===")
