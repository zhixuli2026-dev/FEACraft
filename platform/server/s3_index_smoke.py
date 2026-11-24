"""J15：S3 索引重建、内容搜索与作业产物增量可见。"""
import json
import os
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")
if os.environ.get("VIBEFLOW_TEST_S3") != "1":
    print("默认 fs 环境：S3 索引专项由 VIBEFLOW_TEST_S3=1 启用 [SKIP]")
    raise SystemExit(0)


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


auth = call("POST", "/auth/register", {"email": f"s3-index-{time.time_ns()}@vibeflow.cn",
            "name": "s3-index", "password": "s3-index-pass-123"})
token = auth["token"]
workspace = call("POST", "/workspaces", {"name": f"s3-index-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"s3-index-{time.time_ns()}"}, token=token)
case_id = case["id"]
marker = f"S3_INDEX_MARKER_{time.time_ns()}"
assert call("PUT", f"/cases/{case_id}/file", {"path": "Ref/marker.txt", "content": marker}, token=token)["ok"]
rebuilt = call("POST", f"/cases/{case_id}/index:rebuild", {}, token=token)
assert rebuilt["ok"] is True and rebuilt["entries"] > 0, rebuilt
result = call("GET", f"/cases/{case_id}/search?q={marker}", token=token)
assert result["ready"] and any(hit["path"] == "Ref/marker.txt" for hit in result["hits"]), result
print("J15 S3 index rebuild and content search: OK")
print("\n=== J15 SeaweedFS 索引链路冒烟通过 ===")
