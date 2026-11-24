"""J11：组织审计中心、哈希链、过滤、脱敏与篡改检测。"""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
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


def register(label):
    result = call("POST", "/auth/register", {
        "email": f"audit-{label}-{time.time_ns()}@vibeflow.cn", "name": label,
        "password": "audit-pass-123"})
    return result["token"], result["user"]


admin, admin_user = register("admin")
member, member_user = register("member")
org_id = call("GET", "/org/mine", token=admin)["org"]["id"]
assert call("GET", f"/org/{org_id}/audit", token=member).get("_err") == 403
invited = call("POST", f"/org/{org_id}/members",
               {"email": member_user["email"], "role": "member"}, token=admin)
assert invited.get("membership"), invited
assert call("POST", f"/org/{org_id}/invitation:accept", {}, token=member)["ok"] is True
assert call("PATCH", f"/org/{org_id}/members/{member_user['id']}", {"role": "viewer"}, token=admin)["membership"]["role"] == "viewer"
workspace = call("POST", "/workspaces", {"name": f"audit-{time.time_ns()}"}, token=admin)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"audit-{time.time_ns()}"}, token=admin)
secret_command = "echo AUDIT_COMMAND_MUST_NOT_BE_LOGGED"
job = call("POST", f"/cases/{case['id']}/jobs", {"command": secret_command, "runtime": "windows"}, token=admin)
assert job.get("id"), job

page = call("GET", f"/org/{org_id}/audit?limit=100", token=admin)
assert page["chain_valid"] is True and page["total"] >= 4, page
actions = {event["action"] for event in page["events"]}
assert {"org.member.invited", "org.member.accepted", "org.member.role_changed", "job.submitted"} <= actions, actions
for event in page["events"]:
    assert event["previous_hash"] is not None and len(event["event_hash"]) == 64, event
    assert secret_command not in json.dumps(event), event
job_event = next(event for event in page["events"] if event["action"] == "job.submitted")
assert job_event["detail"]["command_sha256"] == hashlib.sha256(secret_command.encode()).hexdigest(), job_event
filtered = call("GET", f"/org/{org_id}/audit?action=org.member.role_changed", token=admin)
assert filtered["total"] == 1 and filtered["events"][0]["detail"]["role"] == "viewer", filtered
actor = call("GET", f"/org/{org_id}/audit?actor_user_id={member_user['id']}", token=admin)
assert actor["total"] == 1 and actor["events"][0]["action"] == "org.member.accepted", actor
paged = call("GET", f"/org/{org_id}/audit?limit=2", token=admin)
assert len(paged["events"]) == 2 and paged["has_more"] is True, paged

root = os.environ.get("VIBEFLOW_TEST_AUDIT_DIR")
if root:
    path = Path(root) / f"org-{org_id}.jsonl"
    raw = path.read_text(encoding="utf-8")
    assert secret_command not in raw
    lines = raw.splitlines()
    event = json.loads(lines[-1])
    event["actor_user_id"] = 999999
    lines[-1] = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tampered = call("GET", f"/org/{org_id}/audit", token=admin)
    assert tampered["chain_valid"] is False, tampered
    print("1. 测试组织审计篡改可检测 [OK]")
else:
    print("1. 主库不篡改；隔离模式另验 chain_valid=false [SKIP]")
print("J11 admin ACL, action/actor filters, pagination, hash-only command and chain: OK")
print("\n=== J11 组织审计中心冒烟通过 ===")
