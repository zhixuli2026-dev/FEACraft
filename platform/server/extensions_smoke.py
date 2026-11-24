"""CB16：extensions v1 命名空间/类型校验与注册删除生命周期。"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from app.services.casedict import CASE_DICT_TEMPLATE, parse_casedict
from app.services.casedict_schema import validate_case_dict_data

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
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


bad = CASE_DICT_TEMPLATE.replace("extensions\n{\n}", "extensions\n{\n    unknown true;\n}", 1)
assert any(error["code"] == "unsupported" for error in validate_case_dict_data(parse_casedict(bad)))
print("1. 原空块兼容，未知顶层字段拒绝 [OK]")

email = f"extensions-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "extensions-smoke", "password": "extensions-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"extensions-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "extensions-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"extensions-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"extensions-{time.time_ns()}"}, token=token)
case_id = case["id"]
created = call("PUT", f"/cases/{case_id}/caseDict/extensions/acme.mesh",
               {"version": "1.0", "enabled": True, "settings": {"order": 2}}, token=token)
assert created["entry"]["version"] == "1.0" and created["entry"]["settings"]["order"] == 2, created
revision = created["dict_revision"]
for namespace, body in (
    ("Bad Space", {"version": "1.0", "enabled": True, "settings": {}}),
    ("acme.empty", {"version": "", "enabled": True, "settings": {}}),
    ("acme.badsettings", {"version": "1.0", "enabled": True, "settings": []}),
):
    rejected = call("PUT", f"/cases/{case_id}/caseDict/extensions/{urllib.parse.quote(namespace, safe='')}", body, token=token)
    assert rejected.get("_err") == 422, rejected
    assert call("GET", f"/cases/{case_id}/caseDict", token=token)["dict_revision"] == revision
print("2. 注册成功，非法命名空间/空版本/settings 类型原子拒绝 [OK]")

parsed = call("GET", f"/cases/{case_id}/caseDict", token=token)["parsed"]["extensions"]
assert str(parsed["schemaVersion"]) == "1" and parsed["entries"]["acme.mesh"]["enabled"] in (True, "true", "True"), parsed
assert call("DELETE", f"/cases/{case_id}/caseDict/extensions/acme.mesh", token=other).get("_err") == 404
deleted = call("DELETE", f"/cases/{case_id}/caseDict/extensions/acme.mesh", token=token)
assert deleted["dict_revision"] == revision + 1
assert call("DELETE", f"/cases/{case_id}/caseDict/extensions/acme.mesh", token=token).get("_err") == 404
provenance = call("GET", f"/cases/{case_id}/provenance", token=token)
assert provenance["events"][0]["source"] == "extensions"
print("3. 删除/缺失/归属和 provenance 生命周期 [OK]")
print("\n=== extensions v1 schema 与生命周期冒烟通过 ===")
