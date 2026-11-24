"""files.changed WS：快照监听创建/修改/删除、FTS 同步与归属拒绝。"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request

import aiohttp

from app.routers import events as events_router

B = os.environ.get('VIBEFLOW_TEST_BASE', 'http://localhost:8000/api/v1')
WS = B.removesuffix('/api/v1').replace('http', 'ws', 1)


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


def register(email, password):
    result = call("POST", "/auth/register", {"email": email, "name": "events-security", "password": password})
    if result.get("_err") == 409:
        result = call("POST", "/auth/login", {"email": email, "password": password})
    return result["token"]


async def check_disconnect_exit():
    class DisconnectWs:
        async def receive(self):
            return {"type": "websocket.disconnect"}

    await asyncio.wait_for(events_router._wait_disconnect(DisconnectWs()), timeout=1)
    print("0. 无文件变化时 disconnect 立即退出 watcher [OK]")


async def next_change(ws, wanted_kind, wanted_path):
    async with asyncio.timeout(10):
        while True:
            message = await ws.receive()
            if message.type == aiohttp.WSMsgType.CLOSE:
                raise AssertionError(f"WS 提前关闭: {message.data}")
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            if event.get("type") != "files.changed":
                continue
            for change in event["changes"]:
                if change["kind"] == wanted_kind and change["path"] == wanted_path:
                    return event


async def main():
    await check_disconnect_exit()
    owner = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    other = register(f"events-security-{time.time_ns()}@vibeflow.cn", "events-pass")
    case = call("POST", "/workspaces/1/cases", {"name": f"events-{time.time_ns()}"}, token=owner)
    case_id = case["id"]
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(f"{WS}/ws/events?case_id={case_id}&token={owner}")
        ready = json.loads((await ws.receive()).data)
        assert ready["type"] == "ready" and ready["backend"] == "poll", ready
        async with asyncio.timeout(10):
            while True:
                message = await ws.receive()
                assert message.type == aiohttp.WSMsgType.TEXT, message.type
                event = json.loads(message.data)
                if event.get('type') == 'files.changed' and event.get('source') == 'file_watch':
                    assert event['event_id'] and event['rescan']
                    break
        path = "Ref/watched.txt"
        call("POST", f"/cases/{case_id}/index:rebuild", {}, token=owner)
        call("PUT", f"/cases/{case_id}/file", {"path": path, "content": "WATCH_CREATE"}, token=owner)
        created = await next_change(ws, "created", path)
        assert created["case_id"] == case_id
        hits = call("GET", f"/cases/{case_id}/search?q=WATCH_CREATE", token=owner)
        assert hits["hits"], hits
        call("PUT", f"/cases/{case_id}/file", {"path": path, "content": "WATCH_MODIFY"}, token=owner)
        await next_change(ws, "modified", path)
        hits = call("GET", f"/cases/{case_id}/search?q=WATCH_MODIFY", token=owner)
        assert hits["hits"], hits
        call("DELETE", f"/cases/{case_id}/file?path={path}", token=owner)
        await next_change(ws, "deleted", path)
        hits = call("GET", f"/cases/{case_id}/search?q=WATCH_MODIFY", token=owner)
        assert not hits["hits"], hits
        print("files.changed created/modified/deleted + FTS [OK]")
        await ws.close()

        # 其他用户和无效 token 必须拒绝，不泄露 watcher
        try:
            other_ws = await session.ws_connect(f"{WS}/ws/events?case_id={case_id}&token={other}")
            closed = await other_ws.receive()
            assert closed.type == aiohttp.WSMsgType.CLOSE and closed.data == 4403, (closed.type, closed.data)
        except aiohttp.WSServerHandshakeError as error:
            assert error.status == 403, error.status
        try:
            bad_ws = await session.ws_connect(f"{WS}/ws/events?case_id={case_id}&token=bad")
            closed = await bad_ws.receive()
            assert closed.type == aiohttp.WSMsgType.CLOSE and closed.data == 4401, (closed.type, closed.data)
        except aiohttp.WSServerHandshakeError as error:
            assert error.status == 403, error.status
        print("files.changed 跨用户/无效 token 均 403 [OK]")


if __name__ == "__main__":
    asyncio.run(main())
