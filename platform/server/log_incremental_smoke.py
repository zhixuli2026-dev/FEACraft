"""H05：日志增量游标、前缀 fingerprint、半行续接与重写/截断拒绝。"""
import json
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_API_BASE", "http://localhost:8000") + "/api/v1"


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
            assert response.status == expected, response.status
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", "replace")
        assert error.code == expected, f"{method} {path}: {error.code} {payload}"
        return json.loads(payload)


def parse(case_id, token, offset=0, fingerprint=None, expected=200):
    query = {"path": "Ref/incremental.log", "incremental": "true", "offset": offset}
    if fingerprint is not None:
        query["fingerprint"] = fingerprint
    return call("GET", f"/cases/{case_id}/logs/parse?{urllib.parse.urlencode(query)}",
                token=token, expected=expected)


email = f"log-incremental-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "log-incremental", "password": "log-incremental-pass"})["token"]
workspace = call("POST", "/workspaces", {"name": f"log-incremental-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"log-incremental-{time.time_ns()}"}, token=token)
case_id = case["id"]
path = "Ref/incremental.log"
call("PUT", f"/cases/{case_id}/file", {"path": path, "content": "Time = "}, token=token)
empty = parse(case_id, token)["cursor"]
assert empty["offset"] == 0 and empty["fingerprint"] == hashlib.sha256(b"").hexdigest()
assert parse(case_id, token, 0, empty["fingerprint"])["cursor"] == empty
parse(case_id, token, 0, "0"*64, expected=400)
prefix = "Time = 0.1\nExecutionTime = 1.0 s\nsmoothSolver: Solving for Ux, Ini"
call("PUT", f"/cases/{case_id}/file", {"path": path, "content": prefix}, token=token)

first = parse(case_id, token, empty["offset"], empty["fingerprint"])
parse(case_id, token, 2, hashlib.sha256(b"Ti").hexdigest(), expected=400)
assert first["times"] == [0.1] and first["summary"]["execution_time_last_s"] == 1.0, first
assert first["residuals"] == {}, first
cursor = first["cursor"]
assert cursor["offset"] < len(prefix.encode()) and cursor["pending_bytes"] > 0, first
assert len(cursor["fingerprint"]) == 64, first
print("1. 初次增量解析保留未完整半行 [OK]")

suffix = ("tial residual = 0.1, Final residual = 0.01, No Iterations 2\n"
          "Time = 0.2\nDILU: Solving for Ux, Initial residual = 0.03, Final residual = 0.003, No Iterations 1\n"
          "ExecutionTime = 2.0 s\nEnd\n")
call("PUT", f"/cases/{case_id}/file", {"path": path, "content": prefix + suffix}, token=token)
second = parse(case_id, token, cursor["offset"], cursor["fingerprint"])
assert second["times"] == [0.2], second
assert second["residuals"]["Ux"] == [0.01, 0.003], second
assert second["summary"]["execution_time_last_s"] == 2.0 and second["summary"]["finished_cleanly"], second
assert second["cursor"]["previous_offset"] == cursor["offset"], second
assert second["cursor"]["offset"] == len((prefix + suffix).encode()), second
print("2. 半行续接只返回新增完整行，DILU 格式可解析 [OK]")

third = parse(case_id, token, second["cursor"]["offset"], second["cursor"]["fingerprint"])
assert third["cursor"]["unchanged"] is True, third
assert third["times"] == [] and third["residuals"] == {}, third
print("3. 相同游标空增量且 fingerprint 稳定 [OK]")

with ThreadPoolExecutor(max_workers=2) as pool:
    reads = [pool.submit(parse, case_id, token, cursor["offset"], cursor["fingerprint"]) for _ in range(2)]
    assert all(read.result() == second for read in reads)
other = call("POST", "/auth/register", {"email": f"other-{time.time_ns()}@example.com",
             "name": "Other reader", "password": "other-reader-pass"})["token"]
parse(case_id, other, cursor["offset"], cursor["fingerprint"], expected=404)

rewritten = "Time = 0.9\n" + (prefix + suffix).split("\n", 1)[1]
call("PUT", f"/cases/{case_id}/file", {"path": path, "content": rewritten}, token=token)
conflict = parse(case_id, token, second["cursor"]["offset"], second["cursor"]["fingerprint"], expected=409)
assert "fingerprint" in conflict["detail"], conflict
print("4. 已消费前缀被重写 → 409 [OK]")

call("PUT", f"/cases/{case_id}/file", {"path": path, "content": "Time = 0.1\n"}, token=token)
truncated = parse(case_id, token, second["cursor"]["offset"], second["cursor"]["fingerprint"], expected=409)
assert "截断" in truncated["detail"] or "fingerprint" in truncated["detail"], truncated
print("5. 日志截断 → 409 [OK]")

print("\n=== 日志增量 fingerprint 冒烟通过 ===")
