"""J15：S3 PTY staging 与会话结束产物同步。"""
import asyncio
import json
import os
import time
import urllib.parse
import httpx
import websockets

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")
if os.environ.get("VIBEFLOW_TEST_S3") != "1":
    print("默认 fs 环境：J15 S3 专项由 VIBEFLOW_TEST_S3=1 显式启用 [SKIP]")
    raise SystemExit(0)


async def main():
    async with httpx.AsyncClient(base_url=B, timeout=30, trust_env=False) as client:
        response = await client.post("/auth/register", json={
            "email": f"s3-pty-{time.time_ns()}@vibeflow.cn", "name": "s3-pty", "password": "s3-pty-pass-123"})
        token = response.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.post("/workspaces", json={"name": f"s3-pty-{time.time_ns()}"}, headers=headers)
        ws_id = response.json()["id"]
        response = await client.post(f"/workspaces/{ws_id}/cases", json={"name": "s3-pty"}, headers=headers)
        case_id = response.json()["id"]
        url = (B.replace("http://", "ws://").removesuffix("/api/v1") +
               f"/ws/terminal?shell=wsl&case={case_id}&token={urllib.parse.quote(token)}")
        async with websockets.connect(url, ping_interval=None) as ws:
            ready = json.loads(await ws.recv())
            assert ready["type"] == "ready"
            await ws.send(json.dumps({"type": "input", "data": "printf S3_PTY_OK > D-post/pty-result.txt && exit\r"}))
            for _ in range(100):
                chunk = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(chunk, str) and "exit" in chunk:
                    break
        for _ in range(100):
            response = await client.get(f"/cases/{case_id}/file?path=D-post/pty-result.txt", headers=headers)
            if response.status_code == 200:
                assert response.json()["content"] == "S3_PTY_OK"
                print("J15 PTY staging and close-time S3 output sync: OK")
                return
            await asyncio.sleep(0.1)
        raise AssertionError(response.text)


asyncio.run(main())
