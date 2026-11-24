"""H07：工作区内跨 case FTS，结果归属、未索引状态与跨用户隔离。"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None, expected=200):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            assert response.status == expected
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", "replace")
        assert error.code == expected, f"{method} {path}: {error.code} {payload}"
        return json.loads(payload)


def register(label):
    return call("POST", "/auth/register", {
        "email": f"{label}-{time.time_ns()}@vibeflow.cn", "name": label,
        "password": "workspace-search-pass"})


owner = register("workspace-search")
token = owner["token"]
workspace = call("POST", "/workspaces", {"name": f"workspace-search-{time.time_ns()}"}, token=token)
marker = f"WORKSPACE_FTS_{time.time_ns()}"
cases = []
for index in range(3):
    case = call("POST", f"/workspaces/{workspace['id']}/cases", {
        "name": f"fts-case-{index}-{time.time_ns()}"}, token=token)
    call("PUT", f"/cases/{case['id']}/file", {
        "path": "Ref/shared-marker.txt", "content": f"{marker} case={case['id']}"}, token=token)
    if index < 2:
        call("POST", f"/cases/{case['id']}/index:rebuild", {}, token=token)
    cases.append(case)

query = urllib.parse.urlencode({"q": marker, "limit": 10})
result = call("GET", f"/workspaces/{workspace['id']}/search?{query}", token=token)
assert result["ready"] and result["total_cases"] == 3 and result["indexed_cases"] == 2, result
hits = [hit for hit in result["hits"] if hit["path"] == "Ref/shared-marker.txt"]
assert len(hits) == 2, result
assert {hit["case_id"] for hit in hits} == {case["id"] for case in cases[:2]}, hits
assert all(hit["case_name"] == next(case["name"] for case in cases if case["id"] == hit["case_id"]) for hit in hits), hits
print("1. 两个已索引 case 同路径命中且 case_id/name 正确，未索引 case 不伪造结果 [OK]")

other = register("workspace-search-other")
forbidden = call("GET", f"/workspaces/{workspace['id']}/search?{query}",
                 token=other["token"], expected=404)
assert "工作区" in forbidden["detail"], forbidden
print("2. 跨用户工作区搜索 404 [OK]")

empty = call("GET", f"/workspaces/{workspace['id']}/search?q=%20", token=token, expected=400)
assert "空查询" in empty["detail"], empty
print("3. 空白查询 400 [OK]")

print("\n=== 工作区跨算例搜索冒烟通过 ===")
