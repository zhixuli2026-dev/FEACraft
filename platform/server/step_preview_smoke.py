"""A03：真实 STEP 只读导入、source hash 与不覆盖 scene.json。"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

B = os.environ.get('VIBEFLOW_TEST_BASE', 'http://localhost:8000/api/v1')
SCRIPT = '''import cadquery as cq
box = cq.Workplane("XY").box(2, 3, 4)
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


def raw(path, token):
    request = urllib.request.Request(B + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def wait_job(job_id, token):
    for _ in range(150):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("作业超时")


email = f"step-preview-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "step-preview", "password": "step-preview-pass"})["token"]
other = call("POST", "/auth/register", {"email": f"step-preview-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "step-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"step-preview-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"step-preview-{time.time_ns()}"}, token=token)
case_id = case["id"]
preview = call("PUT", f"/cases/{case_id}/file", {"path": "A-geo/geometry.py", "content": SCRIPT}, token=token)
assert wait_job(preview["auto_preview_job"], token)["status"] == "succeeded"
exported = call("POST", f"/cases/{case_id}/geometry:export", {}, token=token)
assert wait_job(exported["job_id"], token)["status"] == "succeeded"
scene_before = raw(f"/cases/{case_id}/raw?path=A-geo%2Fscene.json", token=token)
step_bytes = raw(f"/cases/{case_id}/raw?path=A-geo%2Fexports%2Fresult.step", token=token)
scene = call("GET", f"/cases/{case_id}/geometry/import-scene?path=A-geo%2Fexports%2Fresult.step", token=token)
assert scene["schema"] == "vibeflow.cadquery.scene.v1" and scene["sourceSha256"] == hashlib.sha256(step_bytes).hexdigest()
assert len(scene["objects"]) == 1 and len(scene["objects"][0]["vertices"]) > 0 and len(scene["objects"][0]["faces"]) > 0
assert scene["objects"][0]["name"] == "result.step"
scene_after = raw(f"/cases/{case_id}/raw?path=A-geo%2Fscene.json", token=token)
assert scene_after == scene_before, "只读 STEP 导入不得覆盖 scene.json"
assert call("GET", f"/cases/{case_id}/geometry/import-scene?path=A-geo%2Fgeometry.py", token=token).get("_err") == 400
assert call("GET", f"/cases/{case_id}/geometry/import-scene?path=missing.step", token=token).get("_err") == 404
assert call("GET", f"/cases/{case_id}/geometry/import-scene?path=A-geo%2Fexports%2Fresult.step", token=other).get("_err") == 404
print("真实 STEP 导入网格、source hash、scene 不变、类型/缺失/归属边界: OK")
print("\n=== STEP 专用只读 CAD 路由冒烟通过 ===")
