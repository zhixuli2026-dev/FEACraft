"""fs 分块上传会话：连续 offset、断点状态、SHA-256 完成校验和原子发布。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
import time
import uuid

from ..config import get_settings
from ..storage import _fs_path
from .case_write import CaseWriteConflict, file_hash, is_case_dictionary

MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
MAX_METADATA_BYTES = 65536
_LOCK = threading.RLock()
COMPLETED_EVENT = 'files.upload_completed'


class UploadError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _session_dir(prefix: str, upload_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise UploadError("上传会话不存在", 404)
    from .file_transactions import _path
    return _path(_fs_path(prefix, '.'), f".vibeflow/uploads/{upload_id}")


def _read_meta(prefix: str, upload_id: str) -> tuple[Path, dict]:
    directory = _session_dir(prefix, upload_id)
    return directory, _read_meta_at(directory, upload_id)


def _read_meta_at(directory: Path, upload_id: str, *, require_data=True) -> dict:
    try:
        from .file_transactions import _path
        with _path(directory, 'meta.json').open('rb') as stream:
            encoded = stream.read(65537)
        if len(encoded) > 65536:
            raise ValueError('Oversized upload metadata')
        meta = json.loads(encoded)
        if require_data and not _path(directory, 'data.part').is_file():
            raise ValueError('Upload staging file is missing')
        if (not isinstance(meta, dict) or meta.get('id') != upload_id
                or not isinstance(meta.get('path'), str) or type(meta.get('size')) is not int
                or not 1 <= meta['size'] <= MAX_UPLOAD_BYTES
                or type(meta.get('overwrite')) is not bool
                or not all(isinstance(meta.get(key), (int, float)) for key in ('created_at', 'updated_at'))):
            raise ValueError('Invalid upload metadata')
        for key in ('sha256', 'target_sha256'):
            value = meta.get(key)
            if value is not None and (not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)):
                raise ValueError('Invalid upload digest')
    except (OSError, ValueError, TypeError) as error:
        raise UploadError("上传会话不存在或元数据损坏", 404) from error
    return meta


def _encoded_meta(meta):
    encoded = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode('utf8')
    if len(encoded) > MAX_METADATA_BYTES:
        raise UploadError('上传元数据超过大小上限', 413)
    return encoded


def creation_metadata_peak(path, size):
    """Include the longest digest, boolean and finite timestamp encodings."""
    path = PurePosixPath(path.replace('\\', '/').lstrip('/')).as_posix()
    return len(_encoded_meta({'id':'0'*32, 'path':path, 'size':size,
        'sha256':'0'*64, 'target_sha256':'0'*64, 'overwrite':False,
        'created_at':-sys.float_info.max, 'updated_at':-sys.float_info.max}))


def chunk_metadata_peak(prefix, upload_id):
    _, meta = _read_meta(prefix, upload_id)
    updated = {**meta, 'updated_at':-sys.float_info.max}
    return max(len(_encoded_meta(meta)), len(_encoded_meta(updated)))


def _write_meta(directory: Path, meta: dict):
    """Caller reserves the encoded metadata together with its payload peak."""
    encoded = _encoded_meta(meta)
    temporary = directory / ".meta.tmp"
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / "meta.json")


def _status(meta: dict, part_size: int) -> dict:
    return {"id": meta["id"], "path": meta["path"], "size": meta["size"],
            "offset": part_size, "sha256": meta.get("sha256"),
            "created_at": meta["created_at"], "updated_at": meta["updated_at"]}


def initial_metadata(prefix, path, size, sha256, overwrite, upload_id, conditions=None):
    from .attempt_workspace import assert_mutable_path
    assert_mutable_path(prefix, path)
    path = PurePosixPath(path.replace("\\", "/").lstrip("/")).as_posix()
    if is_case_dictionary(prefix, path):
        raise UploadError("caseDict 请走专用端点", 400)
    if type(size) is not int or not 1 <= size <= MAX_UPLOAD_BYTES:
        raise UploadError("分块上传 size 必须为 1 字节至 1 GiB")
    if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise UploadError("sha256 必须为 64 位十六进制")
    if not conditions and get_settings().storage_backend == 'fs' and not overwrite and _fs_path(prefix,path).exists():
        raise UploadError("目标文件已存在；显式 overwrite=true 才允许替换", 409)
    if get_settings().storage_backend == 'fs':
        target_sha256 = file_hash(prefix, path)
    else:
        from .s3_streams import snapshot
        before, _, _ = snapshot(prefix, path)
        target_sha256 = before['sha256'] if before else None
    if conditions and path in conditions and target_sha256 != conditions[path]:
        raise CaseWriteConflict('file_conflict', '文件内容已变化，请重新检查目标', path=path)
    if target_sha256 is not None and not overwrite:
        raise UploadError("目标文件已存在；显式 overwrite=true 才允许替换", 409)
    now = time.time()
    return {"id": upload_id, "path": path, "size": size,
            "sha256": sha256.lower() if sha256 else None,
            "target_sha256": target_sha256,
            "overwrite": overwrite, "created_at": now, "updated_at": now}


def create_session(prefix: str, path: str, size: int, sha256: str | None, overwrite: bool) -> dict:
    if get_settings().storage_backend != "fs":
        from .s3_uploads import create_session as create_remote
        return create_remote(prefix, path, size, sha256, overwrite)
    meta = initial_metadata(prefix,path,size,sha256,overwrite,os.urandom(16).hex())
    directory = _session_dir(prefix, meta['id'])
    from .node_storage_budget import reserve
    with reserve(len(_encoded_meta(meta))):
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "data.part").write_bytes(b"")
        _write_meta(directory, meta)
    return _status(meta, 0)


def session_status(prefix: str, upload_id: str) -> dict:
    if get_settings().storage_backend != 'fs':
        from .s3_uploads import session_status as remote_status
        return remote_status(prefix, upload_id)
    _, meta = _read_meta(prefix, upload_id)
    part_size = (_session_dir(prefix, upload_id) / "data.part").stat().st_size
    return _status(meta, part_size)


def write_chunk(prefix: str, upload_id: str, offset: int, data: bytes) -> dict:
    if get_settings().storage_backend != 'fs':
        from .s3_uploads import write_chunk as remote_chunk
        return remote_chunk(prefix, upload_id, offset, data)
    if not data:
        raise UploadError("chunk 不能为空")
    if len(data) > MAX_CHUNK_BYTES:
        raise UploadError("单个 chunk 不能超过 8 MiB", 413)
    with _LOCK:
        directory, meta = _read_meta(prefix, upload_id)
        part = directory / "data.part"
        current = part.stat().st_size
        if offset != current:
            raise UploadError(f"offset 不连续：服务端当前为 {current}", 409)
        if current + len(data) > meta["size"]:
            raise UploadError("chunk 超出声明 size", 413)
        meta["updated_at"] = time.time()
        from .node_storage_budget import reserve
        # Existing part and metadata are already inventory. The append and the
        # replacement metadata must fit together before either writes bytes.
        with reserve(len(data) + len(_encoded_meta(meta))):
            with part.open("r+b") as stream:
                stream.seek(current)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _write_meta(directory, meta)
        return _status(meta, current + len(data))


def prepare_completion(prefix: str, upload_id: str):
    """Validate the immutable-to-this-request staging source; do not consume it."""
    from .attempt_workspace import assert_mutable_path
    from .durable_events import json_text
    from .file_transactions import FileSource, _capture
    from .local_file_edits import _version
    from .request_deadline import current

    directory, meta = _read_meta(prefix, upload_id)
    part = directory / 'data.part'
    version = _version(part)
    if version is None or version[2] != meta['size']:
        raise UploadError('文件尚未收齐，不能 complete', 409)
    # Verify the part without retaining either the process upload lock or a SQL lock.
    captured = _capture(part, deadline=current())
    if captured is None or captured['size'] != meta['size'] or _version(part) != version:
        raise UploadError('上传内容在校验期间发生变化，请保留会话重新检查', 409)
    if meta.get('sha256') and meta['sha256'] != captured['sha256']:
        raise UploadError('整文件 SHA-256 不匹配', 409)
    assert_mutable_path(prefix, meta['path'])
    if is_case_dictionary(prefix, meta['path']):
        raise UploadError('caseDict 请走专用端点', 400)
    if 'target_sha256' not in meta:
        raise UploadError('上传缺少目标版本，请重新创建上传会话', 409)
    if meta['target_sha256'] is not None and not meta['overwrite']:
        raise UploadError('目标文件已存在，拒绝覆盖', 409)
    result = {**_status(meta, meta['size']), 'sha256': captured['sha256'], 'status': 'completed'}
    receipt = {'result': result, 'metadata_sha256': hashlib.sha256(json_text(meta).encode()).hexdigest()}
    source = FileSource(f'.vibeflow/uploads/{upload_id}/data.part', captured)
    return receipt, source, meta['target_sha256'], version


def _confirm_local_source(prefix, upload_id, receipt, version):
    from .durable_events import json_text
    from .local_file_edits import _check

    directory, meta = _read_meta(prefix, upload_id)
    if (hashlib.sha256(json_text(meta).encode()).hexdigest() != receipt['metadata_sha256']
            or not _check(directory / 'data.part', version)):
        raise UploadError('上传暂存内容已变化，请保留现场重新核对', 409)


def _completion_id(case_id, upload_id):
    if not re.fullmatch('[0-9a-f]{32}', upload_id):
        raise UploadError('上传会话不存在', 404)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'vibeflow:upload:{case_id}:{upload_id}'))


async def completion_record(db, case, upload_id):
    from ..models import DurableEvent
    event = await db.get(DurableEvent, _completion_id(case.id, upload_id), populate_existing=True)
    if event is None:
        return None
    receipt = json.loads(event.payload_json)
    if event.case_id != case.id or event.type != COMPLETED_EVENT or receipt['result']['id'] != upload_id:
        raise UploadError('上传提交证明不匹配，请保留现场检查', 409)
    return receipt


async def complete_session(db, case, checked, upload_id):
    """Publish a preverified local upload and its receipt in one short SQL transaction."""
    from types import SimpleNamespace
    from . import case_copy_io, case_read_capacity, local_file_edits, upload_storage
    from .async_ops import finish_before_cancel
    from .case_provision import io
    from .case_write import coordinate
    from .durable_events import append_event
    from .local_file_budget import lock_for_prepared_edit
    from .owner_storage_budget import owner_for_case
    from .quota import check_locked_storage_quota
    from .request_deadline import STAGING_CLEANUP_SECONDS
    from .upload_recovery import guard
    from .upload_recovery_source import guard_complete

    reference = SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix)
    owner = await owner_for_case(db, case)
    owner_id = owner.id
    receipt = await completion_record(db, case, upload_id)
    selected = None
    if receipt is None:
        row = await upload_storage.row_for(db, case, owner, upload_id)
        if row is not None and row.state != 'active':
            raise UploadError('上传额度已结算，不能继续写入', 409)
        selected = await guard(db, case, upload_id)
    await finish_before_cancel(db.commit())

    async def current_case():
        value = await checked(db)
        if value.s3_prefix != reference.s3_prefix or (await owner_for_case(db, value)).id != owner_id:
            raise CaseWriteConflict('case_source_changed', '项目存储来源已变化，请刷新后重试')
        return value

    async with case_read_capacity.file_read_deadline('upload_complete', '完成上传', seconds=STAGING_CLEANUP_SECONDS):
        with case_copy_io.admit(code='upload_complete_busy', label='完成上传'):
            if receipt is None:
                try:
                    receipt, source, before, version = await io(prepare_completion, reference.s3_prefix, upload_id)
                    result = receipt['result']
                    if selected is not None:
                        await io(guard_complete, reference.s3_prefix, upload_id, selected)
                        await io(_confirm_local_source, reference.s3_prefix, upload_id, receipt, version)
                    inventory = await lock_for_prepared_edit(db, owner_id, wait=True)
                    case = await current_case()
                    owner = await owner_for_case(db, case)
                    await check_locked_storage_quota(db, owner, 2 * result['size'], inventory=inventory)
                    await finish_before_cancel(db.commit())

                    path = result['path']
                    async with local_file_edits.prepare_changes(reference, {path: source}, {path: before}) as prepared:
                        inventory = await lock_for_prepared_edit(db, owner_id, wait=True)
                        case = await current_case()
                        async with coordinate(db, case, defer_journal=True):
                            replay = await completion_record(db, case, upload_id)
                            if replay is not None:
                                receipt = replay
                            else:
                                owner = await owner_for_case(db, case)
                                row = await upload_storage.row_for(db, case, owner, upload_id)
                                if row is not None and row.state != 'active':
                                    raise UploadError('上传额度已结算，不能继续写入', 409)
                                if await guard(db, case, upload_id) != selected:
                                    raise UploadError('人工确认的上传来源已变化，请重新核对', 409)
                                await io(_confirm_local_source, case.s3_prefix, upload_id, receipt, version)
                                if row is None:
                                    await upload_storage.register(db, case, owner, result)
                                else:
                                    await upload_storage.progress(db, row, result)
                                await prepared.publish(db, case, inventory=inventory)
                                await upload_storage.settle(db, case, owner, upload_id, 'completed')
                                await append_event(db, case.id, COMPLETED_EVENT, receipt,
                                                   event_id=_completion_id(case.id, upload_id))
                        await finish_before_cancel(db.commit())
                except (UploadError, CaseWriteConflict, FileNotFoundError) as error:
                    # A concurrent completion can retire the source while we stage.
                    # Only its committed receipt authorizes an idempotent result.
                    await finish_before_cancel(db.rollback())
                    case = await current_case()
                    replay = await completion_record(db, case, upload_id)
                    if replay is None:
                        if isinstance(error, FileNotFoundError):
                            raise UploadError('上传暂存在完成期间发生变化，请核对上传状态', 409) from error
                        raise
                    receipt = replay
                    await finish_before_cancel(db.commit())
            result = dict(receipt['result'])
            if not await finish_completed(db, case, upload_id, receipt):
                result['cleanup_pending'] = True
            return result


def cleanup_completed(prefix, upload_id, receipt, observation=None):
    """Only discard staging that still matches the committed completion proof."""
    if get_settings().storage_backend != 'fs':
        if receipt.get('backend') != 's3':
            return False
        from .s3_uploads import cleanup_completed as cleanup_remote
        return cleanup_remote(prefix, upload_id, receipt)
    try:
        _cleanup_local(prefix, upload_id, receipt=receipt, observation=observation)
    except UploadError as error:
        if error.status_code == 409:
            return False
        raise
    return True


def _observe_completed_part(prefix, upload_id, receipt):
    from .file_transactions import _capture, _path
    from .local_file_edits import _version
    from .request_deadline import current

    directory = _session_dir(prefix, upload_id)
    retired = _path(_fs_path(prefix, '.'), f'.vibeflow/uploads/{upload_id}.completing')
    staging = directory if directory.exists() else retired
    part = staging / 'data.part'
    version = _version(part)
    if version is not None:
        expected = {key: receipt['result'][key] for key in ('size', 'sha256')}
        if _capture(part, limit=expected['size'], deadline=current()) != expected or _version(part) != version:
            raise UploadError('上传暂存文件与提交证明不一致', 409)
    return staging, version


def _cleanup_local(prefix, upload_id, *, receipt=None, reservation=None, observation=None):
    """Retire a checked directory atomically, then resume removing only its known files."""
    from .durable_events import json_text
    from .file_transactions import _capture, _path, _sync
    from .fs_directory import move_new, retry_sharing_violation
    directory = _session_dir(prefix, upload_id)
    root = _fs_path(prefix, '.')
    phase = 'completing' if receipt else 'cancelling'
    retired = _path(root, f'.vibeflow/uploads/{upload_id}.{phase}')
    active = directory.exists()
    if active and retired.exists():
        raise UploadError('上传会话与清理目录同时存在，请保留现场检查', 409)
    if not active and not retired.exists():
        if receipt or reservation is not None:
            return
        raise UploadError('上传会话不存在', 404)
    staging = directory if active else retired
    if not staging.is_dir():
        raise UploadError('上传暂存目录类型不匹配', 409)
    parts = {'meta.json', 'data.part'}
    allowed = parts if receipt else parts | {'.meta.tmp'}
    names = set()
    with os.scandir(staging) as entries:
        for entry in entries:
            if entry.name not in allowed:
                raise UploadError('上传暂存包含未知文件，请保留现场检查', 409)
            names.add(entry.name)
    if (active and not parts <= names) or (names and 'meta.json' not in names):
        raise UploadError('上传暂存内容不完整或包含未知文件，请保留现场检查', 409)
    for name in names:
        if not _path(staging, name).is_file():
            raise UploadError('上传暂存文件类型不匹配', 409)
    if names:
        meta = _read_meta_at(staging, upload_id, require_data=False)
        if reservation and (meta['path'], meta['size']) != reservation:
            raise UploadError('上传暂存与额度记录不一致', 409)
        if receipt:
            if hashlib.sha256(json_text(meta).encode()).hexdigest() != receipt['metadata_sha256']:
                raise UploadError('上传暂存元数据与提交证明不一致', 409)
            if 'data.part' in names:
                if observation is None:
                    expected = {key: receipt['result'][key] for key in ('size', 'sha256')}
                    valid = _capture(staging / 'data.part') == expected
                else:
                    from .local_file_edits import _check
                    valid = staging == observation[0] and observation[1] is not None and _check(
                        staging / 'data.part', observation[1])
                if not valid:
                    raise UploadError('上传暂存文件与提交证明不一致', 409)
    if active:
        move_new(directory, retired, root=root)
        _sync(retired.parent)
    for name in ('.meta.tmp', 'data.part', 'meta.json'):
        if name in names:
            retry_sharing_violation(lambda: _path(retired, name).unlink())
            _sync(retired)
    retry_sharing_violation(retired.rmdir)
    _sync(retired.parent)


async def finish_completed(db, case, upload_id, receipt):
    """Best-effort staging cleanup after known SQL commit, safe to retry."""
    import logging
    from .async_ops import finish_before_cancel
    from .case_provision import io
    from .case_write import coordinate
    from .file_transactions import finish_committed
    await finish_committed(db, case)
    case_id = case.id
    try:
        observation = await io(_observe_completed_part, case.s3_prefix, upload_id, receipt)
        async with coordinate(db, case, content_write=False, allow_deleted=True, defer_journal=True):
            if await completion_record(db, case, upload_id) != receipt:
                raise UploadError('上传缺少提交证明', 409)
            cleaned = await io(cleanup_completed, case.s3_prefix, upload_id, receipt, observation)
        await finish_before_cancel(db.commit())
    except (OSError, ValueError, CaseWriteConflict):
        await finish_before_cancel(db.rollback())
        logging.getLogger(__name__).warning('Committed upload staging retained: case=%s upload=%s',
                                            case_id, upload_id, exc_info=True)
        return False
    return cleaned


def cancel_session(prefix: str, upload_id: str, reservation=None):
    with _LOCK:
        _cleanup_local(prefix, upload_id, reservation=reservation)


async def cancel_with_ledger(db,case,upload_id,*,user_id,validate=None,verify=None,record=None):
    """The ordinary cancellation path, optionally bound to an audited review."""
    import asyncio
    from . import upload_storage,case_read_capacity
    from .async_ops import finish_before_cancel
    from .case_write import CaseWriteConflict, coordinate
    from .. import storage
    if storage._use_s3():
        from .manual_file_recovery import authorized
        from .s3_uploads import cancel

        case_id, prefix = case.id, case.s3_prefix

        async def checked(session):
            current = await authorized(session, case_id, user_id, write=True)
            if current.s3_prefix != prefix:
                raise CaseWriteConflict('case_source_changed', '项目存储位置已变化，请刷新后重试')
            return current

        await finish_before_cancel(db.commit())
        return await cancel(checked, upload_id, validate=validate, verify=verify, record=record)

    async with case_read_capacity.admitted(db,case) as storage_owner:
        if await upload_storage.row_for(db,case,storage_owner,upload_id) is None:
            # Legacy uploads need a bound retry record before the last staging
            # file can disappear. Recheck under the lock in both transactions.
            async with coordinate(db,case):
                row=await upload_storage.row_for(db,case,storage_owner,upload_id)
                if await completion_record(db,case,upload_id) is not None:
                    raise UploadError('上传已经提交，不能取消已完成文件',409)
                if row is None:
                    if validate is not None:await validate(db,case,row)
                    status=await finish_before_cancel(asyncio.to_thread(session_status,case.s3_prefix,upload_id))
                    if not 0 <= status['offset'] <= status['size']:
                        raise UploadError('上传暂存长度无效，请保留现场检查',409)
                    await upload_storage.register(db,case,storage_owner,status)
            await finish_before_cancel(db.commit())
        async with coordinate(db,case):
            if await completion_record(db,case,upload_id) is not None:
                raise UploadError('上传已经提交，不能取消已完成文件',409)
            row=await upload_storage.row_for(db,case,storage_owner,upload_id)
            if validate is not None:await validate(db,case,row)
            already_cancelled = row is not None and row.state == 'cancelled'
            if not already_cancelled:
                if row is not None:
                    from .upload_creation import prepare_cancel
                    await prepare_cancel(db,case,upload_id)
                bound = (row.path,row.size) if row is not None else None
                await finish_before_cancel(asyncio.to_thread(cancel_session,case.s3_prefix,upload_id,bound))
            await upload_storage.settle(db,case,storage_owner,upload_id,'cancelled')
            if record is not None:await record(db,case)
        await finish_before_cancel(db.commit())
    return {'ok':True,'cancelled':True}
