"""A22：case-events.jsonl 创建/修订时间线与只读分页。"""
import json
import os
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


email = f"case-events-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "case-events", "password": "case-events-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"case-events-other-{time.time_ns()}@vibeflow.cn",
                                        "name": "case-events-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"case-events-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"case-events-{time.time_ns()}"}, token=token)
case_id = case["id"]
created = call("GET", f"/cases/{case_id}/events", token=token)
assert created["total"] == 1 and created["events"][0]["type"] == "case.created", created
assert created["events"][0]["schema"] == "vibeflow.case-event.v1"
print("1. case.created 文件时间线 [OK]")

content = call("GET", f"/cases/{case_id}/caseDict", token=token)["content"]
updated = call("POST", f"/cases/{case_id}/caseDict:advance",
               {"state": "V", "confirmed": True, "expected_revision": 0}, token=token)
assert updated["workflow_state"] == "V" and updated["dict_revision"] == 1, updated
second = call("PUT", f"/cases/{case_id}/caseDict", {"content": content.replace("workflowState A;", "workflowState V;")}, token=token)
assert second["dict_revision"] == 2, second
invalid = call("PUT", f"/cases/{case_id}/caseDict", {"content": "broken { {"}, token=token)
assert invalid.get("_err") == 422, invalid
page = call("GET", f"/cases/{case_id}/events?offset=0&limit=2", token=token)
assert page["total"] == 3 and len(page["events"]) == 2 and page["has_more"] is True, page
assert page["events"][0]["revision"] == 2 and page["events"][1]["revision"] == 1, page["events"]
assert page["events"][1]["previous_state"] == "A" and page["events"][1]["workflow_state"] == "V"
older = call("GET", f"/cases/{case_id}/events?offset=2&limit=2", token=token)
assert len(older["events"]) == 1 and older["events"][0]["type"] == "case.created", older
assert older["has_more"] is False
assert call("GET", f"/cases/{case_id}/events", token=other).get("_err") == 404
print("2. 显式 A→V 与第二次修订、非法写入零新增、分页和跨用户 404 [OK]")
print("\n=== case_events 文件时间线冒烟通过 ===")
