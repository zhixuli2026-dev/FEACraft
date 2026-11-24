"""workspace/case 重命名、移动与深拷贝：DB+文件一致性、原数据保留、归属负向。"""
import json
import os
import time
import urllib.error
import urllib.request

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


tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
assert tok
suffix = str(time.time_ns())
source_name = f"admin-source-{suffix}"
source = call("POST", "/workspaces/1/cases", {"name": source_name}, token=tok)
assert source.get("id"), source
case_id = source["id"]
call("PUT", f"/cases/{case_id}/file", {"path": "Ref/marker.txt", "content": "ADMIN_MARKER"}, token=tok)
source_dict = call("GET", f"/cases/{case_id}/caseDict", token=tok)
source_case = call("GET", f"/cases/{case_id}", token=tok)

# 1. case 重命名不改文件前缀和内容
renamed = call("PATCH", f"/cases/{case_id}", {"name": f"admin-renamed-{suffix}"}, token=tok)
assert renamed.get("id") == case_id, renamed
assert call("GET", f"/cases/{case_id}/file?path=Ref/marker.txt", token=tok)["content"] == "ADMIN_MARKER"
print("1. case 重命名且文件不变 [OK]")

# 2. 新建目标工作区并移动 case，文件仍可访问
workspace = call("POST", "/workspaces", {"name": f"admin-target-{suffix}"}, token=tok)
assert workspace.get("id"), workspace
ws_id = workspace["id"]
moved = call("POST", f"/cases/{case_id}:move", {"workspace_id": ws_id}, token=tok)
assert moved.get("workspace_id") == ws_id, moved
assert all(c["id"] != case_id for c in call("GET", "/workspaces/1/cases", token=tok))
assert any(c["id"] == case_id for c in call("GET", f"/workspaces/{ws_id}/cases", token=tok))
assert call("GET", f"/cases/{case_id}/file?path=Ref/marker.txt", token=tok)["content"] == "ADMIN_MARKER"
print("2. case 跨工作区移动且文件可访问 [OK]")

# 3. 深拷贝 case：字典/revision/文件独立，原件不受影响
copied = call("POST", f"/cases/{case_id}:copy", {"name": f"admin-copy-{suffix}", "workspace_id": ws_id}, token=tok)
assert copied.get("id") and copied["id"] != case_id, copied
copy_id = copied["id"]
copy_case = call("GET", f"/cases/{copy_id}", token=tok)
assert copy_case["case_meta"] == source_case["case_meta"], copy_case
assert copy_case["workflow_state"] == source_case["workflow_state"]
assert copy_case["dict_revision"] == source_case["dict_revision"], copy_case
assert call("GET", f"/cases/{copy_id}/caseDict", token=tok)["content"] == source_dict["content"]
assert call("GET", f"/cases/{copy_id}/file?path=Ref/marker.txt", token=tok)["content"] == "ADMIN_MARKER"
call("PUT", f"/cases/{copy_id}/file", {"path": "Ref/marker.txt", "content": "COPIED_ONLY"}, token=tok)
assert call("GET", f"/cases/{case_id}/file?path=Ref/marker.txt", token=tok)["content"] == "ADMIN_MARKER"
print("3. case 深拷贝且原件独立 [OK]")

# 4. 工作区重命名和整区复制
renamed_ws = call("PATCH", f"/workspaces/{ws_id}", {"name": f"admin-target-renamed-{suffix}"}, token=tok)
assert renamed_ws.get("id") == ws_id, renamed_ws
copied_ws = call("POST", f"/workspaces/{ws_id}:copy", {"name": f"admin-workspace-copy-{suffix}", "include_cases": True}, token=tok)
assert copied_ws.get("id") and copied_ws["id"] != ws_id, copied_ws
new_ws_id = copied_ws["id"]
new_cases = call("GET", f"/workspaces/{new_ws_id}/cases", token=tok)
assert len(new_cases) == 2, new_cases
for original in (case_id, copy_id):
    original_info = call("GET", f"/cases/{original}", token=tok)
    matching = next(c for c in new_cases if c["name"] == original_info["name"])
    copied_info = call("GET", f"/cases/{matching['id']}", token=tok)
    assert copied_info["case_meta"] == original_info["case_meta"]
    assert copied_info["dict_revision"] == original_info["dict_revision"]
print("4. 工作区重命名与整区深拷贝 [OK]")

# 5. 重复名称与跨用户负向
assert call("POST", f"/cases/{case_id}:copy", {"name": f"admin-copy-{suffix}", "workspace_id": ws_id}, token=tok).get("_err") == 409
other_email = f"admin-other-{suffix}@vibeflow.cn"
other = call("POST", "/auth/register", {"email": other_email, "name": "other", "password": "admin-pass"})
if other.get("_err") == 409:
    other = call("POST", "/auth/login", {"email": other_email, "password": "admin-pass"})
other_tok = other.get("token")
assert other_tok, other
assert call("POST", f"/cases/{case_id}:move", {"workspace_id": ws_id}, token=other_tok).get("_err") == 404
assert call("POST", f"/cases/{case_id}:copy", {"name": "cross-copy", "workspace_id": ws_id}, token=other_tok).get("_err") == 404
print("5. 重复 409 与跨用户 404 [OK]")

print("\n=== workspace/case 重命名、移动与深拷贝冒烟通过 ===")
