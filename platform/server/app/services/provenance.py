"""caseDict 修订的需求/决策哈希链与完整性校验。"""
from __future__ import annotations

from collections import deque
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from itertools import islice
import json
import os
import re
import threading
import uuid

from .. import storage
from .casedict import CASE_DICT_PATH, INTENT_LIST_FIELDS
from .file_read import FileRead, FileReadConflict
from .request_deadline import current as current_deadline

PROVENANCE_PATH = "0-caseDict/provenance.jsonl"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(prefix: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(prefix, threading.Lock())


def _canonical(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _event_hash(event: dict) -> str:
    return hashlib.sha256(_canonical(event)).hexdigest()


def _event_record(line: bytes) -> dict:
    event = json.loads(line.decode("utf-8"))
    if not isinstance(event, dict):
        raise ValueError("caseDict 修订记录必须是 JSON 对象")
    return event


def _blocks(reader):
    if reader.remote or os.name != "nt":
        yield from reader.chunks()
        return
    # Windows replacement can be denied while the source is open for reading.
    # Verify and close each bounded range before hashing or parsing its bytes.
    deadline = current_deadline()
    block_size = 64 * 1024
    for start in range(0, reader.size, block_size):
        if deadline is not None:
            deadline.check()
        yield reader.read(start, min(start + block_size, reader.size) - 1)


def _lines(reader):
    pending = bytearray()
    separated_cr = False
    with closing(_blocks(reader)) as chunks:
        for block in chunks:
            if separated_cr and block.startswith(b"\n"):
                block = block[1:]
            for piece in block.splitlines(keepends=True):
                pending.extend(piece)
                if piece.endswith((b"\n", b"\r")):
                    yield bytes(pending)
                    pending.clear()
            separated_cr = block.endswith(b"\r")
    if pending:
        yield bytes(pending)


def _history_events(reader, issues):
    deadline = current_deadline()
    previous = None
    for number, line in enumerate(_lines(reader), 1):
        if deadline is not None:
            deadline.check()
        if not line.strip():
            continue
        try:
            event = _event_record(line)
            declared = event.get("event_sha256")
            if (not isinstance(event.get("event_id"), str) or not isinstance(declared, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", declared)
                    or type(event.get("revision")) is not int or event["revision"] < 0):
                raise ValueError("Invalid revision record identity")
            payload = {key: value for key, value in event.items() if key != "event_sha256"}
            calculated = _event_hash(payload)
        except (ValueError, RecursionError):
            if issues is not None:
                issues.append({"code": "invalid_record", "line": number})
            continue

        if issues is not None:
            if declared != calculated:
                issues.append({"code": "record_hash_mismatch", "line": number})
            parent = (previous["event_id"], previous["event_sha256"]) if previous is not None else (None, None)
            if (event.get("previous_event_id"), event.get("previous_sha256")) != parent:
                issues.append({"code": "broken_link", "line": number})
        yield event
        previous = event


def select_provenance(prefix):
    readers = []
    for path in (PROVENANCE_PATH, CASE_DICT_PATH):
        try:
            readers.append(FileRead(prefix, path))
        except FileNotFoundError:
            readers.append(None)
    return tuple(readers)


def _semantic_value(value):
    if isinstance(value, list):
        if len(value) >= 2 and value[0] == "(" and value[-1] == ")":
            value = value[1:-1]
        return [_semantic_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _semantic_value(item) for key, item in value.items()}
    return value


def _semantic_intent(intent):
    if not isinstance(intent, dict):
        return _semantic_value(intent)
    return {key: value if key in INTENT_LIST_FIELDS and isinstance(value, list) else _semantic_value(value)
            for key, value in intent.items()}


def _intent_entries(event):
    intent = event.get("intent")
    if not isinstance(intent, dict):
        return {}
    blocks = {"openQuestions": intent.get("openQuestions"),
              "assumptions": intent.get("assumptions", intent.get("primaryAssumptions")),
              "decisionLog": intent.get("decisionLog")}
    return {kind: entries for kind, entries in blocks.items() if isinstance(entries, dict) and entries}


def _entry_status(entry):
    status = entry.get("status") if isinstance(entry, dict) else None
    return status if isinstance(status, str) and status.strip() else None


def _intent_changes(previous, event):
    before, after = _intent_entries(previous), _intent_entries(event)
    changes = []
    for kind in sorted(before.keys() | after.keys()):
        old, new = before.get(kind, {}), after.get(kind, {})
        for name in sorted(old.keys() | new.keys()):
            if name not in old:
                action = "created"
            elif name not in new:
                action = "removed"
            elif old[name] != new[name]:
                action = "updated"
            else:
                continue
            changes.append({"kind": kind, "id": name, "action": action,
                            "from_status": _entry_status(old.get(name)), "to_status": _entry_status(new.get(name))})
    return changes


def managed_block_entries(dictionary, entries):
    """Keep platform origin fields on managed caseDict block edits."""
    from .casedict import parse_casedict
    from .durable_events import now

    try:
        current = parse_casedict(dictionary).get("provenance")
    except ValueError:
        return entries
    if not isinstance(current, dict):
        return entries
    trace = current.get("artifactTrace")
    if not isinstance(trace, dict) or trace.get("registry") != "casePublications":
        return entries
    updated = dict(entries)
    for name in ("createdBy", "createdAt"):
        if isinstance(current.get(name), str):
            updated[name] = json.dumps(current[name], ensure_ascii=False)
    updated["updatedAt"] = json.dumps(now().isoformat())
    proposed = updated.get("artifactTrace", {})
    if isinstance(proposed, dict):
        updated["artifactTrace"] = {**proposed, "registry": "casePublications"}
    return updated


def record_case_revision(case, parsed: dict, source: str, previous_state: str) -> dict:
    content = storage.get_bytes(case.s3_prefix, CASE_DICT_PATH)
    with _lock(case.s3_prefix):
        event, data = prepare_case_revision(case, parsed, source, previous_state, content)
        storage.put_bytes(case.s3_prefix, PROVENANCE_PATH, data)
        return event


def prepare_case_revision(case, parsed: dict, source: str, previous_state: str, content: bytes, *,
                          existing=None, actor_id=None):
    """Build the next chain record without changing the live dictionary/files."""
    if existing is None:
        existing = storage.get_bytes(case.s3_prefix, PROVENANCE_PATH) \
            if storage.exists(case.s3_prefix, PROVENANCE_PATH) else b''
    previous = None
    for line in existing.splitlines():
        if line.strip():
            previous = _event_record(line)
    provenance = _semantic_value(parsed.get("provenance", {}))
    event = {
            "schema": "vibeflow.provenance.v1",
            "event_id": uuid.uuid4().hex[:16],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "case_id": getattr(case, "id", None),
            "revision": case.dict_revision,
            "source": source,
            "previous_state": previous_state,
            "workflow_state": case.workflow_state,
            "casedict_sha256": hashlib.sha256(content).hexdigest(),
            "intent": _semantic_intent(parsed.get("intent", {})),
        "provenance": provenance,
        "assumptions": provenance.get("assumptions", {}),
        "decisions": provenance.get("decisions", {}),
            "previous_event_id": previous.get("event_id") if previous else None,
            "previous_sha256": previous.get("event_sha256") if previous else None,
        }
    if actor_id is not None:
        event["actor_id"] = actor_id
    for block in ("errors", "extensions"):
        value = parsed.get(block, {})
        hash_key = f"{block}_sha256"
        prior_hash = previous.get(hash_key) if previous else None
        if value or prior_hash:
            current_hash = _event_hash(value)
            event[hash_key] = current_hash
            if current_hash != prior_hash:
                event[block] = value
    event["event_sha256"] = _event_hash(event)
    return event, existing + (json.dumps(event, ensure_ascii=False) + '\n').encode('utf8')


def provenance_status(prefix, revision, readers, offset: int, limit: int, match=None) -> dict:
    history, dictionary = readers
    issues = []
    # Retain the requested page, its preceding event and the chain head.
    # Deep pages use a second pass over the same pinned stream.
    events = deque(maxlen=1 if offset else limit + 1)
    total = 0
    matched = None
    if history is not None:
        for event in _history_events(history, issues):
            total += 1
            events.append(event)
            if match is not None and match == (event["revision"], event.get("casedict_sha256")):
                matched = event
    head = events[-1] if events else None
    if head is not None:
        if head["revision"] != revision:
            issues.append({"code": "head_revision_mismatch"})
        if dictionary is None:
            issues.append({"code": "dictionary_missing"})
        else:
            digest = hashlib.sha256()
            for block in _blocks(dictionary):
                digest.update(block)
            if head.get("casedict_sha256") != digest.hexdigest():
                issues.append({"code": "head_content_mismatch"})
    elif revision > 0:
        issues.append({"code": "history_missing"})

    stop = max(0, total - offset)
    length = min(stop, limit)
    if offset:
        events.clear()
        if stop:
            with closing(_history_events(history, None)) as stream:
                events = list(islice(stream, max(0, stop - limit - 1), stop))
    recent = list(events)
    page = recent[-length:][::-1] if length else []
    current_readers = select_provenance(prefix)
    for original, current in zip(readers, current_readers):
        if getattr(original, "version", None) != getattr(current, "version", None):
            raise FileReadConflict("Case revision history changed during the read")
    result = {"schema": "vibeflow.provenance.chain.v1", "chain_valid": not issues, "issues": issues,
            "events": page, "total": total, "offset": offset, "limit": limit,
            "has_more": offset + len(page) < total}
    if match is not None:
        result["matched_revision"] = matched if not issues else None
    if head is not None and not issues:
        current_entries = {kind: {name: _entry_status(entry) for name, entry in sorted(entries.items())}
                           for kind, entries in _intent_entries(head).items()}
        revisions = []
        for index in range(len(recent) - 1, len(recent) - length - 1, -1):
            event = recent[index]
            changes = _intent_changes(recent[index - 1] if index else {}, event)
            if changes:
                revisions.append({"event_id": event["event_id"], "revision": event["revision"], "changes": changes})
        result["intent_lifecycle"] = {"current": current_entries, "revisions": revisions}
    return result
