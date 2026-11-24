"""客户端错误上报：有界脱敏输入与 append-only JSONL。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

from .redaction import redact_text

_LOG_PATH = Path(os.getenv("VIBEFLOW_CLIENT_ERROR_LOG",
    Path(__file__).parent.parent.parent / "data" / "client-errors.jsonl"))
_LOCK = threading.Lock()


def _source_url(value: str) -> str:
    try:
        url = urlsplit(value)
        if url.scheme in {"http", "https"} and url.hostname:
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((url.scheme, host, url.path, "", ""))
        if not url.scheme and not url.netloc and url.path.startswith("/"):
            return url.path
    except ValueError:
        pass
    return ""


def _diagnostics(payload: dict) -> dict:
    raw_url = str(payload.get("url") or "")
    source = _source_url(raw_url)

    def diagnostic(field: str, limit: int) -> str:
        value = str(payload.get(field) or "")
        if source and raw_url != source:
            value = value.replace(raw_url, source)
            without_fragment = raw_url.partition("#")[0]
            if without_fragment and without_fragment != source:
                value = value.replace(without_fragment, source)
        return redact_text(value[:limit])

    return {
        "message": diagnostic("message", 2000).strip(),
        "stack": diagnostic("stack", 8000),
        "url": redact_text(source[:500]),
        "user_agent": diagnostic("user_agent", 300),
    }


def report_client_error(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("错误上报必须是对象")
    diagnostics = _diagnostics(payload)
    if not diagnostics["message"]:
        raise ValueError("message 不能为空")
    entry = {
        "schema": "vibeflow.client-error.v1",
        "id": uuid.uuid4().hex,
        "created_at": time.time(),
        **diagnostics,
    }
    with _LOCK:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return entry


def recent_client_errors(limit: int = 20) -> list[dict]:
    if not _LOG_PATH.is_file():
        return []
    with _LOCK:
        lines = _LOG_PATH.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in reversed(lines[-limit:]):
        entry = json.loads(line)
        entries.append({**entry, **_diagnostics(entry)})
    return entries
