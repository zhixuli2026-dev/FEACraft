"""A17：真实 CadQuery 实体/开放面的 validation.json 与 export manifest。"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

B = os.environ.get('VIBEFLOW_TEST_BASE', 'http://localhost:8000/api/v1')
SCRIPT = '''import cadquery as cq
box = cq.Workplane("XY").box(1, 2, 3)
open_face = cq.Face.makePlane(4, 5)
def build_geometry(ctx):
    return {"box": box, "open_face": open_face}
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
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(150):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("几何作业超时")


email = f"geo-validation-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "geo-validation", "password": "geo-validation-pass"})["token"]
other = call("POST", "/auth/register", {"email": f"geo-validation-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "geo-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"geo-validation-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"geo-validation-{time.time_ns()}"}, token=token)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "A-geo/geometry.py", "content": SCRIPT}, token=token).get("auto_preview_job")
job = wait_job(call("GET", f"/cases/{case_id}/jobs", token=token)[0]["id"], token)
assert job["status"] == "succeeded", job
validation = call("GET", f"/cases/{case_id}/geometry/validation", token=token)
assert validation["schema"] == "vibeflow.geometry.validation.v1", validation
objects = {item["name"]: item for item in validation["objects"]}
box = objects["box"]
assert box["valid"] and box["watertight"] and box["manifold"], box
assert box["volume"] == 6 and box["counts"] == {"solids": 1, "shells": 1, "faces": 6,
                                                "edges": 12, "vertices": 8}, box
assert box["topology"]["boundary_edges"] == 0 and box["topology"]["nonmanifold_edges"] == 0
assert set(box["topology"]["face_adjacency"]) == {2}
open_face = objects["open_face"]
assert open_face["valid"] and not open_face["watertight"] and open_face["manifold"], open_face
assert open_face["topology"]["boundary_edges"] == 4 and open_face["topology"]["nonmanifold_edges"] == 0
assert validation["summary"] == {"valid": True, "watertight": False, "manifold": True, "object_count": 2}
assert validation["source_sha256"] == hashlib.sha256(SCRIPT.encode()).hexdigest()
assert call("GET", f"/cases/{case_id}/geometry/validation", token=other).get("_err") == 404
print("1. box 水密/双边共享、开放面 boundary、summary/source hash/归属 [OK]")

export = call("POST", f"/cases/{case_id}/geometry:export", {}, token=token)
export_job = wait_job(export["job_id"], token)
assert export_job["status"] == "succeeded", export_job
manifest = call("GET", f"/cases/{case_id}/file?path=A-geo%2Fgeometry_manifest.json", token=token)
manifest_data = json.loads(manifest["content"])
assert manifest_data["validation"] == "../validation.json" and len(manifest_data["artifacts"]) == 2, manifest_data
print("2. 显式 export 同样生成验证产物并链接 manifest [OK]")
print("\n=== CadQuery 几何有效性/水密/流形验证产物冒烟通过 ===")
