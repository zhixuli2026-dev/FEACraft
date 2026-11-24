"""cadscene WS：scene.replace/scene.clear 广播、文件事实源与归属。"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import websockets

from app.routers import cadscene as cadscene_router

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
WS = B.removesuffix("/api/v1").replace("http", "ws", 1)


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=40) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def register(email, password):
    result = call("POST", "/auth/register", {"email": email, "name": "cadscene", "password": password})
    assert result.get("token"), result
    return result["token"]


async def check_broadcast_backpressure():
    class FakeSocket:
        def __init__(self, blocked=False):
            self.blocked = blocked
            self.messages = []

        async def send_json(self, payload):
            if self.blocked:
                await asyncio.Event().wait()
            self.messages.append(payload)

    blocked, healthy = FakeSocket(blocked=True), FakeSocket()
    case_id = 999999999
    stale = cadscene_router.SceneSocket(blocked, case_id, 0, 0, False)
    live = cadscene_router.SceneSocket(healthy, case_id, 0, 0, False)
    previous_timeout = cadscene_router._SEND_TIMEOUT
    cadscene_router._SEND_TIMEOUT = 0.05
    try:
        frame = {"type": "scene.clear", "version": 1, "case_id": case_id}
        outcomes = await asyncio.wait_for(asyncio.gather(stale.send(frame, update=True),
            live.send(frame, update=True), return_exceptions=True), timeout=1)
        assert healthy.messages == [frame], healthy.messages
        assert isinstance(outcomes[0], TimeoutError) and outcomes[1] is None
        assert stale.cursor == 0 and live.cursor == 1
    finally:
        cadscene_router._SEND_TIMEOUT = previous_timeout
    print("0. 背压客户端超时不阻塞健康广播 [OK]")


async def main():
    await check_broadcast_backpressure()
    tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    workspace = call("GET", "/workspaces", token=tok)[0]
    case_id = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"cadscene-{time.time_ns()}"}, token=tok)["id"]
    original = json.dumps({"schema": "vibeflow.cadquery.scene.v1", "objects": [], "messages": []})
    call("PUT", f"/cases/{case_id}/file", {"path": "A-geo/scene.json", "content": original}, token=tok)
    scene = {
        "schema": "vibeflow.cadquery.scene.v1", "caseRoot": "workspaces/1/cases/1",
        "objects": [{"name": "ws_cube", "color": "#00ff00", "alpha": 0.8,
                     "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
                     "faces": [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]],
                     "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}}],
        "messages": ["cadscene-smoke"],
    }
    uri = f"{WS}/ws/cadscene?case_id={case_id}&token={urllib.parse.quote(tok)}"
    try:
        async with websockets.connect(uri) as first, websockets.connect(uri) as second:
            assert (json.loads(await first.recv()))["type"] == "ready"
            assert (json.loads(await second.recv()))["type"] == "ready"
            await first.send(json.dumps({"type": "scene.replace", "objects": scene["objects"]}))
            assert (json.loads(await first.recv()))["type"] == "scene.replace"
            update = json.loads(await second.recv())
            assert update["type"] == "scene.replace" and update["objects"][0]["name"] == "ws_cube", update
            saved = json.loads(call("GET", f"/cases/{case_id}/file?path=A-geo/scene.json", token=tok)["content"])
            assert saved["objects"][0]["name"] == "ws_cube", saved
            print("1. scene.replace 广播并写入 scene.json [OK]")

            await first.send(json.dumps({"type": "scene.clear"}))
            assert (json.loads(await first.recv()))["type"] == "scene.clear"
            assert (json.loads(await second.recv()))["type"] == "scene.clear"
            saved = json.loads(call("GET", f"/cases/{case_id}/file?path=A-geo/scene.json", token=tok)["content"])
            assert saved["objects"] == [], saved
            print("2. scene.clear 广播并清空事实源 [OK]")

            bad = dict(scene["objects"][0], faces=[[0, 1, 99]])
            await first.send(json.dumps({"type": "scene.replace", "objects": [bad]}))
            error = json.loads(await first.recv())
            assert error["type"] == "error" and "越界" in error["detail"], error
            saved = json.loads(call("GET", f"/cases/{case_id}/file?path=A-geo/scene.json", token=tok)["content"])
            assert saved["objects"] == [], saved
            print("3. 非法面索引拒绝且不改文件 [OK]")
    finally:
        call("PUT", f"/cases/{case_id}/file", {"path": "A-geo/scene.json", "content": original}, token=tok)

    for bad_token in ("bad", register(f"cadscene-other-{time.time_ns()}@vibeflow.cn", "other-pass")):
        try:
            async with websockets.connect(f"{WS}/ws/cadscene?case_id={case_id}&token={urllib.parse.quote(bad_token)}"):
                raise AssertionError("非法/跨用户 token 被接受")
        except websockets.exceptions.InvalidStatus as error:
            assert error.response.status_code in (401, 403), error.response.status_code
    print("4. 无效 token 与跨用户均拒绝 [OK]")


asyncio.run(main())
print("\n=== cadscene WebSocket 冒烟通过 ===")
