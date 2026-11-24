"""Bounded v1 workspace backup validation before DB rows or files are created."""
import base64
from datetime import datetime
import hashlib
import io
import json
import os
import re
import zipfile
import zlib

from .case_write import CaseWriteConflict, normalize_path

SCHEMA = "vibeflow.workspace-backup.v1"
MAX_BACKUP_BYTES = 100 * 1024 * 1024
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_MANIFEST_BYTES = 160 * 1024 * 1024
MAX_FILES = 10000
MAX_RECORDS = 100000


class BackupError(ValueError):
    pass


class BackupTooLarge(BackupError):
    pass


def _require(condition, message):
    if not condition:
        raise BackupError(message)


def _json_object(value, label):
    try:
        result = json.loads(value)
    except (TypeError, ValueError, RecursionError) as error:
        raise BackupError(label+" JSON无效") from error
    _require(isinstance(result, dict), label+"必须为JSON对象")
    return result


def _records(manifest, key, fields):
    rows = manifest.get(key, [])
    _require(isinstance(rows, list) and len(rows) <= MAX_RECORDS, key+"记录清单无效或超限")
    for row in rows:
        _require(isinstance(row, dict), key+"记录必须为对象")
        for field, kind in fields.items():
            _require(type(row.get(field)) is kind, key+"字段类型无效: "+field)
            if kind is str:
                _require("\x00" not in row[field], key+"字段包含NUL: "+field)
                try:
                    row[field].encode("utf-8")
                except UnicodeError as error:
                    raise BackupError(key+"字段含无效Unicode: "+field) from error
        for field in ("created_at", "updated_at", "ended_at"):
            value = row.get(field)
            if value is not None:
                _require(isinstance(value, str), key+"日期格式无效")
                try:
                    datetime.fromisoformat(value)
                except ValueError as error:
                    raise BackupError(key+"日期格式无效") from error
    return rows


def _ids(rows, label):
    ids = [row["id"] for row in rows]
    _require(all(value > 0 for value in ids) and len(set(ids)) == len(ids), label+" ID重复或无效")
    return {row["id"]: row for row in rows}


def _name(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and not any(c in value for c in "/\\\x00")


def _validate_material_history(manifest):
    from .material_library import KEY, encode

    rows = _records(manifest, 'material_revisions', {'key': str, 'revision': int, 'archived': bool, 'created_at': str})
    latest = {}
    for row in sorted(rows, key=lambda item: (item['key'], item['revision'])):
        key, revision, archived = row['key'], row['revision'], row['archived']
        previous, was_archived = latest.get(key, (0, True))
        _require(re.fullmatch(KEY, key) and revision == previous+1, '材料库版本重复、缺失或标识非法')
        if archived:
            _require(not was_archived and 'material' not in row and 'sha256' not in row, '材料库归档记录无效')
        else:
            material = row.get('material')
            _require(isinstance(material, dict), '材料库版本缺少材料属性')
            try:
                body = encode(key, material)
            except (ValueError, RecursionError) as error:
                raise BackupError(str(error)) from error
            _require(hashlib.sha256(body.encode('utf8')).hexdigest() == row.get('sha256'), '材料库版本摘要不一致')
        latest[key] = (revision, archived)


def validate_structure(manifest):
    _require(isinstance(manifest, dict) and manifest.get("schema") == SCHEMA, "不支持的备份schema")
    workspace = manifest.get("workspace")
    _require(isinstance(workspace, dict) and _name(workspace.get("name"), 200), "工作区记录无效")
    _records({"workspace": [workspace]}, "workspace", {"name": str})
    _validate_material_history(manifest)
    cases = _records(manifest, "cases", {"id": int, "name": str, "workflow_state": str,
        "dict_revision": int, "case_meta": str, "parse_status": str})
    case_map = _ids(cases, "算例")
    _require(len({row["name"] for row in cases}) == len(cases), "算例名称重复")
    for row in cases:
        _require(_name(row["name"], 200) and 0 <= row["dict_revision"] <= 2147483647 and
            row["workflow_state"] in {"A", "B", "C", "D", "E", "V"} and
            row["parse_status"] in {"ok", "invalid"}, "算例字段值无效")
        _json_object(row["case_meta"], "case_meta")
    jobs = _records(manifest, "jobs", {"case_id": int, "type": str, "command": str,
        "status": str, "log_s3_key": str, "parsed_summary": str})
    from .workflow_state import TERMINAL
    logs = set()
    for row in jobs:
        _require(row["case_id"] in case_map and row["status"] in TERMINAL and
            0 < len(row["type"]) <= 40 and len(row["log_s3_key"]) <= 600 and
            (row.get("exit_code") is None or (type(row["exit_code"]) is int and
                -2147483648 <= row["exit_code"] <= 2147483647)), "作业关联或状态无效，活动任务不能恢复")
        _json_object(row["parsed_summary"], "parsed_summary")
        if row["log_s3_key"]:
            key = (row["case_id"], row["log_s3_key"])
            _require(key not in logs, "作业日志路径重复")
            logs.add(key)
    conversations = _records(manifest, "conversations", {"id": int, "case_id": int,
        "title": str, "model": str, "permission_mode": str})
    conv_map = _ids(conversations, "会话")
    for row in conversations:
        _require(row["case_id"] in case_map and len(row["title"]) <= 300 and len(row["model"]) <= 120 and
            row["permission_mode"] in {"approve_writes", "full_access", "ask", "plan"}, "会话关联或权限无效")
        parent = row.get("forked_from_id")
        _require(parent is None or (type(parent) is int and parent in conv_map and
            conv_map[parent]["case_id"] == row["case_id"]), "会话分支关联无效")
    # Iterative graph walk: reject cycles without recursion on an untrusted chain.
    complete = set()
    for cid in conv_map:
        visiting = set()
        cursor = cid
        while cursor is not None and cursor not in complete:
            _require(cursor not in visiting, "会话分支存在循环")
            visiting.add(cursor)
            cursor = conv_map[cursor].get("forked_from_id")
        complete.update(visiting)
    children, entered, exited = {}, {}, {}
    for row in conversations:
        children.setdefault(row.get("forked_from_id"), []).append(row["id"])
    stack = [(cid, False) for cid in children.get(None, [])]
    cursor = 0
    while stack:
        cid, leaving = stack.pop()
        if leaving:
            exited[cid] = cursor
        else:
            entered[cid] = cursor
            cursor += 1
            stack.append((cid, True))
            stack.extend((child, False) for child in children.get(cid, []))
    events = _records(manifest, "events", {"id": int, "conversation_id": int, "seq": int,
        "event_id": str, "type": str, "payload": str})
    event_map = _ids(events, "事件")
    sequence = set()
    for row in events:
        _require(row["conversation_id"] in conv_map and 0 < row["seq"] <= 2147483647 and
            0 < len(row["type"]) <= 30, "事件关联或字段无效")
        key = (row["conversation_id"], row["seq"])
        _require(key not in sequence, "会话事件序号重复")
        sequence.add(key)
        _json_object(row["payload"], "事件payload")
    for row in conversations:
        head = row.get("branch_head_event_id")
        if head is not None:
            _require(type(head) is int and head in event_map and row.get("forked_from_id") is not None,
                "会话分支事件不存在")
            parent, ancestor = row["forked_from_id"], event_map[head]["conversation_id"]
            _require(entered[ancestor] <= entered[parent] < exited[ancestor], "会话分支事件不属于父会话")
    tools = _records(manifest, "tool_calls", {"conversation_id": int, "tool": str,
        "args": str, "status": str, "result": str})
    for row in tools:
        _require(row["conversation_id"] in conv_map and row["status"] in
            {"executed", "rejected", "cancelled", "interrupted", "failed"} and 0 < len(row["tool"]) <= 60,
            "工具关联或状态无效，活动工具不能恢复")
        _json_object(row["args"], "工具args")
    files = manifest.get("files")
    _require(isinstance(files, list) and len(files) <= MAX_FILES, "文件清单无效或超限")
    paths = {}
    for row in files:
        _require(isinstance(row, dict) and type(row.get("case_id")) is int and row["case_id"] in case_map,
            "文件关联的算例不存在")
        path = row.get("path")
        try:
            normalized = normalize_path(path)
            normalized.encode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise BackupError("备份包含非法文件路径") from error
        _require(normalized == path and not path.casefold().startswith((".vibeflow/uploads/", ".vibeflow/attempts/", '.vibeflow/attempt-provisions/', '.vibeflow/publications/', '.vibeflow/file-transactions/', '.vibeflow/live-store/')),
                 "备份包含路径别名、活动上传或缺少控制记录的执行版本")
        parts = path.split("/")
        _require(all(part == part.rstrip(" .") for part in parts), "备份包含尾点/空格路径别名")
        if os.name == "nt":
            reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
            _require(all(part.split(".")[0].upper() not in reserved for part in parts), "备份包含保留设备路径")
            _require(not any(ord(char) < 32 or char in '<>"|?*' for char in path) and
                all(len(part) <= 255 for part in parts), "备份包含无效Windows文件名")
        key = (row["case_id"], path.casefold() if os.name == "nt" else path)
        _require(key not in paths, "备份文件路径重复或存在大小写别名")
        paths[key] = row
    for cid, path in paths:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        while parent:
            _require((cid, parent) not in paths, "备份文件与父目录冲突")
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
    return cases, paths


def validate_case_dicts(cases, paths, read_content):
    for row in cases:
        canonical = "0-caseDict/caseDict"
        key = (row["id"], canonical.casefold() if os.name == "nt" else canonical)
        _require(key in paths and paths[key]["path"] == canonical, "算例缺少规范caseDict文件")
        if row["parse_status"] == "ok":
            from .casedict import parse_casedict, get_workflow_state
            try:
                content = read_content(paths[key]).decode("utf-8")
                parsed = parse_casedict(content)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise BackupError("caseDict文件与有效解析镜像不一致") from error
            meta = parsed.get("caseMeta", {})
            _require(get_workflow_state(content) == row["workflow_state"] and
                _json_object(row["case_meta"], "case_meta") == (meta if isinstance(meta, dict) else {}),
                "caseDict阶段/元数据与数据库镜像不一致，请先同步源算例")


def validate_manifest(manifest, *, keep_bytes=True, retain_encoded=True):
    cases, paths = validate_structure(manifest)
    total = 0
    for row in manifest["files"]:
        encoded = row.get("data")
        _require(isinstance(encoded, str) and len(encoded) <= 4*((MAX_SOURCE_BYTES+2)//3), "备份文件数据超限或无效")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError) as error:
            raise BackupError("备份文件Base64无效") from error
        total += len(content)
        _require(total <= MAX_SOURCE_BYTES, "备份文件总量超过100MiB")
        _require(hashlib.sha256(content).hexdigest() == row.get("sha256"), "备份文件hash不匹配")
        if keep_bytes:
            row["_bytes"] = content
            if not retain_encoded:
                del row["data"]
    if keep_bytes:
        validate_case_dicts(cases, paths, lambda row: row["_bytes"])
    else:
        validate_case_dicts(cases, paths, lambda row: base64.b64decode(row["data"], validate=True))
    return manifest


def read_archive(data, read):
    # Multipart uploads are seekable and already spooled by the HTTP layer.
    if isinstance(data, (bytes, bytearray, memoryview)):
        if len(data) > MAX_BACKUP_BYTES:
            raise BackupTooLarge("备份超过100MiB")
        source = io.BytesIO(data)
    else:
        source = data
        source.seek(0, io.SEEK_END)
        if source.tell() > MAX_BACKUP_BYTES:
            raise BackupTooLarge("备份超过100MiB")
        source.seek(0)
    try:
        with zipfile.ZipFile(source) as archive:
            _require(archive.namelist() == ["manifest.json"], "备份只允许manifest.json")
            info = archive.getinfo("manifest.json")
            _require(info.file_size <= MAX_MANIFEST_BYTES and not info.flag_bits & 1, "备份manifest超过160MiB或已加密")
            with archive.open(info) as stream:
                return read(stream)
    except (BackupError, CaseWriteConflict):
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError, UnicodeError,
            RuntimeError, NotImplementedError, RecursionError) as error:
        raise BackupError("无效备份ZIP/manifest") from error


def load_manifest(data, *, retain_encoded=True):
    def read(stream):
        raw = stream.read(MAX_MANIFEST_BYTES+1)
        _require(len(raw) <= MAX_MANIFEST_BYTES, "备份manifest超过160MiB")
        return json.loads(raw)

    manifest = read_archive(data, read)
    return validate_manifest(manifest, retain_encoded=retain_encoded)
