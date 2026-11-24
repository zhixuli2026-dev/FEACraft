"""Durable, caller-scoped upload creation and resolution of lost responses."""
import asyncio
import hashlib
import json
import uuid

from .. import storage
from ..models import DurableEvent
from . import owner_storage_budget, upload_storage, uploads
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, coordinate
from .durable_events import append_event, json_text, lock_case_execution
from .job_execution import validate_key
from .storage_admission import lock_owner


PREPARED = 'files.upload_creation_prepared'
READY = 'files.upload_creation_ready'
WITHDRAWN = 'files.upload_creation_withdrawn'
NAMESPACE = uuid.UUID('90394a3f-9583-4ff2-9969-0cbe98ccf5f8')


def identity(case, user, key):
    validate_key(key)
    return uuid.uuid5(NAMESPACE, json_text([case.id, user.id, key]))


def ready_id(identifier):
    return str(uuid.uuid5(identifier, READY))


async def event(db, case, identifier):
    row = await db.get(DurableEvent, str(identifier), populate_existing=True)
    if row is not None and (row.case_id != case.id or row.type not in {PREPARED, WITHDRAWN}):
        raise uploads.UploadError('上传创建记录与当前算例不一致', 409)
    return row


async def io(function, *args):
    return await finish_before_cancel(asyncio.to_thread(function, *args))


def remote_meta(prefix, initial):
    from . import attempt_objects, s3_uploads
    return {**initial, 'schema':s3_uploads.SCHEMA, 'storage':attempt_objects.binding(prefix),
            'offset':0, 'chunk_count':0, 'status':'uploading', 'keyed_creation':True}


def materialize(prefix, initial):
    """Materialize a committed, still-unready intent under case coordination."""
    identifier = initial['id']
    if storage._use_s3():
        from . import s3_uploads
        from .s3_file_transactions import _read
        from .s3_case_files import require_conditions
        require_conditions(prefix)
        meta = remote_meta(prefix, initial)
        data, _, _ = _read(prefix, s3_uploads.directory(identifier)+'/meta.json', uploads.MAX_METADATA_BYTES)
        if data is None:
            try:
                s3_uploads._save_meta(prefix, meta, None)
                return s3_uploads._status(meta)
            except CaseWriteConflict:
                pass  # A delayed copy of the same conditional write may have won.
        actual, _ = s3_uploads._meta(prefix, identifier)
        mutable = {'offset', 'chunk_count', 'updated_at'}
        if any(actual.get(k) != v for k,v in meta.items() if k not in mutable):
            raise uploads.UploadError('远端上传与创建记录不一致', 409)
        return s3_uploads._status(actual)

    from .file_transactions import _path
    from .node_storage_budget import reserve
    directory = uploads._session_dir(prefix, identifier)
    encoded = uploads._encoded_meta(initial)
    directory.mkdir(parents=True, exist_ok=True)
    names = {p.name for p in directory.iterdir()}
    if names - {'data.part','meta.json','.meta.tmp'}:
        raise uploads.UploadError('上传准备目录包含未知文件', 409)
    part = _path(directory, 'data.part')
    if 'meta.json' in names:
        _, actual = uploads._read_meta(prefix, identifier)
        if any(actual.get(k) != v for k,v in initial.items() if k != 'updated_at'):
            raise uploads.UploadError('上传暂存与创建记录不一致', 409)
        if not part.is_file() or part.stat().st_size > initial['size']:
            raise uploads.UploadError('上传暂存大小与创建记录不一致', 409)
        return uploads._status(actual, part.stat().st_size)
    # A process can stop between mkdir, creating the empty part and replacing
    # metadata. Resume only this exact, bounded, empty initial layout.
    if part.exists() and (not part.is_file() or part.stat().st_size):
        raise uploads.UploadError('未完成初始化的上传包含未核验内容', 409)
    temporary = _path(directory, '.meta.tmp')
    if temporary.exists():
        with temporary.open('rb') as stream:
            if not encoded.startswith(stream.read(len(encoded)+1)):
                raise uploads.UploadError('临时元数据与创建记录不一致', 409)
    with reserve(len(encoded)):
        if not part.exists():
            part.touch(exist_ok=False)
        uploads._write_meta(directory, initial)
    return uploads._status(initial, 0)


def require_declaration(row, digest):
    if row.type == WITHDRAWN:
        raise uploads.UploadError('此创建请求已取消，请重新选择文件', 409)
    payload = json.loads(row.payload_json)
    if payload['request_sha256'] != digest:
        raise CaseWriteConflict('idempotency_conflict', '同一上传创建标识已用于另一份请求')
    return payload


async def create(db, case, user, key, declaration, conditions):
    from .case_read_capacity import admitted
    from .quota import check_storage_quota
    identifier = identity(case, user, key)
    digest = hashlib.sha256(json_text([declaration,conditions]).encode()).hexdigest()
    async with admitted(db, case) as owner:
        await lock_owner(db, owner.id, wait=True)
        pending = await event(db, case, identifier)
        if pending is None:
            path, size = declaration['path'], declaration['size']
            capacity = owner_storage_budget.current()
            if capacity is not None:
                capacity.reserve_future(size)
            else:
                await check_storage_quota(db, owner, size + uploads.creation_metadata_peak(path, size))
            async with coordinate(db, case):
                pending = await event(db, case, identifier)
                if pending is None:
                    initial = await io(uploads.initial_metadata, case.s3_prefix, path, size,
                                       declaration['sha256'], declaration['overwrite'], identifier.hex, conditions)
                    await upload_storage.register(db, case, owner, uploads._status(initial, 0))
                    await append_event(db, case.id, PREPARED, {'request_sha256':digest, 'initial':initial},
                                       event_id=str(identifier))
            # Preserve the upload ID and reserved payload before any staging
            # I/O. Keep this owner's remote write grant across the SQL commit.
            await finish_before_cancel(db.commit())
            await lock_owner(db, owner.id, wait=True)
        async with coordinate(db, case):
            pending = await event(db, case, identifier)
            initial = require_declaration(pending, digest)['initial']
            row = await upload_storage.row_for(db, case, owner, identifier.hex)
            if row is None or (row.path, row.size) != (initial['path'], initial['size']):
                raise uploads.UploadError('上传额度与创建记录不一致', 409)
            if row.state == 'cancelled':
                raise uploads.UploadError('此创建请求已取消，请重新选择文件', 409)
            completed = await uploads.completion_record(db, case, identifier.hex)
            if completed is not None:
                result = completed['result']
            elif row.state != 'active':
                raise uploads.UploadError('上传结算记录缺少完成凭据',409)
            elif await db.get(DurableEvent, ready_id(identifier)) is not None:
                result = await io(uploads.session_status, case.s3_prefix, identifier.hex)
            else:
                if not storage._use_s3() and not (uploads._session_dir(case.s3_prefix,identifier.hex)/'meta.json').exists():
                    await check_storage_quota(db, owner, len(uploads._encoded_meta(initial)))
                result = await io(materialize, case.s3_prefix, initial)
                await append_event(db, case.id, READY, {'id':identifier.hex}, event_id=ready_id(identifier))
        await finish_before_cancel(db.commit())
    return result


async def cancel(db, case, user, key):
    """Fence a request not yet admitted, or run the normal cancellation by its SQL identity."""
    from .case_read_capacity import admitted
    identifier = identity(case, user, key)
    if storage._use_s3():
        from .manual_file_recovery import authorized

        await lock_case_execution(db, case.id)
        case = await authorized(db, case.id, user.id, write=True)
        pending = await withdraw(db, case, identifier)
    else:
        async with admitted(db, case), coordinate(db, case):
            pending = await withdraw(db, case, identifier)
    await finish_before_cancel(db.commit())
    if pending:
        return await uploads.cancel_with_ledger(db, case, identifier.hex, user_id=user.id)
    return {'ok':True, 'cancelled':True}


async def withdraw(db, case, identifier):
    row = await event(db, case, identifier)
    if row is None:
        await append_event(db, case.id, WITHDRAWN, {'id':identifier.hex}, event_id=str(identifier))
    return row is not None and row.type == PREPARED


async def pending_cancel_initial(db, case, upload_id):
    identifier = uuid.UUID(hex=upload_id)
    if identifier.version != 5:
        return
    row = await event(db, case, identifier)
    if row is not None and row.type == PREPARED and await db.get(DurableEvent, ready_id(identifier)) is None:
        return json.loads(row.payload_json)['initial']
    return None


async def prepare_cancel(db, case, upload_id):
    initial = await pending_cancel_initial(db, case, upload_id)
    # The case lock prevents any new local staging directory from appearing.
    if initial is not None and uploads._session_dir(case.s3_prefix, upload_id).exists():
        await io(materialize, case.s3_prefix, initial)


async def lookup(db,case,user,key):
    from .case_read_capacity import reading
    identifier = identity(case,user,key)
    async with reading(db,case):
        pending = await event(db,case,identifier)
        if pending is None:
            return {'upload':None}
        if pending.type == WITHDRAWN:
            return {'cancelled':True,'upload':None}
        owner = await owner_storage_budget.owner_for_case(db,case)
        row = await upload_storage.row_for(db,case,owner,identifier.hex)
        if row is None:
            raise uploads.UploadError('上传创建记录缺少额度归属',409)
        if row.state == 'cancelled':
            return {'cancelled':True,'upload':None}
        completed = await uploads.completion_record(db,case,identifier.hex)
        if completed is not None:
            return {'upload':completed['result']}
        if await db.get(DurableEvent,ready_id(identifier)) is None:
            return {'upload':None}
        return {'upload':await io(uploads.session_status,case.s3_prefix,identifier.hex)}
