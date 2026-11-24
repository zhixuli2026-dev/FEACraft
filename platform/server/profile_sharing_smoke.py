"""J09：组织共享 provider profile 的服务端使用与撤销。"""
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
    result = call("POST", "/auth/register", {"email": f"profile-share-{label}-{time.time_ns()}@vibeflow.cn",
                "name": label, "password": "profile-share-pass-123"})
    return result["token"], result["user"]


owner, _ = register("owner")
member, member_user = register("member")
other, _ = register("other")
org_id = call("GET", "/org/mine", token=owner)["org"]["id"]
assert call("POST", f"/org/{org_id}/members", {"email": member_user["email"], "role": "member"}, token=owner)["membership"]
assert call("POST", f"/org/{org_id}/invitation:accept", {}, token=member)["ok"] is True
profile = call("POST", "/provider-profiles", {"provider": "openai", "name": "org-profile",
               "base_url": "", "default_model": "mock-model", "api_key": "mock-secret-never-return"}, token=owner)
profile_id = profile["id"]
before_share = call("GET", "/provider-profiles", token=member)
assert not before_share, before_share
assert call("GET", f"/llm/models?provider_profile_id={profile_id}", token=member).get("_err") == 404
share = call("PUT", f"/org/{org_id}/provider-profiles/{profile_id}", {"enabled": True}, token=owner)
assert share["enabled"] is True
listed = call("GET", "/provider-profiles", token=member)
assert len(listed) == 1 and listed[0]["shared"] is True and listed[0]["has_key"] is True, listed
assert "mock-secret-never-return" not in json.dumps(listed)
models = call("GET", f"/llm/models?provider_profile_id={profile_id}", token=member)
assert models.get("models") is not None, models
assert call("PATCH", f"/provider-profiles/{profile_id}", {"name": "stolen"}, token=member).get("_err") == 404
assert call("DELETE", f"/provider-profiles/{profile_id}", token=member).get("_err") == 404
assert call("GET", f"/llm/models?provider_profile_id={profile_id}", token=other).get("_err") == 404
workspace = call("POST", "/workspaces", {"name": f"profile-share-{time.time_ns()}"}, token=member)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": "profile-shared-case"}, token=member)
conversation = call("POST", "/conversations", {"case_id": case["id"], "provider_profile_id": profile_id}, token=member)
assert conversation.get("provider_profile_id") == profile_id, conversation
revoked = call("DELETE", f"/org/{org_id}/provider-profiles/{profile_id}", token=owner)
assert revoked["enabled"] is False
assert not call("GET", "/provider-profiles", token=member)
assert call("GET", f"/llm/models?provider_profile_id={profile_id}", token=member).get("_err") == 404
print("J09 shared profile list/server use/conversation, owner-only mutation, nonmember and revoke: OK")
print("\n=== J09 组织共享 provider profile 冒烟通过 ===")
