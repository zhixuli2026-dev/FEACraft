"""WP07 通用结果阅读器：真实 VTK/VTU/VTP/VTM/XDMF/Exodus/STL/series 与 viz 会话。"""
import json
import http.cookiejar
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None, raw=False):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=40) as response:
            content = response.read()
            return (response.status, content) if raw else json.loads(content)
    except urllib.error.HTTPError as error:
        content = error.read()
        if raw:
            return error.code, content
        return {"_err": error.code, "detail": content.decode("utf-8", "replace")}


def put(path, content):
    result = call("PUT", f"/cases/{case_id}/file", {"path": path, "content": content}, token=tok)
    assert result.get("ok"), (path, result)


def upload(path, payload):
    boundary = "----vibeflow-reader-smoke"
    multipart = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sample.exo\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(B + f"/cases/{case_id}/upload?path={urllib.parse.quote(path, safe='')}",
                                 data=multipart, method="POST")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=30) as response:
        assert json.loads(response.read())["ok"]


tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"result-reader-{time.time_ns()}"}, token=tok)
assert case.get("id"), case
case_id = case["id"]
case_root = Path(__file__).parent / "data" / "files" / "workspaces" / "1" / "cases" / str(case_id)

stl = """solid triangle
facet normal 0 0 1
 outer loop
  vertex 0 0 0
  vertex 1 0 0
  vertex 0 1 0
 endloop
endfacet
endsolid triangle
"""
legacy_vtk = """# vtk DataFile Version 3.0
reader smoke
ASCII
DATASET POLYDATA
POINTS 3 float
0 0 0 1 0 0 0 1 0
POLYGONS 1 4
3 0 1 2
POINT_DATA 3
SCALARS p float 1
LOOKUP_TABLE default
1 2 3
"""
vtp = """<?xml version="1.0"?>
<VTKFile type="PolyData" version="1.0" byte_order="LittleEndian">
<PolyData><Piece NumberOfPoints="3" NumberOfVerts="0" NumberOfLines="0" NumberOfStrips="0" NumberOfPolys="1">
<PointData><DataArray type="Float32" Name="p" NumberOfComponents="1" format="ascii">1 2 3</DataArray></PointData>
<Points><DataArray type="Float32" NumberOfComponents="3" format="ascii">0 0 0 1 0 0 0 1 0</DataArray></Points>
<Polys><DataArray type="Int32" Name="connectivity" format="ascii">0 1 2</DataArray><DataArray type="Int32" Name="offsets" format="ascii">3</DataArray></Polys>
</Piece></PolyData></VTKFile>
"""
vtu = """<?xml version="1.0"?>
<VTKFile type="UnstructuredGrid" version="1.0" byte_order="LittleEndian">
<UnstructuredGrid><Piece NumberOfPoints="4" NumberOfCells="1">
<PointData><DataArray type="Float32" Name="p" NumberOfComponents="1" format="ascii">1 2 3 4</DataArray></PointData>
<Points><DataArray type="Float32" NumberOfComponents="3" format="ascii">0 0 0 1 0 0 0 1 0 0 0 1</DataArray></Points>
<Cells><DataArray type="Int32" Name="connectivity" format="ascii">0 1 2 3</DataArray><DataArray type="Int32" Name="offsets" format="ascii">4</DataArray><DataArray type="UInt8" Name="types" format="ascii">10</DataArray></Cells>
</Piece></UnstructuredGrid></VTKFile>
"""
xdmf = """<?xml version="1.0"?>
<Xdmf Version="3.0"><Domain><Grid Name="triangle" GridType="Uniform">
<Topology TopologyType="Triangle" NumberOfElements="1"><DataItem Dimensions="3" Format="XML">0 1 2</DataItem></Topology>
<Geometry GeometryType="XYZ"><DataItem Dimensions="9" Format="XML">0 0 0 1 0 0 0 1 0</DataItem></Geometry>
<Attribute Name="p" Center="Node"><DataItem Dimensions="3" Format="XML">1 2 3</DataItem></Attribute>
</Grid></Domain></Xdmf>
"""
for name, content in {
    "sample.stl": stl, "sample.vtk": legacy_vtk, "sample.vtp": vtp,
    "sample.vtu": vtu, "sample.xdmf": xdmf,
    "sample.vtm": '<VTKFile type="vtkMultiBlockDataSet" version="1.0"><vtkMultiBlockDataSet><Block index="0"><DataSet index="0" file="sample.vtu"/></Block><Block index="1"><DataSet index="0" file="sample.vtp"/></Block></vtkMultiBlockDataSet></VTKFile>',
    "sample.series": json.dumps({"files": [{"name": "sample.vtu", "time": 0}, {"name": "sample2.vtu", "time": 1}]}),
    "sample2.vtu": vtu.replace("1 2 3 4", "5 6 7 8"),
    "escape.series": json.dumps({"files": [{"name": "../sample.vtu", "time": 0}]}),
}.items():
    put(f"D-post/data/{name}", content)

# 用标准 NetCDF/Exodus 结构生成真实输入；本机 vtkExodusIIWriter 最小输入会段错误。
import numpy as np
from scipy.io import netcdf_file

exodus_path = Path("D:/tmp") / f"vibeflow-reader-{time.time_ns()}.exo"
nc = netcdf_file(exodus_path, "w")
for name, size in (("num_dim", 3), ("num_nodes", 4), ("num_elem", 1),
                   ("num_el_blk", 1), ("num_el_in_blk1", 1), ("num_nod_per_el1", 4),
                   ("time_step", 1), ("num_nod_var", 1), ("len_string", 33)):
    nc.createDimension(name, size)
nc.title = "vibeflow reader smoke"
nc.api_version = 5.0
nc.version = 5.0
nc.floating_point_word_size = 8
nc.file_size = 1
nc.int64_status = 0
nc.max_name_length = 32
for axis, values in zip("xyz", ([0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1])):
    coordinate = nc.createVariable(f"coord{axis}", "d", ("num_nodes",))
    coordinate[:] = np.array(values, dtype=float)
block = nc.createVariable("eb_prop1", "i", ("num_el_blk",))
block[:] = np.array([1], dtype=np.int32)
block.name = "ID"
connect = nc.createVariable("connect1", "i", ("num_el_in_blk1", "num_nod_per_el1"))
connect[:] = np.array([[1, 2, 3, 4]], dtype=np.int32)
connect.elem_type = "TETRA"
times = nc.createVariable("time_whole", "d", ("time_step",))
times[:] = np.array([0.0])
nodal = nc.createVariable("vals_nod_var1", "d", ("time_step", "num_nodes"))
nodal[:] = np.array([[1, 2, 3, 4]], dtype=float)
node_map = nc.createVariable("node_num_map", "i", ("num_nodes",))
node_map[:] = np.array([1, 2, 3, 4], dtype=np.int32)
elem_map = nc.createVariable("elem_num_map", "i", ("num_elem",))
elem_map[:] = np.array([1], dtype=np.int32)

def put_chars(name, dimensions, strings):
    variable = nc.createVariable(name, "c", dimensions)
    data = np.zeros(tuple(nc.dimensions[d] for d in dimensions), dtype="S1")
    for index, text in enumerate(strings):
        encoded = text.encode("ascii")
        data[index, :len(encoded)] = np.frombuffer(encoded, dtype="S1")
    variable[:] = data

put_chars("name_nod_var", ("num_nod_var", "len_string"), ["p"])
put_chars("eb_names", ("num_el_blk", "len_string"), ["block_1"])
nc.close()
upload("D-post/data/sample.exo", exodus_path.read_bytes())
exodus_path.unlink()
print("1. 七种文本/容器格式与真实 Exodus 输入已生成 [OK]")

# API RED：旧路由只接受 OpenFOAM run_dir，通用文件应启动 viz。
session = call("POST", f"/cases/{case_id}/viz:sessions", {"file_path": "D-post/data/sample.vtu"}, token=tok)
assert session.get("id"), session
sid = session["id"]
try:
    assert session["file_path"] == "D-post/data/sample.vtu"
    proxy_req = urllib.request.Request(
        f"http://localhost:8000/viz-proxy/{sid}/index.html?token={urllib.parse.quote(tok)}")
    with urllib.request.urlopen(proxy_req, timeout=15) as response:
        proxy_status, proxy_body = response.status, response.read(4000).lower()
    assert proxy_status == 200 and b"trame" in proxy_body, (proxy_status, proxy_body[:100])
    vite_req = urllib.request.Request(
        f"http://localhost:5173/viz-proxy/{sid}/index.html?token={urllib.parse.quote(tok)}")
    try:
        with urllib.request.urlopen(vite_req, timeout=15) as response:
            vite_status, vite_body = response.status, response.read(4000).lower()
    except urllib.error.HTTPError as error:
        vite_status, vite_body = error.code, error.read(4000).lower()
    assert vite_status == 200 and b"trame" in vite_body, (vite_status, vite_body[:100])
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(vite_req, timeout=15) as response:
        cookie_header = response.headers.get("Set-Cookie", "")
        assert "HttpOnly" in cookie_header and "SameSite=strict" in cookie_header, cookie_header
        assert f"Path=/viz-proxy/{sid}/" in cookie_header, cookie_header
    with opener.open(f"http://localhost:5173/viz-proxy/{sid}/loading.tpl", timeout=15) as response:
        loading = response.read().lower()
        assert response.status == 200
        assert "令牌无效" not in loading.decode("utf-8", "replace") and len(loading) > 20
    print("2. file_path 启动独立 viz 且所有者 proxy 200 [OK]")
finally:
    deleted = call("DELETE", f"/viz:sessions/{sid}", token=tok)
    assert deleted.get("ok"), deleted

from app.services.result_readers import ResultReaderError, load_result_file
expected = {
    "sample.vtk": 3, "sample.vtu": 4, "sample.vtp": 3,
    "sample.vtm": 2, "sample.xdmf": 3, "sample.exo": 4, "sample.stl": 3,
}
for name, minimum in expected.items():
    result = load_result_file(case_root / "D-post" / "data" / name)
    assert result.dataset.GetNumberOfPoints() >= minimum, (name, result.dataset.GetNumberOfPoints())
    assert result.dataset.GetNumberOfCells() >= 1, name
    print(f"3. {name} -> {result.reader_kind}: {result.dataset.GetNumberOfPoints()} 点/{result.dataset.GetNumberOfCells()} 单元 [OK]")
series = load_result_file(case_root / "D-post" / "data" / "sample.series", time=1)
assert series.times == [0, 1] and series.dataset.GetPointData().GetArray("p").GetRange() == (5, 8)
print("4. series 时刻枚举与 time=1 文件选择 [OK]")

try:
    load_result_file(case_root / "D-post" / "data" / "escape.series")
    raise AssertionError("series 允许目录逃逸")
except ResultReaderError:
    pass
for body in ({"file_path": "../secret.vtu"}, {"file_path": "D:/tmp/secret.vtu"}, {"file_path": "D-post/notes.md"}):
    rejected = call("POST", f"/cases/{case_id}/viz:sessions", body, token=tok)
    assert rejected.get("_err") == 400, (body, rejected)
print("5. series 越界、Windows 绝对路径和不支持扩展均拒绝 [OK]")

print("\n=== WP07 通用结果阅读器冒烟通过 ===")
