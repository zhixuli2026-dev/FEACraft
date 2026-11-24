"""A03：Gmsh→VTU 真实转换与现有 viz session 预览。"""
import hashlib
import json
import time
import urllib.error
import urllib.request

B = "http://localhost:8000/api/v1"
GMSH = '''$MeshFormat
2.2 0 8
$EndMeshFormat
$Nodes
4
1 0 0 0
2 1 0 0
3 0 1 0
4 0 0 1
$EndNodes
$Elements
1
1 4 0 1 2 3 4
$EndElements
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


email = f"msh-preview-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "msh-preview", "password": "msh-preview-pass"})["token"]
other = call("POST", "/auth/register", {"email": f"msh-preview-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "msh-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"msh-preview-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"msh-preview-{time.time_ns()}"}, token=token)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "B-mesh/tetra.msh", "content": GMSH}, token=token).get("ok") is True
preview = call("POST", f"/cases/{case_id}/geometry/msh-preview", {"path": "B-mesh/tetra.msh"}, token=token)
assert preview.get("points") is not None, preview
assert preview["points"] == 4 and preview["cells"] == 1 and preview["source_sha256"] == hashlib.sha256(GMSH.encode()).hexdigest(), preview
assert preview["path"].startswith(".vibeflow/msh-preview/") and preview["path"].endswith("/preview.vtu")
vtu = raw(f"/cases/{case_id}/raw?path={preview['path'].replace('/', '%2F')}", token=token)
assert b"<UnstructuredGrid>" in vtu and b"NumberOfPoints=\"4\"" in vtu and b"NumberOfCells=\"1\"" in vtu
assert call("POST", f"/cases/{case_id}/geometry/msh-preview", {"path": "B-mesh/tetra.msh"}, token=other).get("_err") == 404
session = call("POST", f"/cases/{case_id}/viz:sessions", {"file_path": preview["path"]}, token=token)
try:
    assert session.get("alive") is True and session.get("file_path") == preview["path"], session
    print("Gmsh→VTU source hash/网格数据/真实 viz session/归属: OK")
finally:
    if session.get("id"):
        call("DELETE", f"/viz:sessions/{session['id']}", token=token)
print("\n=== Gmsh 专用可视化路由冒烟通过 ===")
