"""Bind the pre-input sandbox manifest to SQL; never promote an old loose file."""
import hashlib
import json
from pathlib import Path
import uuid

from ..models import DurableEvent
from . import sandbox_volume
from .durable_events import append_event, json_text
from .sandbox_export_execution import read

EVENT = 'terminal.sandbox_baseline'


def event_id(session_id):
    return str(uuid.uuid5(uuid.UUID(session_id), EVENT))


async def bind(db, row, selected, manifest):
    # The caller has verified ready.json and has not started accepting input.
    raw = read(Path(selected['root']), 'initial.json', sandbox_volume.MAX_MANIFEST)
    if json.loads(raw) != manifest:
        raise ValueError('Sandbox initial manifest changed before registration')
    value = {'session_id': row.id, 'created_by': row.created_by,
             'source_prefix': json.loads(row.profile_json)['source_prefix'],
             'image_root': selected['root'], 'gate': selected['gate'],
             'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    previous = await db.get(DurableEvent, event_id(row.id))
    if previous is not None:
        if previous.case_id != row.case_id or previous.type != EVENT or json.loads(previous.payload_json) != value:
            raise ValueError('Sandbox baseline identity changed')
    else:
        await append_event(db, row.case_id, EVENT, value, event_id=event_id(row.id))
    return value


async def registered(db, row):
    event = await db.get(DurableEvent, event_id(row.id))
    if event is None:return None
    value = json.loads(event.payload_json)
    profile = json.loads(row.profile_json)
    if (event.type != EVENT or event.case_id != row.case_id
            or value['session_id'] != row.id or value['created_by'] != row.created_by
            or value['source_prefix'] != profile['source_prefix']
            or value['gate'] != json.loads(row.process_json)['gate']['process']):
        raise ValueError('Sandbox baseline belongs to another source')
    return value


def verified(root, binding):
    raw = read(Path(root), 'initial.json', sandbox_volume.MAX_MANIFEST)
    if len(raw) != binding['bytes'] or hashlib.sha256(raw).hexdigest() != binding['sha256']:
        raise ValueError('Sandbox initial manifest differs from its committed hash')
    value = json.loads(raw)
    if value['schema'] != 'vibeflow.sandbox-volume.v1' or value['binding']['gate'] != binding['gate']:
        raise ValueError('Sandbox initial manifest binding differs')
    return raw, value
