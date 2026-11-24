"""F58：隔离 CLI PTY 的真实 WebSocket 输入与输出。"""
import asyncio
import json
import os
import time
import urllib.parse
import httpx
import websockets

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")


async def main():
    async with httpx.AsyncClient(base_url=B, timeout=30) as client:
        response = await client.post("/auth/register", json={
            "email": f"cli-sandbox-{time.time_ns()}@vibeflow.cn", "name": "cli-sandbox",
            "password": "sandbox-pass-123"})
        token = response.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.post("/workspaces", json={"name": f"cli-sandbox-{time.time_ns()}"}, headers=headers)
        ws_id = response.json()["id"]
        response = await client.post(f"/workspaces/{ws_id}/cases", json={"name": "cli-sandbox"}, headers=headers)
        case_id = response.json()["id"]
    url = (B.replace("http://", "ws://").replace("https://", "wss://").removesuffix("/api/v1") +
           f"/ws/terminal?shell=wsl-sandbox&case={case_id}&token={urllib.parse.quote(token)}")
    async with websockets.connect(url, ping_interval=None) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        await ws.send(json.dumps({"type": "input", "data": "id && test ! -d /mnt && test ! -d /home && test ! -d /root && echo CLI_SANDBOX_OK\r"}))
        output = ""
        for _ in range(100):
            chunk = await asyncio.wait_for(ws.recv(), timeout=10)
            output += chunk.decode() if isinstance(chunk, bytes) else chunk
            if "CLI_SANDBOX_OK" in output and "uid=65534(nobody)" in output:
                break
        assert "uid=65534(nobody)" in output, output
        assert "CLI_SANDBOX_OK" in output, output
        await ws.send(json.dumps({"type": "input", "data": "exit\r"}))
    print("F58 isolated CLI PTY, nobody, no host mounts and case binding: OK")


asyncio.run(main())
