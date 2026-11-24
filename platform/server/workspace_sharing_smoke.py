"""J07：组织共享 workspace viewer/member ACL 与撤销 golden。"""
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
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def register(label):
    result = call("POST", "/auth/register", {"email": f"share-{label}-{time.time_ns()}@vibeflow.cn",
                "name": label, "password": "share-pass-123"})
    return result["token"], result["user"]


owner, owner_user = register("owner")
member, member_user = register("member")
viewer, viewer_user = register("viewer")
other, _ = register("other")
org_id = call("GET", "/org/mine", token=owner)["org"]["id"]
assert call("POST", f"/org/{org_id}/billing:mock-checkout",
            {"plan_code": "pro", "confirmed": True}, token=owner)["subscription"]
for account, role in ((member_user, "member"), (viewer_user, "viewer")):
    assert call("POST", f"/org/{org_id}/members", {"email": account["email"], "role": role}, token=owner)["membership"]
for token, org in ((member, org_id), (viewer, org_id)):
    assert call("POST", f"/org/{org}/invitation:accept", {}, token=token)["ok"] is True
workspace = call("POST", "/workspaces", {"name": f"share-{time.time_ns()}"}, token=owner)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": "shared-case"}, token=owner)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/marker.txt", "content": "OWNER"}, token=owner)["ok"]
assert call("GET", f"/cases/{case_id}/tree", token=member).get("_err") == 404
share = call("PUT", f"/workspaces/{workspace['id']}/sharing",
             {"org_id": org_id, "role": "viewer", "enabled": True}, token=owner)
assert share["role"] == "viewer" and share["enabled"], share
for token in (member, viewer):
    listed = call("GET", "/workspaces", token=token)
    assert any(item["id"] == workspace["id"] and item["shared"] for item in listed), listed
    cases = call("GET", f"/workspaces/{workspace['id']}/cases", token=token)
    assert any(item["id"] == case_id for item in cases), cases
    assert call("GET", f"/cases/{case_id}", token=token)["id"] == case_id
    assert call("GET", f"/cases/{case_id}/file?path=Ref/marker.txt", token=token)["content"] == "OWNER"
    assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/member.txt", "content": "WRITE"}, token=token).get("_err") == 404
    assert call("POST", f"/cases/{case_id}/jobs", {"command": "echo SHARE_WRITE"}, token=token).get("_err") == 404
assert call("GET", f"/cases/{case_id}/tree", token=other).get("_err") == 404

share = call("PUT", f"/workspaces/{workspace['id']}/sharing",
             {"org_id": org_id, "role": "member", "enabled": True}, token=owner)
assert share["role"] == "member"
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/member.txt", "content": "MEMBER"}, token=member)["ok"] is True
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/viewer.txt", "content": "NO"}, token=viewer).get("_err") == 404
assert call("PUT", f"/workspaces/{workspace['id']}/sharing",
             {"org_id": org_id, "role": "member", "enabled": False}, token=owner)["enabled"] is False
for token in (member, viewer):
    assert call("GET", f"/cases/{case_id}/tree", token=token).get("_err") == 404
print("J07 shared listing/read, viewer read-only, member write, nonmember and revoke: OK")
print("\n=== J07 组织共享 ACL 冒烟通过 ===")
