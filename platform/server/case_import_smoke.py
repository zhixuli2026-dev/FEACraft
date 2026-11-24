"""外部 StandardCase ZIP 导入：真实产物、重复/归属与恶意压缩包安全断言。"""
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile

from cae_fixtures import cavity_2d

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def upload_zip(ws_id, name, payload, token, filename="case.zip"):
    boundary = "----vibeflow-import"
    parts = []
    if name is not None:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n{name}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n".encode())
    parts.append(payload)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{B}/workspaces/{ws_id}/cases:import", method="POST", data=b"".join(parts),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def make_zip(entries, root="SampleCase"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, value in entries:
            full = f"{root}/{path}" if root else path
            if isinstance(value, tuple) and value[0] == "symlink":
                info = zipfile.ZipInfo(full)
                info.external_attr = 0o120777 << 16
                info.create_system = 3
                archive.writestr(info, value[1])
            else:
                archive.writestr(full, value)
    return buffer.getvalue()


def cases(token):
    return call("GET", f"/workspaces/{workspace_id}/cases", token=token)


tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
assert tok
workspace_id = call("POST", "/workspaces", {"name": f"import-fixture-{time.time_ns()}"}, token=tok)["id"]
source_id = call("POST", f"/workspaces/{workspace_id}/cases", {"name": "source"}, token=tok)["id"]
for path, content in cavity_2d().items():
    saved = call("PUT", f"/cases/{source_id}/file", {"path": path, "content": content}, token=tok)
    assert "_err" not in saved, saved
mesh = call("POST", f"/cases/{source_id}/jobs", {
    "type": "mesh", "runtime": "wsl",
    "command": "source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && blockMesh",
}, token=tok)
assert mesh.get("id"), mesh
for _ in range(90):
    mesh_state = call("GET", f"/jobs/{mesh['id']}", token=tok)
    if mesh_state["status"] not in ("queued", "running"):
        break
    time.sleep(1)
assert mesh_state["status"] == "succeeded", mesh_state
source_dict = call("GET", f"/cases/{source_id}/caseDict", token=tok)
source_case = call("GET", f"/cases/{source_id}", token=tok)
valid_entries = [
    ("0-caseDict/caseDict", source_dict["content"]),
    ("A-geo/geometry.py", "# imported geometry marker\n"),
    ("Ref/blob.bin", bytes(range(256))),
]
for path in (
    "C-run/case-1/system/controlDict", "C-run/case-1/system/fvSchemes",
    "C-run/case-1/system/fvSolution", "C-run/case-1/system/blockMeshDict",
    "C-run/case-1/constant/transportProperties",
    "C-run/case-1/constant/polyMesh/points", "C-run/case-1/constant/polyMesh/faces",
    "C-run/case-1/constant/polyMesh/owner", "C-run/case-1/constant/polyMesh/neighbour",
    "C-run/case-1/constant/polyMesh/boundary", "C-run/case-1/0/U", "C-run/case-1/0/p",
):
    source = call("GET", f"/cases/{source_id}/file?path={path}", token=tok)
    assert "content" in source, (path, source)
    valid_entries.append((path, source["content"]))
valid_zip = make_zip(valid_entries)
name = f"import-{time.time_ns()}"

# 1. 单根目录 ZIP 导入：路径剥根、caseDict 镜像和二进制均正确
created = upload_zip(workspace_id, name, valid_zip, tok)
assert created.get("id"), created
case_id = created["id"]
assert created["files"] == 15 and created["dict_revision"] == 1, created
imported_case = call("GET", f"/cases/{case_id}", token=tok)
assert imported_case["workflow_state"] == source_case["workflow_state"], imported_case
assert imported_case["case_meta"] == source_case["case_meta"], imported_case
paths = [item["path"] for item in call("GET", f"/cases/{case_id}/tree", token=tok)["entries"]]
assert "0-caseDict/caseDict" in paths and "A-geo/geometry.py" in paths, paths
assert "SampleCase/0-caseDict/caseDict" not in paths, paths
assert call("GET", f"/cases/{case_id}/caseDict", token=tok)["content"] == source_dict["content"]
assert call("GET", f"/cases/{case_id}/file?path=A-geo/geometry.py", token=tok)["content"] == "# imported geometry marker\n"
binary = call("GET", f"/cases/{case_id}/file?path=Ref/blob.bin", token=tok)
assert binary["kind"] == "binary" and binary["size"] == 256, binary
print("1. 合法 ZIP 导入、剥根、caseDict 镜像与二进制 [OK]")

# 2. 导入的新 case 必须能实际求解，而非只有文件
solve = call("POST", f"/cases/{case_id}/jobs", {
    "type": "solve", "runtime": "wsl", "run_dir": "C-run/case-1",
    "command": "source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && icoFoam",
}, token=tok)
assert solve.get("id"), solve
for _ in range(90):
    time.sleep(1)
    solve_state = call("GET", f"/jobs/{solve['id']}", token=tok)
    if solve_state["status"] not in ("queued", "running"):
        break
assert solve_state["status"] == "succeeded", solve_state
result_paths = [item["path"] for item in call("GET", f"/cases/{case_id}/tree", token=tok)["entries"]]
assert "C-run/case-1/0.1/U" in result_paths, result_paths
print("2. 导入 case 实际 v2406 求解并生成 0.1/U [OK]")

# 3. 重复名称 409，不产生第二个 case
before = len(cases(tok))
duplicate = upload_zip(workspace_id, name, valid_zip, tok)
assert duplicate.get("_err") == 409, duplicate
assert len(cases(tok)) == before
print("3. 重复名称 409 [OK]")


def rejected(label, payload, expected, suffix=None):
    candidate = f"{name}-{suffix or label}"
    result = upload_zip(workspace_id, candidate, payload, tok)
    assert result.get("_err") == expected, (label, result)
    assert all(c["name"] != candidate for c in cases(tok)), f"{label} 不应留下 case"
    print(f"   {label} -> {expected} [OK]")


# 3. 路径/链接/格式/大小安全
rejected("zip-slip", make_zip(valid_entries + [("../escape.txt", b"x")]), 400)
rejected("absolute", make_zip(valid_entries + [("/absolute.txt", b"x")]), 400)
rejected("backslash", make_zip(valid_entries + [("..\\escape.txt", b"x")]), 400)
rejected("symlink", make_zip(valid_entries + [("Ref/link", ("symlink", "/etc/passwd"))]), 400)
rejected("missing-dict", make_zip([("A-geo/geometry.py", "x")]), 400, "missing")
rejected("invalid-dict", make_zip([("0-caseDict/caseDict", "broken { {")]), 422, "invalid")
invalid_enum = source_dict["content"].replace("caseMeta\n{", "caseMeta\n{\n    status unknown;", 1)
rejected("invalid-enum", make_zip([("0-caseDict/caseDict", invalid_enum)]), 422, "enum")
rejected("zip-bomb", make_zip(valid_entries + [("Ref/bomb.bin", b"\0" * 2_000_000)]), 413, "bomb")
print("4. 恶意路径、链接、非法字典与 zip bomb 拒绝 [OK]")

# 4. 其他用户不能向本人工作区导入
other = f"import-other-{time.time_ns()}@vibeflow.cn"
other_tok = call("POST", "/auth/register", {"email": other, "name": "other", "password": "import-pass"}, token=None)
if other_tok.get("_err") == 409:
    other_tok = call("POST", "/auth/login", {"email": other, "password": "import-pass"})
other_token = other_tok.get("token")
assert other_token, other_tok
cross = upload_zip(workspace_id, f"{name}-cross", valid_zip, other_token)
assert cross.get("_err") == 404, cross
print("5. 跨用户导入 404 [OK]")

print("\n=== 外部 StandardCase ZIP 导入冒烟通过 ===")
