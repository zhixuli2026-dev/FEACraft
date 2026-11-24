"""二进制下载与单段 HTTP Range 冒烟：206/416、suffix range、HEAD、真实 fs URL。"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

B = "http://localhost:8000/api/v1"


def request(method, path, body=None, token=None, headers=None, raw=False):
    req = urllib.request.Request(B + path, data=body, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.headers.items()}, data if raw else json.loads(data)
    except urllib.error.HTTPError as e:
        data = e.read()
        if raw:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, data
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            payload = data.decode("utf-8", "replace")
        return e.code, dict(e.headers), payload


def main():
    token = request("POST", "/auth/login", json.dumps(
        {"email": "dev@vibeflow.cn", "password": "devpass123"}).encode(),
        headers={"Content-Type": "application/json"})[2]["token"]
    workspace = request("GET", "/workspaces", token=token)[2][0]
    case = request("POST", f"/workspaces/{workspace['id']}/cases",
                   json.dumps({"name": f"range-smoke-{time.time_ns()}"}).encode(),
                   token=token, headers={"Content-Type": "application/json"})[2]
    case_id = case["id"]
    path = "D-post/range.bin"
    encoded = urllib.parse.quote(path, safe="")
    payload = bytes(range(256))
    boundary = "----vibeflow-range-smoke"
    multipart = (
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"file\"; filename=\"range.bin\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    try:
        status, _, _ = request("POST", f"/cases/{case_id}/upload?path={encoded}", multipart,
                               token, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        assert status == 200
        info = request("GET", f"/cases/{case_id}/file?path={encoded}", token=token)[2]
        expected_prefix = f"/api/v1/cases/{case_id}/raw?path="
        assert info["kind"] == "binary" and info["download_url"].startswith(expected_prefix), info
        assert "cases/-" not in info["download_url"]
        print("1. fs 二进制 download_url 指向真实 raw 路由 [OK]")

        status, headers, body = request("GET", f"/cases/{case_id}/raw?path={encoded}",
                                        token=token, headers={"Range": "bytes=0-99"}, raw=True)
        assert status == 206 and body == payload[:100], (status, len(body))
        assert headers["content-range"] == "bytes 0-99/256", headers
        assert headers["content-length"] == "100" and headers["accept-ranges"] == "bytes"
        print("2. bytes=0-99 返回 206 与正确切片 [OK]")

        status, headers, body = request("GET", f"/cases/{case_id}/raw?path={encoded}",
                                        token=token, headers={"Range": "bytes=100-"}, raw=True)
        assert status == 206 and body == payload[100:]
        assert headers["content-range"] == "bytes 100-255/256"
        print("3. 开放结束 range [OK]")

        status, headers, body = request("GET", f"/cases/{case_id}/raw?path={encoded}",
                                        token=token, headers={"Range": "bytes=-16"}, raw=True)
        assert status == 206 and body == payload[-16:]
        assert headers["content-range"] == "bytes 240-255/256"
        print("4. suffix range 返回末 16 字节 [OK]")

        status, headers, body = request("GET", f"/cases/{case_id}/raw?path={encoded}",
                                        token=token, headers={"Range": "bytes=0-999"}, raw=True)
        assert status == 206 and body == payload
        assert headers["content-range"] == "bytes 0-255/256"
        print("5. 超出末尾的 range 截断到文件尾 [OK]")

        for value in ("bytes=256-", "bytes=20-10", "not-a-range"):
            status, headers, _ = request("GET", f"/cases/{case_id}/raw?path={encoded}",
                                         token=token, headers={"Range": value}, raw=True)
            assert status == 416, (value, status)
            assert headers["content-range"] == "bytes */256", headers
        print("6. 越界/倒置/非法 range 均返回 416 [OK]")

        status, headers, body = request("GET", f"/cases/{case_id}/raw?path={encoded}",
                                        token=token, raw=True)
        assert status == 200 and body == payload
        assert headers["accept-ranges"] == "bytes" and headers["content-length"] == "256"
        print("7. 无 Range 的完整 GET 保持 200 [OK]")

        status, headers, body = request("HEAD", f"/cases/{case_id}/raw?path={encoded}",
                                        token=token, raw=True)
        assert status == 200 and body == b""
        assert headers["content-length"] == "256" and headers["accept-ranges"] == "bytes"
        status, headers, body = request("HEAD", f"/cases/{case_id}/raw?path={encoded}",
                                        token=token, headers={"Range": "bytes=10-19"}, raw=True)
        assert status == 206 and body == b""
        assert headers["content-range"] == "bytes 10-19/256"
        assert headers["content-length"] == "10"
        print("8. HEAD 返回长度且不带 body [OK]")

        status, _, _ = request("GET", f"/cases/{case_id}/raw?path={encoded}", raw=True)
        assert status in (401, 403), status
        print("9. raw 仍要求认证 [OK]")
    finally:
        request("DELETE", f"/cases/{case_id}/file?path={encoded}", token=token)

    print("\n=== 二进制下载与 Range 冒烟通过 ===")


if __name__ == "__main__":
    main()
