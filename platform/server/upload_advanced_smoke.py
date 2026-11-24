"""H17：上传内存上限、断点分块上传与 multipart/byteranges。"""
from email.parser import BytesParser
from email.policy import default
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def request(method, path, body=None, token=None, headers=None, raw=False, expected=200):
    req = urllib.request.Request(B + path, data=body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            data = response.read()
            assert response.status == expected, response.status
            return response.status, {k.lower(): v for k, v in response.headers.items()}, data if raw else json.loads(data)
    except urllib.error.HTTPError as error:
        data = error.read()
        assert error.code == expected, f"{method} {path}: {error.code} {data[:500]!r}"
        if raw:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, data
        return error.code, dict(error.headers), json.loads(data)


def multipart(path, payload, boundary="----vibeflow-upload-limit"):
    return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + payload + f"\r\n--{boundary}--\r\n".encode(), boundary


registered = request("POST", "/auth/register", json.dumps({
    "email": f"upload-advanced-{time.time_ns()}@vibeflow.cn", "name": "upload-advanced",
    "password": "upload-advanced-pass"}).encode(), headers={"Content-Type": "application/json"})[2]
token = registered["token"]
workspace = request("POST", "/workspaces", json.dumps({"name": f"upload-advanced-{time.time_ns()}"}).encode(),
                    token=token, headers={"Content-Type": "application/json"})[2]
case = request("POST", f"/workspaces/{workspace['id']}/cases",
               json.dumps({"name": f"upload-advanced-{time.time_ns()}"}).encode(),
               token=token, headers={"Content-Type": "application/json"})[2]
case_id = case["id"]
path = "Ref/resumable.bin"
encoded = urllib.parse.quote(path, safe="")
payload = bytes(range(256))
sha256 = hashlib.sha256(payload).hexdigest()

session = request("POST", f"/cases/{case_id}/uploads", json.dumps({
    "path": path, "size": len(payload), "sha256": sha256}).encode(),
    token=token, headers={"Content-Type": "application/json"})[2]
upload_id = session["id"]
request("PUT", f"/cases/{case_id}/uploads/{upload_id}?offset=1", payload[:100], token,
        {"Content-Type": "application/octet-stream"}, expected=409)
request("PUT", f"/cases/{case_id}/uploads/{upload_id}?offset=0", payload[:100], token,
        {"Content-Type": "application/octet-stream"})
request("POST", f"/cases/{case_id}/uploads/{upload_id}:complete", b"{}", token,
        {"Content-Type": "application/json"}, expected=409)
status = request("GET", f"/cases/{case_id}/uploads/{upload_id}", token=token)[2]
assert status["offset"] == 100, status
request("PUT", f"/cases/{case_id}/uploads/{upload_id}?offset=100", payload[100:], token,
        {"Content-Type": "application/octet-stream"})
completed = request("POST", f"/cases/{case_id}/uploads/{upload_id}:complete", b"{}", token,
                    {"Content-Type": "application/json"})[2]
assert completed["ok"] and completed["sha256"] == sha256, completed
_, _, downloaded = request("GET", f"/cases/{case_id}/raw?path={encoded}", token=token, raw=True)
assert downloaded == payload
print("1. 错误 offset/提前 complete 拒绝，断点续传+SHA-256+原子发布 [OK]")

bad = request("POST", f"/cases/{case_id}/uploads", json.dumps({
    "path": "Ref/bad-hash.bin", "size": 4, "sha256": "0" * 64}).encode(),
    token=token, headers={"Content-Type": "application/json"})[2]
request("PUT", f"/cases/{case_id}/uploads/{bad['id']}?offset=0", b"BAD!", token,
        {"Content-Type": "application/octet-stream"})
request("POST", f"/cases/{case_id}/uploads/{bad['id']}:complete", b"{}", token,
        {"Content-Type": "application/json"}, expected=409)
request("GET", f"/cases/{case_id}/file?path=Ref%2Fbad-hash.bin", token=token, expected=404)
request("DELETE", f"/cases/{case_id}/uploads/{bad['id']}", token=token)
print("2. 坏 hash 不发布且可取消会话 [OK]")

oversize, boundary = multipart("oversize.bin", b"x" * (16 * 1024 * 1024 + 1))
request("POST", f"/cases/{case_id}/upload?path=Ref%2Foversize.bin", oversize, token,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"}, expected=413)
request("GET", f"/cases/{case_id}/file?path=Ref%2Foversize.bin", token=token, expected=404)
print("3. 普通上传 16 MiB+1 → 413 且不创建目标 [OK]")

range_header = "bytes=0-9,100-109,-5"
status, headers, body = request("GET", f"/cases/{case_id}/raw?path={encoded}", token=token,
                                headers={"Range": range_header}, raw=True, expected=206)
assert status == 206 and headers["content-type"].startswith("multipart/byteranges; boundary="), headers
message = BytesParser(policy=default).parsebytes(
    f"Content-Type: {headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
parts = message.get_payload()
assert len(parts) == 3, len(parts)
assert [part.get("Content-Range") for part in parts] == [
    "bytes 0-9/256", "bytes 100-109/256", "bytes 251-255/256"]
assert [part.get_payload(decode=True) for part in parts] == [payload[:10], payload[100:110], payload[-5:]]
_, head_headers, head_body = request("HEAD", f"/cases/{case_id}/raw?path={encoded}", token=token,
                                     headers={"Range": range_header}, raw=True, expected=206)
assert head_body == b"" and head_headers["content-length"] == headers["content-length"]
print("4. 三段 multipart/byteranges 内容、边界与 HEAD 长度 [OK]")

for value in ("bytes=0-10,10-20", "bytes=" + ",".join(f"{i*2}-{i*2}" for i in range(17))):
    request("GET", f"/cases/{case_id}/raw?path={encoded}", token=token,
            headers={"Range": value}, raw=True, expected=416)
print("5. 重叠区间与超过 16 段均返回 416 [OK]")

print("\n=== 高级上传与多段 Range 冒烟通过 ===")
