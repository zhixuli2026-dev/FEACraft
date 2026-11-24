"""J05：组织邀请、seat、角色、禁用与 admin console 真实 API 回归。"""
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")
PEER = os.environ.get("VIBEFLOW_TEST_PEER_BASE", B)


def call(method, path, body=None, token=None, *, base=B):
    request = urllib.request.Request(base + path, method=method)
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
        "email": f"org-{label}-{time.time_ns()}@vibeflow.cn", "name": label,
        "password": "org-member-pass-123"})
    return result["token"], result["user"]


admin, admin_user = register("admin")
member, member_user = register("member")
other, other_user = register("other")
viewer, viewer_user = register("viewer")
org_id = call("GET", "/org/mine", token=admin)["org"]["id"]
member_personal_org = call("GET", "/org/mine", token=member)["org"]["id"]

assert call("GET", f"/org/{org_id}/members", token=member).get("_err") == 403
invited = call("POST", f"/org/{org_id}/members",
               {"email": member_user["email"], "role": "member"}, token=admin)
assert invited["membership"]["role"] == "invited:member", invited
assert invited["seats"]["used"] == 2 and invited["seats"]["limit"] == 2, invited
assert call("GET", "/org/mine", token=member)["org"]["id"] == member_personal_org
pending = call("GET", "/org/invitations", token=member)
assert pending == [{"org_id": org_id, "org_name": invited["org"]["name"], "role": "member"}], pending
assert call("POST", f"/org/{org_id}/members",
            {"email": other_user["email"], "role": "viewer"}, token=admin).get("_err") == 429
assert call("POST", f"/org/{org_id}/invitation:accept", {}, token=other).get("_err") == 404
accepted = call("POST", f"/org/{org_id}/invitation:accept", {}, token=member)
assert accepted["ok"] is True and accepted["role"] == "member", accepted
members = call("GET", f"/org/{org_id}/members", token=admin)
assert members["seats"] == {"used": 2, "limit": 2, "active": 2, "invited": 0}, members
member_row = next(item for item in members["members"] if item["user_id"] == member_user["id"])
assert member_row["email"] == member_user["email"] and member_row["role"] == "member", member_row

assert call("PATCH", f"/org/{org_id}/members/{admin_user['id']}", {"role": "member"}, token=admin).get("_err") == 409
assert call("PATCH", f"/org/{org_id}/members/{member_user['id']}", {"role": "disabled"}, token=member).get("_err") == 403
changed = call("PATCH", f"/org/{org_id}/members/{member_user['id']}", {"role": "viewer"}, token=admin)
assert changed["membership"]["role"] == "viewer", changed
changed = call("PATCH", f"/org/{org_id}/members/{member_user['id']}", {"role": "disabled"}, token=admin)
assert changed["seats"]["used"] == 1 and changed["seats"]["active"] == 1, changed

invited_other = call("POST", f"/org/{org_id}/members",
                     {"email": other_user["email"], "role": "viewer"}, token=admin)
assert invited_other["seats"]["invited"] == 1, invited_other
assert call("PATCH", f"/org/{org_id}/members/{member_user['id']}", {"role": "member"}, token=admin).get("_err") == 429
assert call("POST", f"/org/{org_id}/invitation:decline", {}, token=other)["ok"] is True
invited_viewer = call("POST", f"/org/{org_id}/members",
                      {"email": viewer_user["email"], "role": "viewer"}, token=admin)
assert invited_viewer["seats"]["used"] == 2, invited_viewer
revoked = call("DELETE", f"/org/{org_id}/members/{viewer_user['id']}", token=admin)
assert revoked["ok"] is True and revoked["seats"]["used"] == 1, revoked
reactivated = call("PATCH", f"/org/{org_id}/members/{member_user['id']}", {"role": "member"}, token=admin)
assert reactivated["seats"]["active"] == 2, reactivated

assert call("POST", f"/org/{org_id}/members",
            {"email": viewer_user["email"], "role": "viewer"}, token=admin).get("_err") == 429
assert call("POST", f"/org/{org_id}/members",
            {"email": "missing-org-user@vibeflow.cn", "role": "viewer"}, token=admin).get("_err") == 404
assert call("POST", f"/org/{org_id}/members",
            {"email": member_user["email"], "role": "member"}, token=admin).get("_err") == 409

assert call("PATCH", f"/org/{org_id}/members/{member_user['id']}", {"role": "disabled"}, token=admin)["seats"]["used"] == 1
with ThreadPoolExecutor(max_workers=2) as pool:
    futures = [pool.submit(call, "POST", f"/org/{org_id}/members",
                           {"email": account["email"], "role": "viewer"}, admin, base=base)
               for account, base in zip((other_user, viewer_user), (B, PEER))]
    race_results = [future.result() for future in futures]
assert sorted(result.get("_err", 201) for result in race_results) == [201, 429], race_results
winner = next(result for result in race_results if result.get("_err") is None)
assert winner["seats"]["used"] == 2 and winner["seats"]["invited"] == 1, winner
assert call("DELETE", f"/org/{org_id}/members/{winner['membership']['user_id']}", token=admin)["seats"]["used"] == 1

enabled = call("PATCH", f"/org/{org_id}/members/{member_user['id']}", {"role": "admin"}, token=admin)
assert enabled["membership"]["role"] == "admin", enabled
with ThreadPoolExecutor(max_workers=2) as pool:
    attempts = (pool.submit(call, "PATCH", f"/org/{org_id}/members/{member_user['id']}",
                            {"role": "viewer"}, admin, base=B),
                pool.submit(call, "PATCH", f"/org/{org_id}/members/{admin_user['id']}",
                            {"role": "viewer"}, member, base=PEER))
    outcomes = [attempt.result() for attempt in attempts]
assert sorted(outcome.get("_err", 200) for outcome in outcomes) == [200, 403], outcomes
remaining_admin = admin if "_err" not in outcomes[0] else member
rows = call("GET", f"/org/{org_id}/members", token=remaining_admin, base=PEER)
assert sorted(row["role"] for row in rows["members"]) == ["admin", "viewer"], rows

print("J05 invite/accept/decline, seat reserve/release, roles/disable, last-admin and ACL: OK")
print(f"J05 concurrent invitations and admin role decisions: {'two API processes' if PEER != B else 'one API process'} [OK]")
print("\n=== J05 组织成员与 seat 管理冒烟通过 ===")
