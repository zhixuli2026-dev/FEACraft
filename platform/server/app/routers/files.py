"""算例文件 API — S3 为唯一事实源；caseDict 读写带状态机镜像同步"""
import asyncio
from contextlib import closing
from functools import partial
from pathlib import PurePosixPath
from types import SimpleNamespace
import hashlib
import math
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, UploadFile
from ..upload_routes import UploadCleanupRoute
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services.casedict import (CASE_DICT_PATH, UNCHANGED_DICT_ENTRY, LiteralDictString, WorkflowGateError, get_workflow_state,
                                 parse_casedict, set_block, set_workflow_state)
from ..services.casedict_schema import case_meta_options, error_counts
from ..services.async_ops import finish_before_cancel
from ..services.file_read import FileRead, FileReadConflict
from ..services import openfoam_editor
from ..services.request_deadline import scope as deadline_scope
from ..services.case_write import CaseWriteConflict, bounded_case_read, coordinate, read_file_record, normalize_path, is_case_dictionary
from ..services import case_meta, file_transactions, owner_storage_budget, case_read_capacity
from ..services import material_library
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1/cases/{case_id}", tags=["files"],route_class=UploadCleanupRoute)

_TEXT_EXT = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".c", ".cpp", ".h", ".hpp",
             ".sh", ".foam", ".step", ".stp", ".csv", ".tsv", ".log", ".msh", ".comm", ".html", ".htm", ".css", ".js", ".xml", ".svg",
             ".tex", ".bib", ".cls", ".sty", ".bst", ".inp", ".cfg", ".geo", ".in", ".data", ""}
_DICT_PATH = CASE_DICT_PATH
_MAX_INLINE_UPLOAD = 16 * 1024 * 1024


def _norm_path(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/").lstrip("/")).as_posix()


def _is_text_path(path: str) -> bool:
    import os
    _, ext = os.path.splitext(path.lower())
    return ext in _TEXT_EXT or "/" not in path and "." not in path


def _ordinary_write_path(prefix, path):
    try:
        norm = normalize_path(_norm_path(path))
        if is_case_dictionary(prefix, norm):
            raise HTTPException(400, "caseDict 请走专用端点，不可普通上传、写入或删除")
        return norm
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


async def _save_ordinary_file(db, case, user, path, data, *, revision, conditions, content_type=None):
    from ..services import ordinary_file_edits

    checked = partial(_locked_recovery_case, case_id=case.id, user_id=user.id, write=True, prefix=case.s3_prefix)
    try:
        await ordinary_file_edits.save(db, case, checked, path, lambda _prefix: data, revision=revision,
                                      conditions=conditions, content_type=content_type)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


# ---------- 文件树与读写 ----------
@router.get("/file-transactions")
async def file_transaction_status(case_id: int, user: User = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db),
                                  include_preparations: bool = Query(False),
                                  preparation_limit: int = Query(100, ge=1, le=200),
                                  preparation_after: str = Query('', max_length=1024)):
    from ..services.durable_events import ensure_parent_transaction, lock_case_execution
    from ..services.case_write import assert_case_available
    from ..services import file_transaction_inspection as inspection
    from ..services import storage_operations
    user_id = user.id
    async with case_read_capacity.file_read_deadline(inspection.CODE, inspection.LABEL):
        case = await get_owned_case(case_id, user, db, write=False)
        reference = SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix)
        operation = await storage_operations.status_locked(db, case.id)
        active = await storage_operations.active(db, case.id) if operation is not None else None
        owner_json = active.owner_json if active is not None else None
        await finish_before_cancel(db.rollback())
        can_inspect = operation is None
        if owner_json is not None:
            try:
                can_inspect = await bounded_case_read(storage_operations.owner_exited, owner_json,
                                                     code=inspection.CODE, label=inspection.LABEL)
            except (ValueError, TypeError, KeyError, AttributeError):
                can_inspect = False
        observed = None
        if can_inspect:
            observed = await bounded_case_read(inspection.observe, reference,
                                               code=inspection.CODE, label=inspection.LABEL)
        preparations = None
        if include_preparations:
            from ..services import file_preparation_inventory
            from botocore.exceptions import BotoCoreError, ClientError

            result = operation if observed is None else observed.status
            try:
                preparations = await bounded_case_read(partial(file_preparation_inventory.inspect, reference.s3_prefix,
                    limit=preparation_limit, after=preparation_after, pending_id=result.get('transaction_id')),
                    code=inspection.CODE, label=inspection.LABEL)
            except (OSError, ValueError, BotoCoreError, ClientError):
                preparations = {'status': 'unavailable', 'read_only': True}
        await ensure_parent_transaction(db)
        await lock_case_execution(db, case_id)
        current = await db.get(User, user_id, populate_existing=True)
        if current is None or current.status != 'active':
            raise HTTPException(403, '用户已不可用')
        case = await get_owned_case(case_id, current, db, write=False)
        assert_case_available(case)
        if case.s3_prefix != reference.s3_prefix:
            raise CaseWriteConflict('case_source_changed', '项目来源已变化，请重新读取')
        latest = await storage_operations.status_locked(db, case_id)
        if latest == operation and observed is not None:
            committed = await inspection.read_witness(db, case, observed)
            await finish_before_cancel(db.rollback())
            result = await bounded_case_read(inspection.confirm, reference, observed, committed,
                                             code=inspection.CODE, label=inspection.LABEL)
            if latest is not None and result['status'] == 'idle':
                result = {**latest, 'status': 'recovery_required', 'code': 'file_recovery_required'}
        elif latest is None:
            raise CaseWriteConflict('file_conflict', '文件事务已变化，请重新读取')
        else:
            result = latest
        if include_preparations:
            result['preparations'] = (preparations if latest == operation
                                      else {'status': 'unavailable', 'read_only': True})
        return result


@router.post('/file-transactions:recover')
async def recover_file_transactions(case_id:int,user:User=Depends(get_current_user)):
    from ..services import case_journal_recovery

    checked = partial(_locked_recovery_case, case_id=case_id, user_id=user.id, write=True)
    if storage._use_s3():
        from ..services.s3_storage_recovery import recover
        return {'result':await recover(checked)}
    await case_journal_recovery.recover_local(checked)
    return {'result': None}


async def _locked_recovery_case(db, *, case_id, user_id, write, prefix=None):
    from ..services.durable_events import lock_case_execution
    from ..services.case_write import assert_case_available

    await lock_case_execution(db, case_id)
    current = await db.get(User, user_id, populate_existing=True)
    if current is None or current.status != 'active':
        raise HTTPException(403, '用户已不可用')
    case = await get_owned_case(case_id, current, db, write=write)
    assert_case_available(case)
    if prefix is not None and case.s3_prefix != prefix:
        raise CaseWriteConflict('case_source_changed', '项目来源已变化，请先核对恢复目标')
    return case


async def _case_for_file_request(case_id, user, db, *, write=True):
    """Handle existing cold journals before taking the editor's case/owner locks."""
    from ..services import case_journal_recovery

    case = await get_owned_case(case_id, user, db, write=write)
    user_id = user.id
    local_reference = None
    remote = storage._use_s3()
    if remote:
        from ..services.s3_file_transactions import pending_version
        from ..services.storage_operations import status_locked
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            pending = await bounded_case_read(pending_version, case.s3_prefix,
                                               code='file_recovery', label='文件事务恢复')
        except (ValueError, OSError, BotoCoreError, ClientError) as error:
            raise file_transactions._conflict('S3 file transaction cannot be verified; preserve remote objects for review') from error
        operation = await status_locked(db, case_id)
        if operation is None and pending is None:
            return case
    else:
        local_reference = case_journal_recovery.pending_local(case)
        if local_reference is None:
            return case
    checked = partial(_locked_recovery_case, case_id=case_id, user_id=user_id, write=write, prefix=case.s3_prefix)
    await finish_before_cancel(db.rollback())
    if remote:
        await case_journal_recovery.recover_remote(checked)
    else:
        await case_journal_recovery.recover_local(checked, local_reference)
    await db.refresh(user)
    if user.status != 'active':
        raise HTTPException(403, '用户已不可用')
    return await get_owned_case(case_id, user, db, write=write)


async def _read_file_tree(db, case, user, select_entries, *args, code, label):
    from ..services.case_journal_recovery import require_settled
    from ..services.case_tree import listing_marker
    from ..services.durable_events import ensure_parent_transaction
    from ..services.event_replay import latest_sequence
    from ..services.storage_operations import admit_locked

    case_id, prefix, user_id = case.id, case.s3_prefix, user.id
    # Local filesystem readers can finish while peers wait on this request's
    # existing budget. Keep S3 recovery and owner-credit admission unchanged.
    local_read = not storage._use_s3()
    async with case_read_capacity.file_read_deadline(code, label):
        async def checkpoint():
            watermark = await latest_sequence(db, case_id)
            marker = await bounded_case_read(listing_marker, prefix, code=code, label=label,
                                             wait_for_capacity=local_read)
            return watermark, marker

        async with case_read_capacity.reading(db, case, defer_journal=True):
            before = await checkpoint()
        await finish_before_cancel(db.rollback())

        for _ in range(2):
            entries = None
            interrupted = False
            try:
                entries = await bounded_case_read(select_entries, prefix, *args, code=code, label=label,
                                                  wait_for_capacity=local_read)
            except (FileNotFoundError, NotADirectoryError):
                # A directory can disappear while an unlocked scan is walking it.
                interrupted = True
            try:
                await ensure_parent_transaction(db)
                case = await _locked_recovery_case(db, case_id=case_id, user_id=user_id, write=False, prefix=prefix)
                if not local_read:
                    # The initial read fenced the catalog. Observe its identity
                    # here instead of advancing it and invalidating the scan.
                    await case_read_capacity.read_ready(db, case)
                else:
                    await admit_locked(db, case_id)
                    await require_settled(case)
                after = await checkpoint()
            finally:
                await finish_before_cancel(db.rollback())
            if not interrupted and before == after:
                return entries, after[0]
            before = after

    raise CaseWriteConflict('file_tree_changed', '文件树在读取期间变化，请刷新后重试')


@router.get("/tree")
async def tree(case_id: int, user: User = Depends(get_current_user),
               db: AsyncSession = Depends(get_db)):
    from ..services.case_tree import full_entries

    case = await _case_for_file_request(case_id, user, db, write=False)
    entries, watermark = await _read_file_tree(db, case, user, full_entries, code='file_tree', label='文件树读取')
    return {"entries": entries, "watermark": watermark}


class TreePaths(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=256)


@router.post('/tree:patch')
async def tree_patch(case_id: int, body: TreePaths, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    from ..services.attempt_workspace import canonical_path
    from ..services.case_tree import selected_entries

    case = await _case_for_file_request(case_id, user, db, write=False)
    paths = tuple(dict.fromkeys(body.paths))
    try:
        if sum(len(path.encode('utf-8')) for path in paths) > 8192 or any(
                canonical_path(path) != path or path.split('/', 1)[0].casefold() == '.vibeflow' for path in paths):
            raise ValueError('Invalid tree paths')
    except (ValueError, UnicodeError) as error:
        raise HTTPException(422, '文件路径超出刷新范围，请刷新文件树') from error
    entries, _ = await _read_file_tree(db, case, user, selected_entries, paths, code='tree_patch', label='文件树刷新')
    return {'entries': entries}


async def _editor_reader(db, case, path, code, label):
    async with case_read_capacity.reading(db, case):
        reader = await bounded_case_read(FileRead, case.s3_prefix, path, code=code, label=label)
        revision = case.dict_revision
    # The selected handle or object version is checked while reading. Parsing
    # and hashing can run after releasing the transaction and owner grant.
    await finish_before_cancel(db.rollback())
    return reader, revision


@router.get("/file")
async def read_file(case_id: int, path: str = Query(...),
                    user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db, write=False)
    try:
        async with case_read_capacity.file_read_deadline('editor_read', '文件读取'):
            ordinary_text = _is_text_path(path)
            probe_header = openfoam_editor.wants_header(path, ordinary_text)
            remote = storage._use_s3()
            async with case_read_capacity.reading(db, c):
                reader = None
                if ordinary_text or probe_header or not remote:
                    reader = await bounded_case_read(FileRead, c.s3_prefix, path, code='editor_read', label='文件读取')
                declared = await bounded_case_read(openfoam_editor.stream_format, reader,
                    code='editor_read', label='文件读取') if probe_header else None
                text = declared == 'ascii' or ordinary_text and declared != 'binary'
                revision = c.dict_revision
                if not text and remote:
                    size, download_url = await bounded_case_read(_signed_download, c.s3_prefix, path,
                                                                 code='editor_read', label='文件读取')
                elif not text:
                    size, download_url = reader.size, f"/api/v1/cases/{case_id}/raw?path={quote(path, safe='')}"
            await finish_before_cancel(db.rollback())
            if not text:
                return {'path': path, 'kind': 'binary', 'size': size, 'download_url': download_url}
            content, digest = await bounded_case_read(reader.text_snapshot, code='editor_read', label='文件读取')
            response = {'path': path, 'kind': 'text', 'content': content, 'size': reader.size,
                        'content_sha256': digest, 'dict_revision': revision}
            if declared == 'ascii':
                response['language'] = 'openfoam-dict'
            return response
    except FileNotFoundError as error:
        raise HTTPException(404, '文件不存在') from error
    except FileReadConflict as error:
        raise HTTPException(409, {'code':'file_changed_during_read','message':'文件在读取期间发生变化，请重新读取'}) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def _signed_download(prefix, path):
    return storage.s3().download_metadata(get_settings().s3_bucket, storage._s3_key(prefix, path), 3600)


@router.get("/file:version")
async def file_version(case_id: int, path: str = Query(...),
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db, write=False)
    try:
        norm = normalize_path(path)
        async with case_read_capacity.reading(db, c):
            record = await read_file_record(c.s3_prefix, norm)
            return {"path":norm, "exists":record is not None, "content_sha256":record['sha256'] if record is not None else None,
                    "size":record['size'] if record is not None else 0,
                    "dict_revision":c.dict_revision, "protected":is_case_dictionary(c.s3_prefix, norm)}
    except CaseWriteConflict:
        raise
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


class WriteIn(BaseModel):
    path: str
    content: str
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    expected_sha256: str | None = None


class UploadSessionIn(BaseModel):
    path: str
    size: int
    sha256: str | None = None
    overwrite: bool = False
    expected_sha256: str | None = None


@router.put("/file")
async def write_file(case_id: int, body: WriteIn, request: Request, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db)
    norm = _ordinary_write_path(c.s3_prefix, body.path)
    data = body.content.encode('utf8')
    digest = hashlib.sha256(data).hexdigest()
    conditions = {norm: body.expected_sha256} if 'expected_sha256' in body.model_fields_set else {}
    await _save_ordinary_file(db, c, user, norm, data, revision=body.expected_revision,
                              conditions=conditions, content_type='text/plain; charset=utf-8')
    receipt = {"ok": True, "auto_preview_job": None, "content_sha256": digest}
    if norm == "A-geo/geometry.py":
        # 原版 Auto-Preview 语义：几何脚本保存即自动重建预览（第一刀：显式作业，非文件监听）
        from .geometry import _launch
        preview = await _launch(case_id, "preview", user, db, saved_source_sha256=digest, request=request)
        receipt['auto_preview_job'] = preview['job_id']
        if 'auto_preview_error' in preview:
            receipt['auto_preview_error'] = preview['auto_preview_error']
    return receipt


@router.post("/upload")
async def upload_file(case_id: int, file: UploadFile, path: str = Query(...),
                      expected_revision: int | None = Query(None, ge=0),
                      expected_sha256: str | None = Query(None),
                      expected_absent: bool = Query(False),
                      user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db)
    norm = _ordinary_write_path(c.s3_prefix, path)
    if expected_absent and expected_sha256 is not None:
        raise HTTPException(422, "expected_absent 与 expected_sha256 不可同时指定")
    data = await file.read(_MAX_INLINE_UPLOAD + 1)
    if len(data) > _MAX_INLINE_UPLOAD:
        raise HTTPException(413, "普通上传不能超过 16 MiB，请使用分块上传")
    conditions = {norm: expected_sha256} if expected_absent or expected_sha256 is not None else {}
    await _save_ordinary_file(db, c, user, norm, data, revision=expected_revision,
                              conditions=conditions, content_type=file.content_type or 'application/octet-stream')
    return {"ok": True, "size": len(data), "content_sha256":hashlib.sha256(data).hexdigest()}


@router.post("/uploads")
async def create_upload_session(case_id: int, body: UploadSessionIn,
                                user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db),
                                idempotency_key: str | None = Header(None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    from ..services.quota import check_storage_quota
    from ..services.uploads import UploadError, create_session
    try:
        norm = normalize_path(body.path)
        conditions = {norm:body.expected_sha256} if "expected_sha256" in body.model_fields_set else {}
        if idempotency_key is not None:
            from ..services import upload_creation
            if body.expected_sha256 is not None and not re.fullmatch('[a-f0-9]{64}', body.expected_sha256):
                raise ValueError('expected_sha256 必须为 64 位小写十六进制')
            declaration = {'path':norm, 'size':body.size, 'sha256':body.sha256.lower() if body.sha256 is not None else None,
                           'overwrite':body.overwrite}
            return await upload_creation.create(db, case, user, idempotency_key, declaration, conditions)
        async with owner_storage_budget.hold_case(db, case) as storage_owner:
            capacity = owner_storage_budget.current()
            if capacity is not None:
                capacity.reserve_future(body.size)
            else:
                from ..services.uploads import creation_metadata_peak
                extra = creation_metadata_peak(_norm_path(body.path), body.size)
                await check_storage_quota(db, storage_owner, body.size + extra)
            async with coordinate(db, case, expected_files=conditions):
                result = await finish_before_cancel(asyncio.to_thread(create_session, case.s3_prefix, _norm_path(body.path),
                                                body.size, body.sha256, body.overwrite))
                from ..services.upload_storage import register
                await register(db, case, storage_owner, result)
            await finish_before_cancel(db.commit())
        return result
    except UploadError as error:
        raise HTTPException(error.status_code, str(error)) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/upload-intent")
@router.delete("/upload-intent")
async def upload_intent(case_id:int, request:Request, idempotency_key:str=Header(alias='Idempotency-Key'),
                               user:User=Depends(get_current_user), db:AsyncSession=Depends(get_db)):
    from ..services import upload_creation
    from ..services.uploads import UploadError
    case = await get_owned_case(case_id, user, db)
    try:
        operation = upload_creation.cancel if request.method == 'DELETE' else upload_creation.lookup
        return await operation(db, case, user, idempotency_key)
    except UploadError as error:
        raise HTTPException(error.status_code, str(error)) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/uploads")
async def list_upload_sessions(case_id: int, user: User = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    storage_owner = await owner_storage_budget.owner_for_case(db, case)
    from ..models import UploadStorageReservation
    from ..services.upload_storage import binding
    from sqlalchemy import select
    rows = await db.scalars(select(UploadStorageReservation).where(
        UploadStorageReservation.case_id == case.id,
        UploadStorageReservation.state == 'active').order_by(UploadStorageReservation.created_at))
    return [{'id': row.id, 'path': row.path, 'size': row.size,
             'remaining_bytes': row.remaining_bytes,
             'storage_binding_valid': row.binding_json == binding(case),
             'owner_reconciliation_required': row.user_id != storage_owner.id,
             'can_reconcile_owner': user.id == storage_owner.id} for row in rows]


@router.post("/uploads/{upload_id}:reconcile-owner")
async def reconcile_upload_owner(case_id: int, upload_id: str,
                                 user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    from ..services.upload_owner_reconciliation import reconcile
    from ..services.uploads import UploadError
    try:
        return await reconcile(db, case, user, upload_id)
    except UploadError as error:
        raise HTTPException(error.status_code, str(error)) from error


@router.get("/uploads/{upload_id}")
async def upload_session_status(case_id: int, upload_id: str,
                                user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    from ..services.uploads import UploadError, session_status, completion_record
    try:
        async with case_read_capacity.reading(db, case):
            completed = await completion_record(db, case, upload_id)
            if completed is not None:
                return dict(completed['result'])
            from ..services.manual_upload_recovery import require_open
            await require_open(db,case,upload_id)
            return await finish_before_cancel(asyncio.to_thread(session_status, case.s3_prefix, upload_id))
    except UploadError as error:
        raise HTTPException(error.status_code, str(error)) from error


async def _upload_chunk_body(request, maximum):
    declared = request.headers.get('Content-Length')
    if declared is not None:
        try:
            if not declared.isascii() or not declared.isdecimal():
                raise ValueError('Invalid content length')
            length = int(declared)
        except ValueError:
            raise HTTPException(400, 'Content-Length 必须为非负整数') from None
        if length > maximum:
            raise HTTPException(413, '单个 chunk 不能超过 8 MiB')
    data = bytearray()
    async for chunk in request.stream():
        if len(chunk) > maximum - len(data):
            raise HTTPException(413, '单个 chunk 不能超过 8 MiB')
        data.extend(chunk)
    if not data:
        raise HTTPException(400, 'chunk 必须为 1 字节至 8 MiB')
    return bytes(data)


@router.put("/uploads/{upload_id}")
async def write_upload_chunk(case_id: int, upload_id: str, request: Request,
                             offset: int = Query(..., ge=0),
                             user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    from ..services.uploads import MAX_CHUNK_BYTES, UploadError, write_chunk, completion_record
    data = await _upload_chunk_body(request, MAX_CHUNK_BYTES)
    from ..services import upload_storage
    from ..services.quota import check_storage_quota
    try:
        if storage._use_s3():
            from ..services.manual_file_recovery import authorized
            from ..services.s3_uploads import write_chunk_reserved

            user_id, prefix = user.id, case.s3_prefix

            async def checked(session):
                current = await authorized(session, case_id, user_id, write=True)
                if current.s3_prefix != prefix:
                    raise CaseWriteConflict('case_source_changed', '项目存储位置已变化，请刷新后重试')
                return current

            await finish_before_cancel(db.commit())
            return await write_chunk_reserved(checked, upload_id, offset, data)

        async with case_read_capacity.admitted(db,case) as storage_owner:
            async with coordinate(db, case):
                if await completion_record(db, case, upload_id) is not None:
                    raise UploadError('上传已经提交，不能继续写入分块', 409)
                from ..services.upload_recovery import guard
                selected=await guard(db,case,upload_id)
                if selected is not None:
                    from ..services.upload_recovery_source import guard_chunk
                    await finish_before_cancel(asyncio.to_thread(guard_chunk,case.s3_prefix,upload_id,offset,data,selected))
                reservation,_=await upload_storage.reconcile(db,case,storage_owner,upload_id)
                from ..services.uploads import chunk_metadata_peak
                extra = await finish_before_cancel(asyncio.to_thread(chunk_metadata_peak, case.s3_prefix, upload_id))
                await check_storage_quota(db,storage_owner,extra)
                result = await finish_before_cancel(asyncio.to_thread(write_chunk, case.s3_prefix, upload_id, offset, data))
                await upload_storage.progress(db, reservation, result)
            await finish_before_cancel(db.commit())
        return result
    except UploadError as error:
        raise HTTPException(error.status_code, str(error)) from error


@router.post("/uploads/{upload_id}:complete")
async def complete_upload_session(case_id: int, upload_id: str,
                                  user: User = Depends(get_current_user),
                                  db: AsyncSession = Depends(get_db)):
    from ..services.uploads import UploadError, complete_session

    user_id = user.id

    async def checked(session):
        current = await session.get(User, user_id, populate_existing=True)
        if current is None or current.status != 'active':
            raise HTTPException(403, '用户已不可用')
        return await get_owned_case(case_id, current, session)

    try:
        if storage._use_s3():
            from ..services.s3_uploads import complete

            await checked(db)
            await finish_before_cancel(db.commit())
            return {"ok": True, **await complete(checked, upload_id)}
        case = await _case_for_file_request(case_id, user, db)
        result = await complete_session(db, case, checked, upload_id)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except UploadError as error:
        raise HTTPException(error.status_code, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {"ok": True, **result}


@router.delete("/uploads/{upload_id}")
async def cancel_upload_session(case_id: int, upload_id: str,
                                user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    from ..services.uploads import UploadError, cancel_with_ledger
    try:
        return await cancel_with_ledger(db,case,upload_id,user_id=user.id)
    except UploadError as error:
        raise HTTPException(error.status_code, str(error)) from error


@router.delete("/file")
async def delete_file(case_id: int, path: str = Query(...),
                      expected_revision: int | None = Query(None, ge=0),
                      expected_sha256: str | None = Query(None),
                      user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db)
    norm = _ordinary_write_path(c.s3_prefix, path)
    conditions = {norm: expected_sha256} if expected_sha256 is not None else {}
    await _save_ordinary_file(db, c, user, norm, None, revision=expected_revision, conditions=conditions)
    return {"ok": True}


# ---------- 原始字节下载（PDF/图片查看器等需要真实字节流） ----------
def _parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if not value.startswith("bytes="):
        raise ValueError("unsupported range unit")
    spec = value[6:].strip()
    start_text, sep, end_text = spec.partition("-")
    if not sep:
        raise ValueError("invalid range")
    try:
        if not start_text:
            length = int(end_text)
            if length <= 0 or size == 0:
                raise ValueError("invalid suffix range")
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_text)
            if start < 0 or start >= size:
                raise ValueError("range starts beyond file")
            if not end_text:
                end = size - 1
            else:
                end = int(end_text)
                if end < start:
                    raise ValueError("range ends before start")
                end = min(end, size - 1)
    except (TypeError, ValueError):
        raise ValueError("invalid range") from None
    if start < 0 or end < start or end >= size:
        raise ValueError("unsatisfiable range")
    return start, end


def _parse_ranges(value: str | None, size: int) -> list[tuple[int, int]] | None:
    if value is None:
        return None
    specs = value[6:].split(",") if value.startswith("bytes=") else []
    if not specs or len(specs) > 16:
        raise ValueError("invalid range count")
    ranges = [_parse_range("bytes=" + spec.strip(), size) for spec in specs]
    ordered = sorted(ranges)
    if any(left[1] >= right[0] for left, right in zip(ordered, ordered[1:])):
        raise ValueError("overlapping ranges")
    if sum(end - start + 1 for start, end in ranges) > 64 * 1024 * 1024:
        raise ValueError("range payload too large")
    return ranges


def _multipart_ranges(ranges, size, mime):
    import uuid
    boundary = f"vibeflow-{uuid.uuid4().hex}"
    parts = []
    length = 0
    for start, end in ranges:
        header = (f"--{boundary}\r\nContent-Type: {mime}\r\n"
                  f"Content-Range: bytes {start}-{end}/{size}\r\n\r\n").encode()
        parts.extend((header, (start, end), b"\r\n"))
        length += len(header) + end - start + 1 + 2
    closing_boundary = f"--{boundary}--\r\n".encode()
    parts.append(closing_boundary)
    return parts, boundary, length + len(closing_boundary)


@router.get("/raw")
@router.head("/raw")
async def raw_file(case_id: int, request: Request, path: str = Query(...),
                   range_header: str | None = Header(default=None, alias="Range"),
                   file_version: str | None = Header(default=None, alias="X-Vibeflow-File-Version",
                                                     pattern=r"^[0-9a-f]{64}$"),
                   user: User = Depends(get_current_user),
                   db: AsyncSession = Depends(get_db)):
    from ..raw_download import begin

    c = await _case_for_file_request(case_id, user, db, write=False)
    download = begin(request) if request.method == 'GET' else None
    try:
        async with deadline_scope(get_settings().storage_measurement_timeout_seconds):
            async with case_read_capacity.reading(db, c):
                reader = await bounded_case_read(FileRead, c.s3_prefix, path, code='raw_download', label='文件下载')
                if file_version is not None and file_version != reader.version:
                    raise FileReadConflict('The requested file version is no longer available')
            # FileRead validates the pinned identity during every read. Release
            # the case transaction and owner recovery grant before sourcing a
            # large response or waiting on the client.
            await finish_before_cancel(db.rollback())
            return await bounded_case_read(_raw_response, reader, path, range_header, request.method, download,
                                           code='raw_download', label='文件下载')
    except FileNotFoundError as error:
        if file_version is not None:
            raise HTTPException(409, detail={"code": "file_changed_during_read",
                                "message": "文件已被删除或替换，请重新读取"}) from error
        raise HTTPException(404, "文件不存在") from error
    except FileReadConflict as error:
        raise HTTPException(409, detail={"code": "file_changed_during_read",
                            "message": "文件在读取期间发生变化，请重新读取"}) from error
    except CaseWriteConflict:
        raise
    except TimeoutError as error:
        raise HTTPException(504, {'code':'raw_download_timeout','message':'文件下载准备超时，请稍后重试'}) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def _raw_layout(reader, path, range_header):
    import mimetypes
    size = reader.size
    headers = {"Accept-Ranges": "bytes", "X-Vibeflow-File-Version": reader.version,
               "Cache-Control": "no-store"}
    try:
        spans = _parse_ranges(range_header, size)
    except ValueError:
        return 416, None, [], 0, {
            **headers, "Content-Range": f"bytes */{size}",
            "Content-Length": "0"}
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    status = 206 if spans is not None else 200
    if spans is not None and len(spans) > 1:
        parts, boundary, length = _multipart_ranges(spans, size, mime)
        mime = f"multipart/byteranges; boundary={boundary}"
    elif spans is not None:
        start, end = spans[0]
        parts, length = spans, end - start + 1
        headers['Content-Range'] = f"bytes {start}-{end}/{size}"
    else:
        parts, length = [(None, None)], size
    headers['Content-Length'] = str(length)
    return status, mime, parts, length, headers


def _raw_response(reader, path, range_header, method, download=None):
    status, mime, parts, length, headers = _raw_layout(reader, path, range_header)
    if method == 'HEAD' or status == 416:
        return Response(status_code=status, media_type=mime, headers=headers)
    if download is None:
        data = b''.join(part if isinstance(part, bytes) else reader.read(*part) for part in parts)
        return Response(content=data, status_code=status, media_type=mime, headers=headers)

    from starlette.responses import StreamingResponse

    download.reserve_body(length)
    for part in parts:
        if isinstance(part, bytes):
            download.write(part)
        else:
            with closing(reader.chunks(*part)) as blocks:
                for block in blocks:
                    download.write(block)
    if download.tell() != length:
        raise FileReadConflict('Staged response length differs from the pinned file')
    download.seek(0)
    return StreamingResponse(download.chunks(), status_code=status, media_type=mime, headers=headers)


def _verified_publication_response(reader_context, path, range_header, file_version, download, *, version_message):
    from starlette.responses import StreamingResponse

    with reader_context as reader:
        status, mime, parts, length, headers = _raw_layout(reader,path,range_header)
        changed = file_version is not None and file_version != reader.version
        body = download if status != 416 and not changed else None
        with closing(reader.chunks()) as chunks:
            if body is None:
                for _ in chunks:
                    pass
            else:
                _stage_verified_ranges(body,chunks,parts,reader.size,length)
    if changed:
        raise HTTPException(409,{'code':'file_changed_during_read','message':version_message})
    if body is None:
        return Response(status_code=status,media_type=mime,headers=headers)
    return StreamingResponse(body.chunks(),status_code=status,media_type=mime,headers=headers)


def _stage_verified_ranges(download,chunks,parts,size,length):
    """Retain only requested bytes, preserving multipart request order."""
    download.reserve_body(length)
    spans, counted, offset = [], 0, 0
    for part in parts:
        if isinstance(part, bytes):
            download.seek(offset)
            counted += download.write(part)
            offset += len(part)
        else:
            start, end = part
            start, stop = (0,size) if start is None else (start,end+1)
            spans.append((start,stop,offset))
            offset += stop-start
    position = 0
    for block in chunks:
        following = position+len(block)
        for start, stop, target in spans:
            left, right = max(start,position), min(stop,following)
            if left < right:
                download.seek(target+left-start)
                counted += download.write(block[left-position:right-position])
        position = following
    if counted != length:
        raise FileReadConflict('Archived download length differs from the verified response')
    download.seek(0)


# ---------- caseDict ----------
@router.get("/caseDict")
async def read_dict(case_id: int, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db, write=False)
    try:
        async with case_read_capacity.file_read_deadline('editor_read', 'caseDict 读取'):
            reader, revision = await _editor_reader(db, c, _DICT_PATH, 'editor_read', 'caseDict 读取')
            return await bounded_case_read(_editor_dictionary, reader, revision, code='editor_read', label='caseDict 读取')
    except FileNotFoundError as error:
        raise HTTPException(404, 'caseDict 不存在') from error
    except FileReadConflict as error:
        raise HTTPException(409, {'code':'file_changed_during_read','message':'caseDict 在读取期间发生变化，请重新读取'}) from error


def _editor_dictionary(reader, revision):
    text, digest = reader.text_snapshot()
    try:
        parsed, status = parse_casedict(text), 'ok'
    except ValueError as error:
        parsed, status = None, f'invalid: {error}'
    result={'content': text, 'workflow_state': get_workflow_state(text), 'parsed': parsed,
            'parse_status': status, 'dict_revision': revision, 'content_sha256': digest}
    if parsed is not None:
        from ..services.case_execution_settings import selected_interactive,selected_workflow
        for key,resolve in (('workflow_defaults',selected_workflow),('interactive_defaults',selected_interactive)):
            try:
                defaults=resolve(parsed)
                if defaults:
                    result[key]=defaults
            except ValueError as error:
                result[key+'_error']=str(error)
    return result


class DictConditionsIn(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    expected_sha256: str | None = None


async def _edit_dictionary(db, case, user, edit, *, revision, expected_sha256,
                           invalid_prefix='', conflict_message='caseDict 已变化，请重新读取',
                           workflow_status=409, **options):
    from ..services import case_dict_edits

    checked = partial(_locked_recovery_case, case_id=case.id, user_id=user.id, write=True, prefix=case.s3_prefix)
    try:
        return await case_dict_edits.save(db, case, checked, edit, revision=revision,
                                          expected_sha256=expected_sha256, actor_id=user.id, **options)
    except FileNotFoundError as error:
        raise HTTPException(404, 'caseDict 不存在') from error
    except FileReadConflict as error:
        raise HTTPException(409, {'code':'file_conflict', 'message':conflict_message}) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except WorkflowGateError as error:
        raise HTTPException(workflow_status, (invalid_prefix if workflow_status == 422 else '')+str(error)) from error
    except ValueError as error:
        raise HTTPException(422, invalid_prefix+str(error)) from error


class DictWriteIn(DictConditionsIn):
    content: str
    workflow_state: str | None = None  # 兼容旧客户端；变更阶段只能通过 :advance


@router.put("/caseDict")
async def write_dict(case_id: int, body: DictWriteIn,
                     user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db)

    def edit(_text, _prefix):
        content = body.content
        if body.workflow_state is not None:
            content = set_workflow_state(content, body.workflow_state)
        return content, {}

    return await _edit_dictionary(db, c, user, edit, revision=body.expected_revision,
        expected_sha256=body.expected_sha256, allow_missing=True, invalid_prefix='caseDict 语法错误：')


class CaseMetaEditIn(DictConditionsIn):
    changes: dict = Field(default_factory=dict)
    time_behavior: str | None = None


def _case_meta_candidate(content, body):
    behavior = body.time_behavior if "time_behavior" in body.model_fields_set else case_meta.UNSET_TIME_BEHAVIOR
    return case_meta.prepare(content, body.changes, behavior)


@router.get("/caseDict/meta-schema")
async def read_case_meta_schema(case_id: int, user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db, write=False)
    return case_meta_options()


@router.post("/caseDict/meta:lint")
async def lint_case_meta(case_id: int, body: CaseMetaEditIn, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    case = await _case_for_file_request(case_id, user, db, write=False)
    try:
        async with case_read_capacity.file_read_deadline("editor_read", "caseDict 读取"):
            reader, revision = await _editor_reader(db, case, _DICT_PATH, "editor_read", "caseDict 读取")
            content, digest = await bounded_case_read(reader.text_snapshot, code="editor_read", label="caseDict 读取")
            if body.expected_revision is not None and body.expected_revision != revision:
                raise CaseWriteConflict("revision_conflict", "caseDict 修订已变化，请重新读取", current_revision=revision)
            if body.expected_sha256 is not None and body.expected_sha256 != digest:
                raise CaseWriteConflict("file_conflict", "caseDict 文件已变化，请重新读取", path=_DICT_PATH)
            issues = case_meta.lint(_case_meta_candidate(content, body))
    except FileNotFoundError as error:
        raise HTTPException(404, "caseDict 不存在") from error
    except FileReadConflict as error:
        raise HTTPException(409, {"code": "file_conflict", "message": "caseDict 已变化，请重新读取"}) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {"valid": all(issue["severity"] != "error" for issue in issues), "issues": issues}


@router.patch("/caseDict/meta")
async def edit_case_meta(case_id: int, body: CaseMetaEditIn, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    case = await _case_for_file_request(case_id, user, db)

    def edit(text, _prefix):
        return _case_meta_candidate(text, body), {}

    return await _edit_dictionary(db, case, user, edit, revision=body.expected_revision,
        expected_sha256=body.expected_sha256, event_source='case-meta', allow_noop=True)


class ApplyMaterialIn(DictConditionsIn):
    expected_revision: int = Field(ge=0, strict=True)
    key: str = Field(pattern=material_library.KEY)
    library_revision: int = Field(ge=1, strict=True)
    name: str = Field(pattern=material_library.KEY)


@router.post('/caseDict/materials:apply')
async def apply_material(case_id: int, body: ApplyMaterialIn, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    case = await _case_for_file_request(case_id, user, db)
    workspace_id = case.workspace_id
    head = await material_library.get(db, workspace_id, body.key)
    if head is None:
        raise HTTPException(404, '当前工作区材料库条目不存在')
    if head.material_json is None:
        raise HTTPException(409, {'code': 'material_archived', 'message': '材料已归档，请从材料库恢复后再引用'})
    row = head if head.revision == body.library_revision else await material_library.get(
        db, workspace_id, body.key, body.library_revision)
    if row is None or row.material_json is None:
        raise HTTPException(404, '材料库版本不存在')
    snapshot = material_library.serialize(row, full=True)

    def edit(text, _prefix):
        return material_library.apply(text, body.name, snapshot, workspace_id), {'material': snapshot}

    return await _edit_dictionary(db, case, user, edit, revision=body.expected_revision,
        expected_sha256=body.expected_sha256, event_source='materials-library', allow_noop=True)


class AdvanceIn(BaseModel):
    state: str
    confirmed: bool = False
    expected_revision: int = Field(ge=0, strict=True)
    workflow_step_id: str | None = None
    expected_step_version: int | None = None


@router.post("/caseDict:advance")
async def advance_workflow(case_id: int, body: AdvanceIn, request: Request, response: Response,
                           user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    from ..services.engineering_stage import advance_staged, StageAdvanceError
    from ..services.workflow_state import ExecutionConflict

    workflow_id = None
    try:
        c = await _case_for_file_request(case_id, user, db)
        checked = partial(_locked_recovery_case, case_id=c.id, user_id=user.id, write=True, prefix=c.s3_prefix)
        if body.workflow_step_id is not None:
            from ..models import WorkflowRun, WorkflowStep
            from ..services.workflow_scheduler import confirm_staged_step
            step = await db.get(WorkflowStep, body.workflow_step_id)
            run = await db.get(WorkflowRun, step.run_id) if step else None
            if run is None or run.case_id != c.id:
                raise HTTPException(404, "工作流确认步骤不存在")
            if body.expected_step_version is None:
                raise HTTPException(422, "工作流确认需要 expected_step_version")
            workflow_id, plan_json = run.id, run.plan_json
            result = await confirm_staged_step(db, c, checked, run=run, step=step,
                user_id=user.id, target=body.state, confirmed=body.confirmed,
                expected_revision=body.expected_revision, expected_version=body.expected_step_version)
        else:
            if body.expected_step_version is not None:
                raise HTTPException(422, "expected_step_version 需要 workflow_step_id")
            result = await advance_staged(db, c, checked, target=body.state, confirmed=body.confirmed,
                                          expected_revision=body.expected_revision, actor_id=user.id)
    except StageAdvanceError as error:
        raise HTTPException(error.status_code, error.detail) from error
    except ExecutionConflict as error:
        raise HTTPException(409, {"code":error.code, "message":error.detail}) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except HTTPException as error:
        if (error.status_code==409 and isinstance(error.detail,dict) and
                error.detail.get('code')=='file_cleanup_pending'):
            raise HTTPException(409, {'code':'confirmation_cleanup_pending','committed':True,'case_id':case_id,
                'message':'阶段确认已提交，后续清理仍在等待；请读取当前状态，不要重复确认'}) from error
        raise
    if workflow_id is not None:
        import json
        from ..services.step_handlers import CONTRACT
        plan = json.loads(plan_json)
        if plan.get('execution_contract') == CONTRACT:
            preferences = {value.strip().lower() for value in request.headers.get('prefer','').split(',')}
            recovery = getattr(request.app.state,'workflow_dispatch_task',None)
            if ('respond-async' in preferences and body.state=='D' and storage._use_s3()
                    and plan.get('target') in {'postprocess','report'} and recovery is not None and not recovery.done()):
                response.headers['Preference-Applied']='respond-async'
                result['workflow_dispatch']={'status':'pending','run_id':workflow_id,'code':'continuation_queued'}
                return result
            from ..services.workflow_execution import dispatch
            try:
                await dispatch(db,workflow_id,user,request=request)
                result['workflow_dispatch']={'status':'dispatched','run_id':workflow_id}
            except (ExecutionConflict,ValueError,OSError,HTTPException) as error:
                # Confirmation is already durable. A subsequent admission error
                # must not pretend to roll it back; ready steps remain retryable.
                await db.rollback()
                detail = error.detail() if isinstance(error,CaseWriteConflict) else getattr(error,'detail',error)
                code = (detail.get('code') if isinstance(detail,dict) else None) or getattr(error,'code','dispatch_failed')
                result['workflow_dispatch']={'status':'pending','run_id':workflow_id,
                    'code':code,'detail':str(detail.get('message',detail) if isinstance(detail,dict) else detail)}
    return result


class ExtensionIn(DictConditionsIn):
    version: str
    enabled: bool
    settings: dict = Field(default_factory=dict)


def _extension_scalar(value):
    if isinstance(value, str):
        return LiteralDictString(value)
    if isinstance(value, (bool, int)) or isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("扩展设置只支持有限的数值、布尔值、字符串、子字典和标量列表")


def _extension_settings(settings):
    result = {}
    for key, value in settings.items():
        if isinstance(value, dict):
            prepared = _extension_settings(value)
        elif isinstance(value, list):
            prepared = [_extension_scalar(item) for item in value]
        else:
            prepared = _extension_scalar(value)
        result[LiteralDictString(key)] = prepared
    return result


def _extension_block(block):
    entries = block.get("entries")
    if not isinstance(entries, dict):
        return block
    prepared = {}
    for namespace, entry in entries.items():
        if isinstance(entry, dict):
            entry = dict(entry)
            if isinstance(entry.get("version"), str):
                entry["version"] = LiteralDictString(entry["version"])
            if isinstance(entry.get("settings"), dict):
                entry["settings"] = _extension_settings(entry["settings"])
        prepared[namespace] = entry
    return {**block, "entries": prepared}


async def _save_extension(db, c, user, namespace, expected_revision, expected_sha256, value):
    def edit(text, _prefix):
        extensions = parse_casedict(text).get('extensions', {})
        if not isinstance(extensions, dict) or not isinstance(extensions.get('entries', {}), dict):
            raise ValueError('extensions 和 entries 必须是字典')
        current_entries = extensions.get('entries', {})
        entries = dict.fromkeys(current_entries, UNCHANGED_DICT_ENTRY)
        if value is None:
            if namespace not in entries:
                raise HTTPException(404, '扩展条目不存在')
            del entries[namespace]
        else:
            previous = current_entries.get(namespace, {})
            if not isinstance(previous, dict):
                raise ValueError('扩展条目必须是字典')
            entries[namespace] = {**dict.fromkeys(previous, UNCHANGED_DICT_ENTRY), **value}
        return set_block(text, 'extensions', {'schemaVersion': 1, 'entries': entries}), {}

    return await _edit_dictionary(db, c, user, edit, revision=expected_revision,
        expected_sha256=expected_sha256, event_source='extensions', workflow_status=422)


@router.put("/caseDict/extensions/{namespace}")
async def put_extension(case_id: int, namespace: str, body: ExtensionIn,
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db)
    try:
        entry = {"version": LiteralDictString(body.version), "enabled": body.enabled,
                 "settings": _extension_settings(body.settings)}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    receipt = await _save_extension(db, c, user, namespace, body.expected_revision, body.expected_sha256, entry)
    return {**receipt, "entry": entry}


@router.delete("/caseDict/extensions/{namespace}")
async def delete_extension(case_id: int, namespace: str,
                           expected_revision: int | None = Query(None, ge=0),
                           expected_sha256: str | None = Query(None),
                           user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db)
    return await _save_extension(db, c, user, namespace, expected_revision, expected_sha256, None)


class ErrorUpdateIn(DictConditionsIn):
    title: str | None = None
    stage: str | None = None
    category: str | None = None
    severity: str | None = None
    status: str | None = None
    fixOptions: dict | None = None
    resolution: str | None = None
    verification: str | None = None
    blocking: bool | None = None
    message: str | None = None
    relatedPaths: list[str] | None = None


class ErrorCreateIn(ErrorUpdateIn):
    id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')
    title: str
    stage: str
    category: str
    severity: str
    status: str = "open"
    blocking: bool = False


def _error_evidence_paths(prefix, selected, previous):
    retained = [previous] if isinstance(previous, str) else (previous or [])
    paths = list(dict.fromkeys(normalize_path(path) for path in selected))
    for path in paths:
        if path not in retained:
            try:
                FileRead(prefix, path)
            except FileNotFoundError as error:
                raise ValueError(f"相关证据文件不存在：{path}") from error
    return paths


async def _save_error_entry(db, c, user, error_id, body, *, create=False):
    fields = body.model_dump(exclude_unset=not create, exclude_none=True,
                             exclude={"id", "expected_revision", "expected_sha256"})
    if not fields:
        raise HTTPException(422, "请提供要更新的错误字段")

    def edit(text, prefix):
        parsed = parse_casedict(text)
        errors = parsed.get('errors', {})
        if not isinstance(errors, dict):
            raise ValueError('errors 必须是字典')
        entries = errors.get('entries', {})
        if not isinstance(entries, dict):
            raise ValueError('errors.entries 必须是字典')
        if create and error_id in entries:
            raise HTTPException(409, {'code':'error_exists', 'message':'该错误编号已存在'})
        if not create and error_id not in entries:
            raise HTTPException(404, '错误条目不存在')
        legacy_counts = error_counts(parsed) if not entries else None
        entry = entries.get(error_id, {})
        if not isinstance(entry, dict):
            raise ValueError('错误条目必须是字典')
        updates = dict(fields)
        if 'relatedPaths' in updates:
            updates['relatedPaths'] = _error_evidence_paths(prefix, updates['relatedPaths'], entry.get('relatedPaths'))
        entries[error_id] = {**entry, **updates}
        updated_errors = {**errors, 'entries': entries}
        summary = {**error_counts({'errors': updated_errors}), 'lastErrorId': error_id}
        if legacy_counts and any(summary[key] < legacy_counts[key] for key in ('open', 'blocking')):
            raise HTTPException(409, {'code':'error_summary_reconciliation_required',
                'message':'旧算例还有未拆分的错误计数。请先在 caseDict 错误块核对旧记录，再添加条目。'})
        updated_errors['summary'] = summary
        return set_block(text, 'errors', updated_errors), {'error_id':error_id, 'summary':summary}

    return await _edit_dictionary(db, c, user, edit, revision=body.expected_revision,
        expected_sha256=body.expected_sha256, event_source='error-lifecycle', workflow_status=422,
        invalid_prefix='错误生命周期状态不合法（未写入）：',
        conflict_message='读取期间证据或 caseDict 已变化，请重新读取')


@router.post("/caseDict/errors")
async def create_error_entry(case_id: int, body: ErrorCreateIn,
                             user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db)
    return await _save_error_entry(db, c, user, body.id, body, create=True)


@router.patch("/caseDict/errors/{error_id}")
async def update_error_entry(case_id: int, error_id: str, body: ErrorUpdateIn,
                             user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db)
    return await _save_error_entry(db, c, user, error_id, body)


# ---------- caseDict 块级编辑（表单编辑器用：只换目标块内容，保注释/保其它块） ----------
class BlockIn(DictConditionsIn):
    block: str
    entries: dict   # {key: "值" | ["列","表"] | {子块}}


@router.put("/caseDict/block")
async def write_dict_block(case_id: int, body: BlockIn,
                           user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    c = await _case_for_file_request(case_id, user, db)
    block = body.block.strip()
    if not block:
        raise HTTPException(400, "块名不能为空")
    def edit(text, _prefix):
        entries = body.entries
        if block == 'extensions':
            entries = _extension_block(entries)
        elif block == 'provenance' and text:
            from ..services.provenance import managed_block_entries
            entries = managed_block_entries(text, entries)
        return set_block(text, block, entries), {}

    return await _edit_dictionary(db, c, user, edit, revision=body.expected_revision,
        expected_sha256=body.expected_sha256, allow_missing=True, invalid_prefix='替换后语法错误（未写入）：')


class UnitConvertIn(BaseModel):
    dimension: str
    value: float
    source: str
    target: str


@router.post("/caseDict/units:convert")
async def convert_case_units(case_id: int, body: UnitConvertIn,
                             user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db)
    from ..services.units import convert_unit
    try:
        return {"dimension": body.dimension, "source": body.source, "target": body.target,
                "value": convert_unit(body.dimension, body.value, body.source, body.target)}
    except ValueError as error:
        raise HTTPException(422, str(error))


@router.post("/caseDict/validate")
async def validate_dict(case_id: int, body: DictWriteIn,
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id, user, db)
    try:
        parsed = parse_casedict(body.content)
        return {"ok": True, "parsed": parsed}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
