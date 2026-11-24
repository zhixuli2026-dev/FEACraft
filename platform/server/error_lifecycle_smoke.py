"""A08/A14：errors.entries 全生命周期、summary 自动重算与原子负例。"""
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
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


email = f"error-life-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "error-life", "password": "error-life-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"error-life-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "error-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"error-life-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"error-life-{time.time_ns()}"}, token=token)
case_id = case["id"]
content = call("GET", f"/cases/{case_id}/caseDict", token=token)["content"]
errors_block = '''errors
{
    summary { total 1; open 1; blocking 1; resolved 0; lastErrorId "mesh-1"; }
    entries
    {
        mesh-1
        {
            title "Invalid mesh";
            stage mesh;
            category meshQuality;
            severity error;
            status open;
            blocking true;
            message "negative Jacobian";
        }
    }
}
'''
content = content.replace("errors\n{\n    // entries { }\n}", errors_block, 1)
assert call("PUT", f"/cases/{case_id}/caseDict", {"content": content}, token=token).get("ok") is True
path = f"/cases/{case_id}/caseDict/errors/mesh-1"
revision = call("GET", f"/cases/{case_id}/caseDict", token=token)["dict_revision"]

investigating = call("PATCH", path, {"status": "investigating"}, token=token)
assert investigating["summary"] == {"total": 1, "open": 1, "blocking": 1, "resolved": 0,
                                    "lastErrorId": "mesh-1"}, investigating
for body in ({"status": "fixProposed"}, {"status": "fixed"}, {"status": "verified"}):
    rejected = call("PATCH", path, body, token=token)
    assert rejected.get("_err") == 422, rejected
    assert call("GET", f"/cases/{case_id}/caseDict", token=token)["dict_revision"] == revision + 1
print("1. investigating 与缺 fixOptions/resolution/verification 的原子 422 [OK]")

proposed = call("PATCH", path, {"status": "fixProposed", "fixOptions": {"text": "refine mesh"}}, token=token)
assert proposed["summary"]["open"] == 1 and proposed["summary"]["blocking"] == 1
fixed = call("PATCH", path, {"status": "fixed", "resolution": "refined boundary layer"}, token=token)
assert fixed["summary"] == {"total": 1, "open": 0, "blocking": 0, "resolved": 1,
                            "lastErrorId": "mesh-1"}, fixed
verified = call("PATCH", path, {"status": "verified", "verification": "checkMesh all metrics passed"}, token=token)
assert verified["summary"]["resolved"] == 1 and verified["summary"]["open"] == 0
final = call("GET", f"/cases/{case_id}/caseDict", token=token)
entry = final["parsed"]["errors"]["entries"]["mesh-1"]
assert entry["status"] == "verified" and entry["resolution"] == "refined boundary layer"
assert entry["verification"] == "checkMesh all metrics passed"
assert entry["fixOptions"] == {"text": "refine mesh"}
assert "workflowState A;" in final["content"] and "// userIntent" in final["content"]
provenance = call("GET", f"/cases/{case_id}/provenance", token=token)
assert provenance["events"][0]["source"] == "error-lifecycle"
print("2. fixProposed→fixed→verified 全链、summary/文件/provenance 一致 [OK]")

reopened = call("PATCH", path, {"status": "open"}, token=token)
assert reopened["summary"]["open"] == 1 and reopened["summary"]["blocking"] == 1
assert call("PATCH", path, {"status": "invalid"}, token=token).get("_err") == 422
assert call("PATCH", f"/cases/{case_id}/caseDict/errors/missing", {"status": "open"}, token=token).get("_err") == 404
assert call("PATCH", path, {"status": "open"}, token=other).get("_err") == 404
print("3. 可重开、非法状态/缺失条目/跨用户边界 [OK]")
print("\n=== caseDict 错误生命周期与 summary 冒烟通过 ===")
