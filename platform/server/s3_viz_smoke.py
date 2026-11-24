"""J15：S3 viz staging、真实 kernel 和关闭时日志上传。"""
import json
import os
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")
if os.environ.get("VIBEFLOW_TEST_S3") != "1":
    print("默认 fs 环境：J15 S3 专项由 VIBEFLOW_TEST_S3=1 显式启用 [SKIP]")
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


auth = call("POST", "/auth/register", {"email": f"s3-viz-{time.time_ns()}@vibeflow.cn",
            "name": "s3-viz", "password": "s3-viz-pass-123"})
token = auth["token"]
workspace = call("POST", "/workspaces", {"name": f"s3-viz-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"s3-viz-{time.time_ns()}"}, token=token)
case_id = case["id"]
STL = """solid cube
 facet normal 0 0 1
  outer loop
   vertex 0 0 0
   vertex 1 0 0
   vertex 0 1 0
  endloop
 endfacet
endsolid cube
"""
assert call("PUT", f"/cases/{case_id}/file", {"path": "D-post/cube.stl", "content": STL}, token=token)["ok"]
session = call("POST", f"/cases/{case_id}/viz:sessions", {"file_path": "D-post/cube.stl"}, token=token)
assert session.get("alive") is True, session
port = session["port"]
result = call("DELETE", f"/viz:sessions/{session['id']}", token=token)
assert result["ok"] is True, result
log = call("GET", f"/cases/{case_id}/file?path=.vibeflow/viz/viz-{port}.log", token=token)
assert log.get("kind") in {"text", "binary"} and log.get("size", 0) > 0, log
print("J15 viz staging, real kernel and close-time S3 log upload: OK")
print("\n=== J15 SeaweedFS viz 链路冒烟通过 ===")
