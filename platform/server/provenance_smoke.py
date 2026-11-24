"""A18：intent/开放问题/假设/决策的 revision 哈希链与篡改检测。"""
import json
import os
import time
import urllib.error
import urllib.parse
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


def raw_text(case_id, path, token):
    request = urllib.request.Request(
        f"{B}/cases/{case_id}/raw?path={urllib.parse.quote(path, safe='')}",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


email = f"provenance-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "provenance-smoke", "password": "provenance-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"provenance-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "provenance-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"provenance-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"provenance-{time.time_ns()}"}, token=token)
case_id = case["id"]
content = call("GET", f"/cases/{case_id}/caseDict", token=token)["content"]
intent = '''intent
{
    userIntent "Verify cavity convergence";
    targetOutputs ("residual history" "Ux field");
    acceptanceCriteria ("Ux final residual < 1e-6");
    openQuestions ("mesh sensitivity");
    assumptions ("laminar incompressible");
    decisionLog ("start with coarse mesh");
}
'''
first = content.replace("intent\n{\n    // userIntent        ;\n    // targetOutputs     ;\n    // acceptanceCriteria;\n}", intent, 1)
assert first != content
assert call("PUT", f"/cases/{case_id}/caseDict", {"content": first}, token=token).get("ok") is True
second = first.replace("mesh sensitivity", "time step sensitivity", 1)
assert call("PUT", f"/cases/{case_id}/caseDict", {"content": second}, token=token).get("ok") is True
status = call("GET", f"/cases/{case_id}/provenance", token=token)
assert status["chain_valid"] is True and status["total"] == 2, status
latest, previous = status["events"]
assert latest["previous_event_id"] == previous["event_id"]
assert latest["previous_sha256"] == previous["event_sha256"]
assert latest["intent"]["userIntent"] == "Verify cavity convergence"
assert latest["intent"]["openQuestions"] == ["time step sensitivity"], latest["intent"]
assert latest["intent"]["assumptions"] == ["laminar incompressible"]
assert latest["intent"]["decisionLog"] == ["start with coarse mesh"]
assert latest["workflow_state"] == previous["workflow_state"] == "A"
assert latest["casedict_sha256"] != previous["casedict_sha256"]
assert call("GET", f"/cases/{case_id}/provenance", token=other).get("_err") == 404
older = call("GET", f"/cases/{case_id}/provenance?offset=1&limit=1", token=token)
assert older["events"][0]["revision"] == previous["revision"] and older["has_more"] is False
print("1. 两次修订形成 intent/开放问题/假设/决策哈希链并分页 [OK]")

path = "0-caseDict/provenance.jsonl"
original = raw_text(case_id, path, token)
tampered = original.replace("Verify cavity convergence", "Tampered intent", 1)
assert call("PUT", f"/cases/{case_id}/file", {"path": path, "content": tampered}, token=token).get("ok") is True
assert call("GET", f"/cases/{case_id}/provenance", token=token)["chain_valid"] is False
assert call("PUT", f"/cases/{case_id}/file", {"path": path, "content": original}, token=token).get("ok") is True
assert call("GET", f"/cases/{case_id}/provenance", token=token)["chain_valid"] is True
print("2. 中途篡改可检出，跨用户 404，恢复后链重新有效 [OK]")
print("\n=== caseDict 需求/决策 provenance 哈希链冒烟通过 ===")
