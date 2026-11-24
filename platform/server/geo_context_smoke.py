"""B02：原版几何脚本全局上下文别名与真实 CadQuery preview。"""
import json
import os
import time
import urllib.request

B = os.environ.get('VIBEFLOW_TEST_BASE', 'http://localhost:8000/api/v1')


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(request, data, timeout=30) as response:
        return json.loads(response.read())


token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
workspace = call('POST', '/workspaces', {'name':f'geo-context-{time.time_ns()}'}, token=token)
case = call("POST", f'/workspaces/{workspace["id"]}/cases', {"name": f"geo-context-{time.time_ns()}"}, token=token)
script = r'''import os
from pathlib import Path
import cadquery as cq

assert Path(__file__).resolve() == Path(VIBEFLOW_GEOMETRY_SCRIPT).resolve()
assert CASE_ROOT == case_root == ctx.case_root
assert A_GEO_DIR == ctx.a_geo_dir == Path(CASE_ROOT) / "A-geo"
assert VIBEFLOW_CASE_ROOT == os.environ["VIBEFLOW_CASE_ROOT"] == str(ctx.case_root)
assert ctx.case_dict_path == Path(CASE_ROOT) / "0-caseDict" / "caseDict"
assert ctx.mode == "preview" and ctx.tolerance == 0.1
shape = cq.Workplane("XY").box(2, 3, 4)
show_object(shape, name="context_box", options={"color": (1, 0, 0), "opacity": 0.5})
debug("context", "ok")
'''
call("PUT", f"/cases/{case['id']}/file", {"path": "A-geo/geometry.py", "content": script}, token=token)
job = call("POST", f"/cases/{case['id']}/geometry:preview", {}, token=token)
state = job
for _ in range(120):
    state = call("GET", f"/jobs/{job['job_id']}", token=token)
    if state["status"] not in ("queued", "running"):
        break
    time.sleep(0.5)
assert state["status"] == "succeeded", state["log_backlog"][-1600:]
scene = call("GET", f"/cases/{case['id']}/geometry/scene", token=token)
assert len(scene["objects"]) == 1, scene
obj = scene["objects"][0]
assert obj["name"] == "context_box" and obj["color"] == "#ff0000" and obj["alpha"] == 0.5, obj
assert "context ok" in scene["messages"], scene
print("B02 geometry globals/__file__/Path aliases/env/show_object/debug: OK")
