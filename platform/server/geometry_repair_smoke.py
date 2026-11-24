"""A17：sew 边拓扑修复、fix/mirror/periodic 与 STEP 产物。"""
import json
import math
import os
import time
import urllib.error
import urllib.request

import cadquery as cq

from app.services.geo_repair import repair_shape
from app.services.geo_validation import validate_shape

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
SCRIPT = '''import cadquery as cq
box = cq.Workplane("XY").box(1, 2, 3)
def build_geometry(ctx):
    return box
'''


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(150):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("作业超时")


left = cq.Face.makePlane(1, 1, cq.Vector(0.5, 0, 0), cq.Vector(0, 0, 1))
right = cq.Face.makePlane(1, 1, cq.Vector(1.5, 0, 0), cq.Vector(0, 0, 1))
compound = cq.Compound.makeCompound([left, right])
before = validate_shape(compound)
sewed = repair_shape(compound, "sew")
after = validate_shape(sewed)
assert before["topology"]["boundary_edges"] == 8, before
assert after["topology"]["boundary_edges"] == 6 and after["topology"]["shared_edges"] == 1, after
assert after["manifold"] and not after["watertight"], after
box = cq.Workplane("XY").box(1, 2, 3).val()
assert validate_shape(repair_shape(box, "fix"))["watertight"]
notched = cq.Workplane("XY").box(2, 2, 2).cut(
    cq.Workplane("XY").box(1, 1, 1).translate((0.75, 0, 0))).val()
mirrored = repair_shape(notched, "mirror", "YZ")
assert abs(mirrored.Center().x + notched.Center().x) < 1e-7, (mirrored.Center().x, notched.Center().x)
assert abs(mirrored.Volume() - notched.Volume()) < 1e-7
periodic = repair_shape(box, "periodic", "X", 4, 2)
assert periodic.BoundingBox().xmax == 4.5 and len(periodic.Solids()) == 2
uniform = repair_shape(box, "scale", factor=2)
assert uniform.Volume() == 48 and uniform.BoundingBox().xlen == 2
nonuniform = repair_shape(box, "scale", factor=[2, 3, 4])
assert nonuniform.Volume() == 144 and math.isclose(nonuniform.BoundingBox().ylen, 6, abs_tol=1e-6) and math.isclose(nonuniform.BoundingBox().zlen, 12, abs_tol=1e-6)
for invalid in (0, -1, [1, 0, 2], [1, 2, 3, 4]):
    try:
        repair_shape(box, "scale", factor=invalid)
        raise AssertionError("非法缩放比例应拒绝")
    except ValueError:
        pass
print("1. sew 共享边修复、fix、mirror、periodic 与 scale 几何 [OK]")

email = f"geo-repair-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "geo-repair", "password": "geo-repair-pass"})["token"]
other = call("POST", "/auth/register", {"email": f"geo-repair-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "geo-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"geo-repair-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"geo-repair-{time.time_ns()}"}, token=token)
case_id = case["id"]
preview = call("PUT", f"/cases/{case_id}/file", {"path": "A-geo/geometry.py", "content": SCRIPT}, token=token)
assert wait_job(preview["auto_preview_job"], token)["status"] == "succeeded"
exported = call("POST", f"/cases/{case_id}/geometry:export", {}, token=token)
assert wait_job(exported["job_id"], token)["status"] == "succeeded"
fixed = call("POST", f"/cases/{case_id}/geometry/repair", {"path": "A-geo/exports/result.step", "operation": "fix"}, token=token)
assert fixed.get("result"), fixed
assert fixed["result"]["valid"] and fixed["result"]["watertight"], fixed
assert call("POST", f"/cases/{case_id}/geometry/repair", {"path": "A-geo/exports/result.step", "operation": "fix"}, token=token).get("_err") == 409
overwritten = call("POST", f"/cases/{case_id}/geometry/repair", {"path": "A-geo/exports/result.step", "operation": "fix", "overwrite": True}, token=token)
assert overwritten["result"]["valid"]
periodic_result = call("POST", f"/cases/{case_id}/geometry/repair", {
    "path": "A-geo/exports/result.step", "operation": "periodic", "axis": "X", "period": 4, "count": 2}, token=token)
assert periodic_result["result"]["counts"]["solids"] == 2, periodic_result
scaled = call("POST", f"/cases/{case_id}/geometry/repair", {
    "path": "A-geo/exports/result.step", "operation": "scale", "factor": 2}, token=token)
assert scaled["result"]["volume"] == 48 and scaled["result"]["watertight"], scaled
assert call("POST", f"/cases/{case_id}/geometry/repair", {"path": "A-geo/exports/result.step", "operation": "fix"}, token=other).get("_err") == 404
print("2. 修复 STEP/validation 产物、覆盖保护、周期复制与归属 [OK]")
print("\n=== 几何拓扑修复/对称/周期冒烟通过 ===")
