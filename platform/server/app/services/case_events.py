"""case_events 文件时间线：创建与 caseDict 修订，避免新增实库表。"""
from __future__ import annotations

import codecs
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from itertools import chain, islice
import json
import threading
import uuid

from .. import storage
from .file_read import FileRead, FileReadConflict

CASE_EVENTS_PATH = "0-caseDict/case-events.jsonl"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(prefix: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(prefix, threading.Lock())


def build_case_event(case, event_type: str, source: str,
                     previous_state: str | None = None,
                     details: dict | None = None) -> dict:
    event = {"schema": "vibeflow.case-event.v1", "event_id": uuid.uuid4().hex[:16],
             "type": event_type, "source": source,
             "created_at": datetime.now(timezone.utc).isoformat(),
             "case_id": getattr(case, "id", None), "revision": case.dict_revision,
             "previous_state": previous_state, "workflow_state": case.workflow_state}
    if details:
        event.update(details)
    return event


def prepare_case_events(case, events: list[dict], *, existing=None) -> bytes:
    if existing is None:
        existing = storage.get_bytes(case.s3_prefix, CASE_EVENTS_PATH) \
            if storage.exists(case.s3_prefix, CASE_EVENTS_PATH) else b''
    return existing + ''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in events).encode('utf8')


def append_case_event(case, event_type: str, source: str,
                      previous_state: str | None = None,
                      details: dict | None = None) -> dict:
    event = build_case_event(case, event_type, source, previous_state, details)
    with _lock(case.s3_prefix):
        storage.put_bytes(case.s3_prefix, CASE_EVENTS_PATH, prepare_case_events(case, [event]))
    return event


def select_case_events(prefix):
    try:
        return FileRead(prefix, CASE_EVENTS_PATH)
    except FileNotFoundError:
        return None


def _read_case_events(reader):
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = []
    line_ends = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    with closing(reader.chunks()) as chunks:
        for block in chain(chunks, (b"",)):
            for part in decoder.decode(block, final=not block).splitlines(keepends=True):
                pending.append(part)
                if part[-1] in line_ends:
                    line = "".join(pending)
                    pending.clear()
                    if line.strip():
                        yield json.loads(line[:-1])
        if pending:
            line = "".join(pending)
            if line.strip():
                yield json.loads(line)


def list_case_events(prefix, reader, offset: int, limit: int) -> dict:
    # Validate all records and count the history even for an empty or deep page.
    # A second pass on the pinned version avoids retaining offset-sized data.
    latest = deque(maxlen=limit)
    total = 0
    if reader is not None:
        for event in _read_case_events(reader):
            total += 1
            if not offset:
                latest.append(event)
        if 0 < offset < total:
            end = total - offset
            with closing(_read_case_events(reader)) as events:
                latest.extend(islice(events, max(0, end - limit), end))
        current = select_case_events(prefix)
        if current is None or current.version != reader.version:
            raise FileReadConflict("Case timeline changed during the read")
    page = list(reversed(latest))
    return {"events": page, "total": total, "offset": offset, "limit": limit,
            "has_more": offset + len(page) < total}
