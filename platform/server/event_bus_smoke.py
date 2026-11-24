"""H16：统一 /ws/events 用户级扇出与跨用户隔离。"""
import asyncio
import json
import time
import urllib.error
import urllib.request

import aiohttp

from app.db import SessionLocal, engine
from app.models import Plan
from sqlalchemy import select

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def register(prefix):
    return call("POST", "/auth/register", {"email": f"{prefix}-{time.time_ns()}@vibeflow.cn",
                                           "name": prefix, "password": "event-bus-pass-123"})


async def receive_ready(ws):
    message = await asyncio.wait_for(ws.receive(), timeout=10)
    assert message.type == aiohttp.WSMsgType.TEXT
    assert json.loads(message.data)["type"] == "ready"


async def receive_type(ws, expected, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = await asyncio.wait_for(ws.receive(), timeout=max(0.1, deadline - time.monotonic()))
        if message.type == aiohttp.WSMsgType.TEXT:
            event = json.loads(message.data)
            if event["type"] == expected:
                return event
    raise AssertionError(f"未收到 {expected}")


async def set_viz_quota(user_id, value):
    async with SessionLocal() as db:
        plan = await db.scalar(select(Plan).where(Plan.code == "free"))
        original = plan.quotas
        quotas = json.loads(original)
        quotas["viz_concurrent"] = value
        plan.quotas = json.dumps(quotas)
        await db.commit()
        return original


async def restore_quotas(text):
    async with SessionLocal() as db:
        plan = await db.scalar(select(Plan).where(Plan.code == "free"))
        plan.quotas = text
        await db.commit()
    await engine.dispose()


async def main():
    a = register("event-a")
    b = register("event-b")
    ta, tb = a["token"], b["token"]
    wa = call("POST", "/workspaces", {"name": f"event-a-{time.time_ns()}"}, token=ta)
    wb = call("POST", "/workspaces", {"name": f"event-b-{time.time_ns()}"}, token=tb)
    ca = call("POST", f"/workspaces/{wa['id']}/cases", {"name": f"event-a-{time.time_ns()}"}, token=ta)
    cb = call("POST", f"/workspaces/{wb['id']}/cases", {"name": f"event-b-{time.time_ns()}"}, token=tb)
    quotas = await set_viz_quota(a["user"]["id"], 100)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://127.0.0.1:8000/ws/events?case_id={ca['id']}&token={ta}") as ws_a, \
                   session.ws_connect(f"ws://127.0.0.1:8000/ws/events?case_id={cb['id']}&token={tb}") as ws_b:
            await receive_ready(ws_a)
            await receive_ready(ws_b)
            job = call("POST", f"/cases/{ca['id']}/jobs", {"type": "shell", "command": "echo BUS_OK", "runtime": "windows"}, token=ta)
            event = await receive_type(ws_a, "jobs.updated")
            assert event["job_id"] == job["id"] and event["status"] in ("running", "succeeded"), event
            print("1. jobs.updated 按所有者推送 [OK]")

            vtp = ('<VTKFile type="PolyData"><PolyData><Piece NumberOfPoints="1" NumberOfVerts="1" NumberOfLines="0" NumberOfStrips="0" NumberOfPolys="0">'
                   '<Points><DataArray type="Float32" NumberOfComponents="3" format="ascii">0 0 0</DataArray></Points>'
                   '<Verts><DataArray type="Int64" Name="connectivity" format="ascii">0</DataArray>'
                   '<DataArray type="Int64" Name="offsets" format="ascii">1</DataArray></Verts></Piece></PolyData></VTKFile>')
            call("PUT", f"/cases/{ca['id']}/file", {"path": "D-post/point.vtp", "content": vtp}, token=ta)
            viz = call("POST", f"/cases/{ca['id']}/viz:sessions", {"file_path": "D-post/point.vtp"}, token=ta)
            created = await receive_type(ws_a, "scene.available")
            assert created["session_id"] == viz["id"] and created["status"] == "created", created
            call("DELETE", f"/viz:sessions/{viz['id']}", token=ta)
            closed = await receive_type(ws_a, "scene.available")
            assert closed["session_id"] == viz["id"] and closed["status"] == "closed", closed
            print("2. scene.available created/closed [OK]")

            await set_viz_quota(a["user"]["id"], 0)
            rejected = call("POST", f"/cases/{ca['id']}/viz:sessions", {"file_path": "D-post/point.vtp"}, token=ta)
            assert rejected.get("_err") == 429, rejected
            warning = await receive_type(ws_a, "quota.warning")
            assert warning["dimension"] == "并发可视化会话" and warning["limit"] == 0, warning
            await restore_quotas(quotas)
            print("3. quota.warning 429 同事件且保留原错误 [OK]")

            call("GET", "/runtime/capabilities", token=ta)
            degraded = await receive_type(ws_a, "runtime.degraded")
            assert degraded["runtime"] == "wsl" and degraded["gmsh"] is False, degraded
            print("4. runtime.degraded [OK]")

            try:
                event = await asyncio.wait_for(ws_b.receive(), timeout=0.5)
                if event.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(event.data)
                    assert payload["type"] not in ("jobs.updated", "scene.available", "quota.warning", "runtime.degraded"), payload
            except asyncio.TimeoutError:
                pass
            print("5. 其他用户收不到 A 的作业/场景/配额/降级事件 [OK]")


asyncio.run(main())
print("\n=== 统一事件总线/所有者隔离冒烟通过 ===")
