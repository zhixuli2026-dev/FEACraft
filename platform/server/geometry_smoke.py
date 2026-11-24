"""几何服务冒烟：写 CadQuery 脚本 → preview 作业 → scene.json 内容校验 → export 出 STEP/STL"""
import json
import time
import urllib.error
import urllib.request
import os
from app.services.casedict import set_block

B = os.environ.get('VIBEFLOW_TEST_BASE', 'http://localhost:8000/api/v1')

def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token: req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data) as r:
        return json.loads(r.read())

tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
workspace = call('POST', '/workspaces', {'name':f'geometry-{time.time_ns()}'}, token=tok)
case_id = call('POST', f'/workspaces/{workspace["id"]}/cases', {'name':'parameterized geometry'}, token=tok)['id']
before_dict = call("GET", f"/cases/{case_id}/caseDict", token=tok)
parameterized = set_block(before_dict["content"], "geometry", {
    "parameters": {"width": 70, "depth": 50, "thickness": 6},
    "domain": {"scale": 1.5},
})
call("PUT", f"/cases/{case_id}/caseDict", {"content": parameterized}, token=tok)
for marker_path in ("A-geo/export-hook.txt", "A-geo/inline-export.step"):
    try:
        call("DELETE", f"/cases/{case_id}/file?path={marker_path}", token=tok)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

# 1. 写一个真实的 CadQuery 脚本（带锥台孔洞的盒子）
GEO = '''import cadquery as cq

# 参数化几何示例：带孔平板
width = float(ctx.params["width"]) * float(ctx.domain["scale"])
depth = float(ctx.params["depth"]) * float(ctx.domain["scale"])
thickness = float(ctx.params["thickness"])
assert ctx.data["geometry"]["parameters"]["width"] == "70"
plate = cq.Workplane("XY").box(width, depth, thickness)
hole = cq.Workplane("XY").cylinder(20, thickness)
plate = plate.cut(hole)
cq.exporters.export(plate, str(ctx.artifact_path("inline-export.step")))

def build_geometry(ctx):
    ctx.log("建模完成")
    return {"带孔平板": plate}

def export_geometry(ctx, objects):
    ctx.artifact_path("export-hook.txt").write_text("called", encoding="utf-8")
'''
call("PUT", f"/cases/{case_id}/file", {"path": "A-geo/geometry.py", "content": GEO}, token=tok)
print("1. geometry.py 已写入")

# 2. preview 作业
j = call("POST", f"/cases/{case_id}/geometry:preview", {}, token=tok)
print("2. preview job:", j["job_id"], j["status"])
for _ in range(60):
    time.sleep(2)
    st = call("GET", f"/jobs/{j['job_id']}", token=tok)
    if st["status"] not in ("queued", "running"):
        break
print("   完成:", st["status"], "exit:", st["exit_code"])
assert st["status"] == "succeeded", st["log_backlog"][-800:]

# 3. scene.json 校验
scene = call("GET", f"/cases/{case_id}/geometry/scene", token=tok)
assert scene["schema"] == "vibeflow.cadquery.scene.v1"
objs = scene["objects"]
assert len(objs) == 1 and objs[0]["name"] == "带孔平板"
o = objs[0]
n_v, n_f = len(o["vertices"]), len(o["faces"])
print(f"3. scene.json OK: {n_v} 顶点 {n_f} 面, color={o['color']}")
assert n_v > 50 and n_f > 50, "镶嵌结果太简陋"
zspan = o["bbox"]["max"][2] - o["bbox"]["min"][2]
xspan = o["bbox"]["max"][0] - o["bbox"]["min"][0]
yspan = o["bbox"]["max"][1] - o["bbox"]["min"][1]
assert abs(zspan - 6) < 0.5 and abs(xspan - 105) < 0.5 and abs(yspan - 75) < 0.5, (xspan, yspan, zspan)
# 参数和 domain 缩放必须真实驱动模型尺寸：105×75×6
print(f"   bbox: {o['bbox']['min']} ~ {o['bbox']['max']}")

# 3b. preview 不应执行导出钩子
try:
    call("GET", f"/cases/{case_id}/file?path=A-geo/export-hook.txt", token=tok)
    raise AssertionError("preview 不应执行 export_geometry，但标记文件已产生")
except urllib.error.HTTPError as e:
    assert e.code == 404, e.code
print("3b. preview 未执行导出钩子 [OK]")

# 3c. preview 也必须抑制脚本顶层内联导出
try:
    call("GET", f"/cases/{case_id}/file?path=A-geo/inline-export.step", token=tok)
    raise AssertionError("preview 不应产生脚本内联导出文件")
except urllib.error.HTTPError as e:
    assert e.code == 404, e.code
print("3c. preview 未产生内联导出文件 [OK]")

# 4. export 作业 → STEP/STL 文件出现
j2 = call("POST", f"/cases/{case_id}/geometry:export", {}, token=tok)
for _ in range(60):
    time.sleep(2)
    st2 = call("GET", f"/jobs/{j2['job_id']}", token=tok)
    if st2["status"] not in ("queued", "running"):
        break
assert st2["status"] == "succeeded", st2["log_backlog"][-800:]
tree = call("GET", f"/cases/{case_id}/tree", token=tok)
paths = [e["path"] for e in tree["entries"]]
step_paths = [p for p in paths if p.startswith("A-geo/exports/") and p.endswith(".step")]
stl_paths = [p for p in paths if p.startswith("A-geo/exports/") and p.endswith(".stl")]
assert step_paths and stl_paths, paths
assert "A-geo/geometry_manifest.json" in paths
marker = call("GET", f"/cases/{case_id}/file?path=A-geo/export-hook.txt", token=tok)
assert marker["content"] == "called", marker
print("4b. 显式 export 执行导出钩子 [OK]")
inline = call("GET", f"/cases/{case_id}/file?path=A-geo/inline-export.step", token=tok)
assert "ISO-10303-21" in inline["content"][:200], "显式 export 未保留脚本内联导出"
print("4c. 显式 export 保留内联导出 [OK]")
from urllib.parse import quote
step = call("GET", f"/cases/{case_id}/file?path={quote(step_paths[0])}", token=tok)
assert "ISO-10303-21" in step["content"][:200]
print("4. export OK: plate.step/.stl + manifest，STEP 头合法")

# 5. build/export 返回形态：build(ctx)、result、objects、stage、build_geometry
FORMS = {
    "build_alias": "def build(ctx):\n    return cq.Workplane('XY').box(2, 2, 2)\n",
    "result": "result = cq.Workplane('XY').box(2, 2, 2)\n",
    "objects": "objects = [cq.Workplane('XY').box(2, 2, 2)]\n",
    "stage": "ctx.stage('staged', cq.Workplane('XY').box(2, 2, 2))\n",
    "build_list": "def build_geometry(ctx):\n    return [cq.Workplane('XY').box(2, 2, 2)]\n",
}
for form_name, body in FORMS.items():
    form_case = call("POST", f'/workspaces/{workspace["id"]}/cases',
                     {"name": f"geo-form-{form_name}-{time.time_ns()}"}, token=tok)
    form_id = form_case["id"]
    call("PUT", f"/cases/{form_id}/file",
         {"path": "A-geo/geometry.py", "content": "import cadquery as cq\n" + body}, token=tok)
    form_job = call("POST", f"/cases/{form_id}/geometry:preview", {}, token=tok)
    for _ in range(60):
        time.sleep(1)
        form_state = call("GET", f"/jobs/{form_job['job_id']}", token=tok)
        if form_state["status"] not in ("queued", "running"):
            break
    assert form_state["status"] == "succeeded", (form_name, form_state["log_backlog"][-600:])
    form_scene = call("GET", f"/cases/{form_id}/geometry/scene", token=tok)
    assert len(form_scene["objects"]) == 1, (form_name, form_scene)
    print(f"5. 返回形态 {form_name} [OK]")

print("\n=== 几何服务冒烟通过（CadQuery preview→scene.json + export→STEP/STL + 返回形态）===")
