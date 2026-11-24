"""A CAD live version binds a committed WS mutation to the workspace scene bytes."""
import hashlib
import json

from botocore.exceptions import ClientError
from sqlalchemy import select

from .. import storage
from ..models import DurableEvent
from .case_read_capacity import reading
from .case_write import bounded_case_read
from .durable_events import append_event


SCENE_PATH = "A-geo/scene.json"
SCENE_EVENT = "cadscene.updated"


async def latest(db, case_id):
    return await db.scalar(select(DurableEvent).where(
        DurableEvent.case_id == case_id, DurableEvent.type == SCENE_EVENT
    ).order_by(DurableEvent.seq.desc()).limit(1))


def version(event):
    return event.seq if event is not None else 0


async def record(db, case_id, action, data, dict_revision):
    receipt = {"action": action, "content_sha256": hashlib.sha256(data).hexdigest(),
               "content_size": len(data), "dict_revision": dict_revision}
    event = await append_event(db, case_id, SCENE_EVENT, receipt)
    return {"version": event.seq, "content_sha256": receipt["content_sha256"], "dict_revision": dict_revision}


def _live_scene(prefix, receipt):
    try:
        if storage.file_size(prefix, SCENE_PATH) != receipt["content_size"]:
            return None
        data = storage.get_bytes(prefix, SCENE_PATH, expected_size=receipt["content_size"])
    except FileNotFoundError:
        return None
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
            raise
        return None
    if hashlib.sha256(data).hexdigest() != receipt["content_sha256"]:
        return None
    return json.loads(data)["objects"]


async def snapshot(db, case, event=None):
    """Reference an ordinary CAD publication rather than copying it into the socket."""
    frame = {"type": "scene.snapshot", "case_id": case.id, "source": "case",
             "scene_url": f"/api/v1/cases/{case.id}/geometry/scene", "version": version(event),
             "dict_revision": case.dict_revision}
    if event is None:
        return frame
    async with reading(db, case):
        current = await latest(db, case.id)
        if current is None:
            return frame
        receipt = json.loads(current.payload_json)
        frame.update(version=current.seq, dict_revision=case.dict_revision)
        objects = await bounded_case_read(_live_scene, case.s3_prefix, receipt, code="cadscene_snapshot",
                                          label="CAD 场景读取")
        if objects is not None:
            frame.update(source="live", objects=objects, action=receipt["action"],
                         content_sha256=receipt["content_sha256"], action_dict_revision=receipt["dict_revision"])
    return frame
