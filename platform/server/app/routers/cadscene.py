"""CAD scene WebSocket：scene.replace/scene.clear 与 scene.json 事实源同步。"""
import asyncio
import json
import math
import re

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from botocore.exceptions import ClientError

from ..db import SessionLocal
from ..deps import connection_user_id
from ..models import User
from ..services import cadscene_state, file_transactions, owner_storage_budget
from ..services.async_ops import finish_before_cancel
from ..services.case_write import CaseWriteConflict, coordinate
from ..services.event_replay import MAX_CURSOR
from .workspaces import get_owned_case

router = APIRouter(tags=["cadscene"])
_SEND_TIMEOUT = 5.0
_POLL_INTERVAL = 0.5
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _vector(value, length=3, integer=False):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError("向量维度错误")
    result = []
    for item in value:
        if integer and not isinstance(item, int):
            raise ValueError("面索引必须为整数")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError("坐标/索引必须有限")
        result.append(int(number) if integer else number)
    return result


def _object(item):
    if not isinstance(item, dict):
        raise ValueError("场景对象必须是字典")
    name = item.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 120:
        raise ValueError("对象名称须为 1–120 字符")
    color = item.get("color", "#2563eb")
    if not isinstance(color, str) or not _COLOR.fullmatch(color):
        raise ValueError("对象颜色须为 #RRGGBB")
    alpha = float(item.get("alpha", 1.0))
    if not math.isfinite(alpha) or not 0 <= alpha <= 1:
        raise ValueError("alpha 须在 [0,1]")
    vertices = item.get("vertices")
    faces = item.get("faces")
    if not isinstance(vertices, list) or len(vertices) > 100000:
        raise ValueError("vertices 必须是至多 100000 项的列表")
    if not isinstance(faces, list) or len(faces) > 200000:
        raise ValueError("faces 必须是至多 200000 项的列表")
    points = [_vector(vertex) for vertex in vertices]
    polygons = []
    for face in faces:
        polygon = _vector(face, len(face), integer=True)
        if len(polygon) < 3 or any(index < 0 or index >= len(points) for index in polygon):
            raise ValueError("面索引越界")
        polygons.append(polygon)
    bbox = item.get("bbox")
    if bbox is None:
        if not points:
            raise ValueError("空对象缺 bbox")
        bbox = {"min": [min(point[axis] for point in points) for axis in range(3)],
                "max": [max(point[axis] for point in points) for axis in range(3)]}
    if not isinstance(bbox, dict):
        raise ValueError("bbox 必须是对象")
    return {"name": name, "color": color, "alpha": alpha,
            "vertices": points, "faces": polygons,
            "bbox": {"min": _vector(bbox["min"]), "max": _vector(bbox["max"])}}


def _objects(value):
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("objects 必须是至多 64 项的列表")
    return [_object(item) for item in value]


class SceneVersionConflict(CaseWriteConflict):
    def __init__(self, current):
        super().__init__("scene_version_conflict", "CAD 场景版本已更新，请重新读取后保存", path=cadscene_state.SCENE_PATH)
        self.current = current

    def detail(self):
        return {**super().detail(), "current_version": self.current}


async def _owned_case(db, case_id, uid):
    user = await db.get(User, uid)
    if user is None or user.status != "active":
        raise HTTPException(401, "当前账户不可用")
    return await get_owned_case(case_id, user, db)


async def _write_scene(case_id: int, user_id: int, objects: list, message: dict):
    # A socket may outlive permissions or the case itself. Each mutation has a
    # fresh authorization/session and holds the same case lock as HTTP writers.
    condition = message.get("expected_version")
    if "expected_version" in message and (type(condition) is not int or not 0 <= condition <= MAX_CURSOR):
        raise ValueError("expected_version 必须是非负整数")
    path = cadscene_state.SCENE_PATH
    async with SessionLocal() as db:
        case = await _owned_case(db, case_id, user_id)
        expected = {path: message["expected_sha256"]} if "expected_sha256" in message else None
        async with owner_storage_budget.hold_case(db, case):
            async with coordinate(db, case, expected_revision=message.get("expected_revision"), expected_files=expected) as snapshot:
                if condition is not None:
                    current = cadscene_state.version(await cadscene_state.latest(db, case_id))
                    if current != condition:
                        raise SceneVersionConflict(current)
                document = {"schema": "vibeflow.cadquery.scene.v1", "caseRoot": case.s3_prefix,
                            "objects": objects, "messages": ["cadscene"]}
                payload = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf8")
                await file_transactions.write(db, case, {path: payload}, content_types={path: "application/json"})
                receipt = await cadscene_state.record(db, case_id, message["type"], payload, snapshot.dict_revision)
            await finish_before_cancel(db.commit())
            await finish_before_cancel(file_transactions.finish_committed(db, case))
        return receipt


class SceneSocket:
    def __init__(self, websocket, case_id, uid, cursor, snapshots):
        self.ws = websocket
        self.case_id = case_id
        self.uid = uid
        self.cursor = cursor
        self.snapshots = snapshots
        self.send_lock = asyncio.Lock()

    async def send(self, frame, *, update=False):
        async with self.send_lock:
            if update and frame["version"] <= self.cursor:
                return
            await asyncio.wait_for(self.ws.send_json(frame), timeout=_SEND_TIMEOUT)
            if update:
                self.cursor = frame["version"]

    async def close(self, code):
        async with self.send_lock:
            await self.ws.close(code=code)

    async def follow(self):
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                async with SessionLocal() as db:
                    event = await cadscene_state.latest(db, self.case_id)
                    if cadscene_state.version(event) <= self.cursor:
                        continue
                    case = await _owned_case(db, self.case_id, self.uid)
                    frame = await cadscene_state.snapshot(db, case, event)
            except HTTPException as error:
                if error.status_code in {401, 403, 404}:
                    await self.close(4401 if error.status_code == 401 else 4403)
                    return
                if error.status_code not in {409, 429, 504}:
                    raise
                continue
            except CaseWriteConflict as error:
                if error.code == "case_unavailable":
                    await self.close(4403)
                    return
                continue
            if frame["source"] == "live":
                payload = {"type": frame["action"], "case_id": self.case_id, "version": frame["version"],
                           "content_sha256": frame["content_sha256"], "dict_revision": frame["action_dict_revision"]}
                if payload["type"] == "scene.replace":
                    payload["objects"] = frame["objects"]
                await self.send(payload, update=True)
            elif self.snapshots:
                await self.send(frame, update=True)
            else:
                self.cursor = max(self.cursor, frame["version"])

    async def receive(self):
        while True:
            try:
                message = await self.ws.receive_json()
                if not isinstance(message, dict):
                    raise ValueError("场景消息必须是对象")
                kind = message.get("type")
                if kind not in {"scene.replace", "scene.clear"}:
                    raise ValueError("未知消息类型")
                objects = _objects(message.get("objects", [])) if kind == "scene.replace" else []
                receipt = await _write_scene(self.case_id, self.uid, objects, message)
                frame = {"type": kind, "case_id": self.case_id, **receipt}
                if kind == "scene.replace":
                    frame["objects"] = objects
                await self.send(frame, update=True)
            except CaseWriteConflict as error:
                await self.send({"type": "error", "detail": str(error), **error.detail()})
                if error.code == "case_unavailable":
                    await self.close(4403)
                    return
            except HTTPException as error:
                unavailable = error.status_code in {401, 403, 404}
                await self.send({"type": "error", "detail": error.detail,
                                 "status": error.status_code,
                                 "code": "case_unavailable" if unavailable else
                                         "storage_capacity" if error.status_code == 429 else "write_failed"})
                if unavailable:
                    await self.close(4403)
                    return
            except (OSError, ClientError):
                await self.send({"type": "error", "detail": "场景保存失败，请重试", "code": "write_failed"})
            except (ValueError, TypeError, KeyError) as error:
                await self.send({"type": "error", "detail": str(error)})


@router.websocket("/ws/cadscene")
async def cadscene(ws: WebSocket, case_id: int, token: str = "", snapshot: bool = False):
    uid = await connection_user_id(ws, token, scope='cadscene', target=str(case_id))
    if uid is None:
        await ws.close(code=4401)
        return
    try:
        async with SessionLocal() as db:
            case = await _owned_case(db, case_id, uid)
            newest = await cadscene_state.latest(db, case_id)
            cursor = cadscene_state.version(newest)
            initial = await cadscene_state.snapshot(db, case, newest) if snapshot else None
    except HTTPException as error:
        await ws.close(code=4401 if error.status_code == 401 else 4403 if error.status_code in {403, 404} else 1013)
        return
    except (CaseWriteConflict, OSError, ClientError):
        await ws.close(code=1013)
        return
    await ws.accept()
    client = SceneSocket(ws, case_id, uid, initial["version"] if initial else cursor, snapshot)
    tasks = []
    try:
        await client.send({"type": "ready", "backend": "cadscene", "case_id": case_id})
        if initial is not None:
            await client.send(initial)
        tasks = [asyncio.create_task(client.receive()), asyncio.create_task(client.follow())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        await client.close(1013)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
