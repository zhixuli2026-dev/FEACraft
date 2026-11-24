"""J28：真实 S3 raw Range/HEAD 与 presign 下载业务验证。"""
import os
import time
from urllib.parse import parse_qs, urlsplit

import httpx

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")
if os.environ.get("VIBEFLOW_TEST_S3") != "1":
    print("默认 fs 环境：J28 S3 Range/presign 专项由 VIBEFLOW_TEST_S3=1 显式启用 [SKIP]")
    raise SystemExit(0)


def main():
    payload = bytes(range(256))
    with httpx.Client(base_url=B, timeout=60, trust_env=False) as client:
        response = client.post("/auth/register", json={
            "email": f"s3-range-{time.time_ns()}@vibeflow.cn", "name": "s3-range",
            "password": "s3-range-pass-123"})
        response.raise_for_status()
        token = response.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post("/workspaces", json={"name": f"s3-range-{time.time_ns()}"}, headers=headers)
        workspace_id = response.json()["id"]
        response = client.post(f"/workspaces/{workspace_id}/cases", json={"name": "s3-range"}, headers=headers)
        case_id = response.json()["id"]
        path = "D-post/range.bin"
        encoded = "D-post%2Frange.bin"
        try:
            response = client.post(f"/cases/{case_id}/upload?path={encoded}",
                files={"file": ("range.bin", payload, "application/octet-stream")}, headers=headers)
            assert response.status_code == 200, response.text
            response = client.get(f"/cases/{case_id}/file?path={encoded}", headers=headers)
            info = response.json()
            assert info["kind"] == "binary" and info["size"] == 256, info
            signed = urlsplit(info["download_url"])
            query = parse_qs(signed.query)
            assert signed.scheme in {"http", "https"} and "X-Amz-Signature" in query, info
            print("1. binary metadata returns a real S3 presigned URL: OK")

            # 预签名 URL 自身授权，不依赖平台 Bearer；不得把签名 URL 写入日志。
            with httpx.Client(timeout=30, trust_env=False) as download:
                response = download.get(info["download_url"])
                assert response.status_code == 200 and response.content == payload, response.status_code
                response = download.get(info["download_url"], headers=headers)
                assert response.status_code == 200 and response.content == payload, response.status_code
            print("2. presigned GET downloads exact bytes with/without platform Authorization: OK")

            response = client.get(f"/cases/{case_id}/raw?path={encoded}",
                                  headers={**headers, "Range": "bytes=0-99"})
            assert response.status_code == 206 and response.content == payload[:100]
            assert response.headers["content-range"] == "bytes 0-99/256"
            assert response.headers["content-length"] == "100"
            response = client.get(f"/cases/{case_id}/raw?path={encoded}",
                                  headers={**headers, "Range": "bytes=-16"})
            assert response.status_code == 206 and response.content == payload[-16:]
            assert response.headers["content-range"] == "bytes 240-255/256"
            response = client.get(f"/cases/{case_id}/raw?path={encoded}",
                                  headers={**headers, "Range": "bytes=100-"})
            assert response.status_code == 206 and response.content == payload[100:]
            print("3. S3 raw closed/open/suffix ranges return exact slices: OK")

            for value in ("bytes=256-", "bytes=20-10", "not-a-range"):
                response = client.get(f"/cases/{case_id}/raw?path={encoded}",
                                      headers={**headers, "Range": value})
                assert response.status_code == 416, (value, response.status_code)
                assert response.headers["content-range"] == "bytes */256"
            print("4. invalid and out-of-bounds ranges return 416: OK")

            response = client.get(f"/cases/{case_id}/raw?path={encoded}", headers=headers)
            assert response.status_code == 200 and response.content == payload
            assert response.headers["content-length"] == "256"
            response = client.head(f"/cases/{case_id}/raw?path={encoded}", headers=headers)
            assert response.status_code == 200 and response.content == b""
            assert response.headers["content-length"] == "256"
            response = client.head(f"/cases/{case_id}/raw?path={encoded}",
                                   headers={**headers, "Range": "bytes=10-19"})
            assert response.status_code == 206 and response.content == b""
            assert response.headers["content-range"] == "bytes 10-19/256"
            assert response.headers["content-length"] == "10"
            print("5. full GET and HEAD/Range HEAD preserve bytes and headers: OK")

            response = client.get(f"/cases/{case_id}/raw?path={encoded}")
            assert response.status_code in {401, 403}
            print("6. raw remains platform-authenticated: OK")
        finally:
            client.delete(f"/cases/{case_id}/file?path={encoded}", headers=headers)

    print("\n=== J28 S3 Range/HEAD/presign smoke passed ===")


if __name__ == "__main__":
    main()
