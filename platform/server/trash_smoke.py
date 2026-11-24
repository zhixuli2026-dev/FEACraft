"""A23：case/workspace 回收站删除、恢复、归属、历史保留和活动作业保护。"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def call(method, path, body=None, token=None, expected=200):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            assert response.status == expected
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", "replace")
        assert error.code == expected, f"{method} {path}: {error.code} {payload}"
        return json.loads(payload)


def register(label):
    return call("POST", "/auth/register", {
        "email": f"{label}-{time.time_ns()}@vibeflow.cn", "name": label, "password": "trash-smoke-pass"})


registered = register("trash-smoke")
token = registered["token"]
workspace = call("POST", "/workspaces", {"name": f"trash-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": "trash-case"}, token=token)
marker = f"TRASH_RESTORE_{time.time_ns()}"
call("PUT", f"/cases/{case['id']}/file", {"path": "Ref/trash-marker.txt", "content": marker}, token=token)
job = call("POST", f"/cases/{case['id']}/jobs", {"type": "shell", "command": "echo TRASH_JOB_OK"}, token=token)
for _ in range(100):
    state = call("GET", f"/jobs/{job['id']}", token=token)
    if state["status"] not in ("queued", "running"):
        break
    time.sleep(0.1)
assert state["status"] == "succeeded", state
revision = call("GET", f"/cases/{case['id']}", token=token)["dict_revision"]

call("DELETE", f"/cases/{case['id']}", token=token)
call("GET", f"/cases/{case['id']}", token=token, expected=404)
call("GET", f"/cases/{case['id']}/raw?path=Ref%2Ftrash-marker.txt", token=token, expected=404)
assert all(item["id"] != case["id"] for item in call("GET", f"/workspaces/{workspace['id']}/cases", token=token))
trash = call("GET", "/trash", token=token)
assert any(item["id"] == case["id"] and item["name"] == "trash-case" for item in trash["cases"]), trash
other = register("trash-other")
call("POST", f"/cases/{case['id']}:restore", token=other["token"], expected=404)
print("1. case 删除后统一 404/列表隐藏/回收站可见，跨用户恢复 404 [OK]")

call("POST", f"/cases/{case['id']}:restore", token=token)
restored = call("GET", f"/cases/{case['id']}/file?path=Ref%2Ftrash-marker.txt", token=token)
assert restored["content"] == marker, restored
assert call("GET", f"/cases/{case['id']}", token=token)["dict_revision"] == revision
assert call("GET", f"/jobs/{job['id']}", token=token)["status"] == "succeeded"
print("2. case 恢复保持文件/revision/历史作业 [OK]")

second = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": "trash-case-2"}, token=token)
call("PUT", f"/cases/{second['id']}/file", {"path": "Ref/second.txt", "content": "SECOND"}, token=token)
call("DELETE", f"/workspaces/{workspace['id']}", token=token)
assert all(item["id"] != workspace["id"] for item in call("GET", "/workspaces", token=token))
call("GET", f"/cases/{case['id']}", token=token, expected=404)
call("GET", f"/cases/{second['id']}", token=token, expected=404)
call("POST", f"/workspaces/{workspace['id']}:restore", token=token)
restored_cases = call("GET", f"/workspaces/{workspace['id']}/cases", token=token)
assert {item["id"] for item in restored_cases} == {case["id"], second["id"]}, restored_cases
assert call("GET", f"/cases/{second['id']}/file?path=Ref%2Fsecond.txt", token=token)["content"] == "SECOND"
print("3. 整个工作区删除/恢复保留全部 case 文件 [OK]")

active = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": "trash-active"}, token=token)
long_job = call("POST", f"/cases/{active['id']}/jobs", {
    "type": "shell", "command": "ping -n 20 127.0.0.1 >nul"}, token=token)
for _ in range(50):
    active_state = call("GET", f"/jobs/{long_job['id']}", token=token)
    if active_state["status"] == "running":
        break
    time.sleep(0.1)
assert active_state["status"] == "running", active_state
conflict = call("DELETE", f"/cases/{active['id']}", token=token, expected=409)
assert "作业" in conflict["detail"], conflict
call("POST", f"/jobs/{long_job['id']}:cancel", {}, token=token)
for _ in range(400):
    cancelled = call("GET", f"/jobs/{long_job['id']}", token=token)
    if (cancelled["status"] == "cancelled" and cancelled["ended_at"] is not None
            and cancelled["attempt"]["phase"] == "cancelled"):
        break
    time.sleep(0.1)
assert (cancelled["status"] == "cancelled" and cancelled["ended_at"] is not None
        and cancelled["attempt"]["phase"] == "cancelled"), cancelled
call("DELETE", f"/cases/{active['id']}", token=token)
call("POST", f"/cases/{active['id']}:restore", token=token)
print("4. 活动作业拒绝删除，取消后可删除并恢复 [OK]")

print("\n=== case/workspace 回收站生命周期冒烟通过 ===")
