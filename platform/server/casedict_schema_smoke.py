"""A06：caseMeta 跨字段、类型/单位 lint 与真实单位换算。"""
import json
import os
import math
import time
import urllib.error
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


from casedict_schema_unit_smoke import run_checks

run_checks()

email = f"units-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "units-smoke", "password": "units-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"units-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "units-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"units-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"units-{time.time_ns()}"}, token=token)
case_id = case["id"]
for body, expected in (
    ({"dimension": "length", "value": 1000, "source": "mm", "target": "m"}, 1.0),
    ({"dimension": "length", "value": 2, "source": "inch", "target": "mm"}, 50.8),
    ({"dimension": "temperature", "value": 0, "source": "degC", "target": "K"}, 273.15),
    ({"dimension": "angle", "value": 180, "source": "deg", "target": "rad"}, math.pi),
):
    result = call("POST", f"/cases/{case_id}/caseDict/units:convert", body, token=token)
    assert math.isclose(result["value"], expected, rel_tol=1e-9), (result, expected)
for body in ({"dimension": "unknown", "value": 1, "source": "m", "target": "mm"},
             {"dimension": "length", "value": float("nan"), "source": "m", "target": "mm"}):
    assert call("POST", f"/cases/{case_id}/caseDict/units:convert", body, token=token).get("_err") == 422
assert call("POST", f"/cases/{case_id}/caseDict/units:convert",
            {"dimension": "length", "value": 1, "source": "m", "target": "mm"}, token=other).get("_err") == 404
print("2. mm/inch/degC/deg 真实换算、非法输入与归属 [OK]")
print("\n=== caseMeta 跨字段/单位 lint 与换算冒烟通过 ===")
