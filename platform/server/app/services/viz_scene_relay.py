"""Deliver committed S3 scene edits to the owning renderer's private stage."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .. import storage
from ..db import SessionLocal
from ..workflow_models import DurableEvent
from . import attempt_objects, file_transactions, s3_streams, staging_sync, viz_records
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, bounded_case_read, coordinate
from .durable_events import append_event, json_text
from .workflow_state import ExecutionConflict


SCENE_PATH = 'D-post/scenes.py'
UNDELIVERED = object()
_EVENT = 'viz.scene_committed'
_INTERVAL = 0.5
_LOG = logging.getLogger(__name__)


def _source(prefix):
    return hashlib.sha256(json_text(attempt_objects.binding(prefix)).encode('utf8')).hexdigest()


async def record_scene_commit(db, case_id, source_prefix, records):
    """Append the committed byte identity in the same SQL txn as the journal."""
    for record in records:
        if record['path'] == SCENE_PATH:
            await append_event(db, case_id, _EVENT,
                               {'source': _source(source_prefix), 'file': record['after']})
            return


async def _latest(db, case_id, after):
    return await db.scalar(select(DurableEvent).where(
        DurableEvent.case_id == case_id, DurableEvent.seq > after, DurableEvent.type == _EVENT
    ).order_by(DurableEvent.seq.desc()).limit(1))


def _stage(session):
    selected = session.selected
    root, manifest = staging_sync.load(selected['staging_prefix'], selected['source_prefix'])
    if root.resolve() != Path(selected['case_root']):
        raise storage.StagingConflict('Visualization stage binding changed')
    return root, manifest


def _download(session, destination, expected):
    root, _ = _stage(session)
    scratch = file_transactions._path(root, destination)
    prefix = session.selected['source_prefix']
    head = s3_streams.snapshot_head(prefix, SCENE_PATH, expected['size'])
    if head is None or head['ContentLength'] != expected['size']:
        return False
    scratch.parent.mkdir(parents=True, exist_ok=True)
    with scratch.open('xb') as stream:
        with attempt_objects._selected_body(prefix, SCENE_PATH, head['ETag'], expected['size']) as (body, size):
            observed = s3_streams._digest_body(body, size, stream, s3_streams.current_deadline())
        return observed == expected


def _local_matches(path, expected):
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return expected is None
    if expected is None or size != expected['size']:
        return False
    return file_transactions._capture(path, limit=size) == expected


def _replace(session, destination, expected):
    root, manifest = _stage(session)
    path = file_transactions._path(root, SCENE_PATH)
    previous = getattr(session, 'scene_relay_last', UNDELIVERED)
    if previous is UNDELIVERED:
        baseline = manifest['files'].get(SCENE_PATH)
        previous = {key: baseline[key] for key in ('size', 'sha256')} if baseline is not None else None
    if not _local_matches(path, previous):
        return False
    if expected == previous:
        return True
    if expected is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = file_transactions._path(root, destination)
        os.replace(scratch, path)
    return True


async def scene_sync_excluded(session):
    """Only omit a delivered file whose private stage copy stayed unchanged."""
    last = getattr(session, 'scene_relay_last', UNDELIVERED)
    if last is UNDELIVERED:
        return ()

    def unchanged():
        root, _ = _stage(session)
        return _local_matches(file_transactions._path(root, SCENE_PATH), last)

    return (SCENE_PATH,) if await file_transactions._io(unchanged) else ()


async def _authorized(db, session, authorize):
    case = await authorize(db)
    if case.id != session.case_id or case.s3_prefix != session.selected['source_prefix']:
        raise ExecutionConflict('input_stale', 'Visualization source binding changed')
    return case


async def _install(session, destination, expected):
    # Keep both the replacement and its cleanup marker inside the drained
    # operation. Cancellation cannot leave a delivered file eligible for sync.
    if await asyncio.to_thread(_replace, session, destination, expected):
        session.scene_relay_last = expected
        return True
    return False


async def _deliver(session, authorize, event, expected):
    destination = f'.vibeflow/attempts/.viz-scene-{uuid4().hex}.tmp'
    try:
        if expected is not None:
            downloaded = await bounded_case_read(_download, session, destination, expected)
            if not downloaded:
                return False
        async with SessionLocal() as db:
            case = await _authorized(db, session, authorize)
            async with coordinate(db, case, content_write=False):
                await _authorized(db, session, authorize)
                owner = await viz_records.current(db, session.owner, launch=True)
                if owner.phase != 'active':
                    raise ExecutionConflict('state_conflict', 'Visualization owner is no longer active')
                newest = await _latest(db, session.case_id, event.seq - 1)
                if newest is None or newest.id != event.id:
                    return False
                return await finish_before_cancel(_install(session, destination, expected))
    finally:
        path = Path(session.selected['case_root']) / destination
        await file_transactions._io(path.unlink, missing_ok=True)


async def relay_scenes(session, authorize, *, after_sequence):
    """Follow the durable case sequence, including saves on a different API."""
    source = _source(session.selected['source_prefix'])
    cursor = after_sequence
    last_error = None
    while True:
        try:
            async with SessionLocal() as db:
                event = await _latest(db, session.case_id, cursor)
                if event is not None:
                    await _authorized(db, session, authorize)
            if event is not None:
                witness = json.loads(event.payload_json)
                if witness['source'] != source or await _deliver(session, authorize, event, witness['file']):
                    cursor = event.seq
            last_error = None
        except HTTPException as error:
            if error.status_code in (401, 403, 404):
                return
            if error.status_code not in (429, 504):
                raise
        except ExecutionConflict:
            return
        except (BotoCoreError, ClientError, CaseWriteConflict, OSError,
                OperationalError, storage.StagingConflict, ValueError) as error:
            kind = type(error)
            if kind != last_error:
                _LOG.warning('Visualization scene relay paused for session %s (%s)', session.id, kind.__name__)
                last_error = kind
        await asyncio.sleep(_INTERVAL)
