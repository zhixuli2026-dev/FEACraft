"""Remote upload sessions and immutable chunks; SQL authorizes completion."""
import logging
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import re
import time
import uuid

from botocore.exceptions import BotoCoreError, ClientError

from .. import storage
from ..config import get_settings
from . import attempt_objects as objects, uploads
from .attempt_workspace import canonical_path, assert_mutable_path
from .case_write import CaseWriteConflict, is_case_dictionary
from .durable_events import json_text
from .file_transactions import FileSource, _capture, _path
from .fs_directory import remove_owned
from .s3_case_files import require_conditions
from .s3_file_transactions import _read, _conditional
from .s3_streams import snapshot, snapshot_head
from .request_deadline import current

MAX_CHUNKS = 10000
MAX_PLAN = 8 * 1024 * 1024
CANCEL_PAGE_SIZE = 32
SCHEMA = 'vibeflow.s3-upload.v1'
ASSEMBLY_OWNER_SCHEMA = 'vibeflow.s3-upload-assembly-owner.v1'


def directory(upload_id):
    if not re.fullmatch('[0-9a-f]{32}', upload_id):
        raise uploads.UploadError('上传会话不存在', 404)
    return '.vibeflow/uploads/' + upload_id


def assembly_root(prefix, upload_id):
    return _path(storage._fs_path(prefix, '.'), directory(upload_id) + '/assembly')


def recover_assembly(prefix, upload_id):
    from .owned_staging import recover_abandoned

    try:
        root = assembly_root(prefix, upload_id)
    except FileNotFoundError:
        return  # This S3 case may never have created a local assembly cache.
    recover_abandoned(root, ASSEMBLY_OWNER_SCHEMA)


def _meta(prefix, upload_id):
    data, etag, _ = _read(prefix, directory(upload_id)+'/meta.json', 65536)
    if not data:
        raise uploads.UploadError('上传会话不存在', 404)
    return _decode_meta(prefix, upload_id, data), etag


def _decode_meta(prefix, upload_id, data):
    meta = json.loads(data)
    if (meta.get('schema') != SCHEMA or meta.get('storage') != objects.binding(prefix)
            or meta.get('id') != upload_id or canonical_path(meta['path']) != meta['path']
            or type(meta.get('size')) is not int or not 1 <= meta['size'] <= uploads.MAX_UPLOAD_BYTES
            or type(meta.get('offset')) is not int or not 0 <= meta['offset'] <= meta['size']
            or type(meta.get('chunk_count')) is not int or not 0 <= meta['chunk_count'] <= MAX_CHUNKS
            or meta.get('status') not in {'uploading', 'cancelled'}):
        raise uploads.UploadError('远端上传元数据不匹配，请保留现场检查', 409)
    assert_mutable_path(prefix, meta['path'])
    if is_case_dictionary(prefix, meta['path']):
        raise uploads.UploadError('caseDict 请走专用端点', 400)
    return meta


def _save_meta(prefix, meta, etag):
    _conditional(prefix, directory(meta['id'])+'/meta.json', json_text(meta).encode(), etag,
                 {'ContentType': 'application/json'})


def _status(meta):
    return {**uploads._status(meta, meta['offset']), 'status': meta['status'],
            'max_chunks': MAX_CHUNKS}


def create_session(prefix, path, size, sha256, overwrite):
    assert_mutable_path(prefix, path)
    path = canonical_path(path)
    if is_case_dictionary(prefix, path):
        raise uploads.UploadError('caseDict 请走专用端点', 400)
    if type(size) is not int or not 1 <= size <= uploads.MAX_UPLOAD_BYTES:
        raise uploads.UploadError('分块上传 size 必须为 1 字节至 1 GiB')
    if sha256 is not None and not re.fullmatch('[0-9a-fA-F]{64}', sha256):
        raise uploads.UploadError('sha256 必须为 64 位十六进制')
    require_conditions(prefix)
    before, _, _ = snapshot(prefix, path)
    if before is not None and not overwrite:
        raise uploads.UploadError('目标文件已存在；显式 overwrite=true 才允许替换', 409)
    identifier, now = uuid.uuid4().hex, time.time()
    meta = {'schema': SCHEMA, 'storage': objects.binding(prefix), 'id': identifier,
            'path': path, 'size': size, 'sha256': sha256.lower() if sha256 else None,
            'target_sha256': before['sha256'] if before else None, 'overwrite': overwrite,
            'created_at': now, 'updated_at': now, 'offset': 0, 'chunk_count': 0, 'status': 'uploading'}
    _save_meta(prefix, meta, None)
    return _status(meta)


def session_status(prefix, upload_id):
    meta, _ = _meta(prefix, upload_id)
    if meta['status'] == 'cancelled':
        raise uploads.UploadError('上传已取消', 404)
    return _status(meta)


def write_chunk(prefix, upload_id, offset, data):
    if not data or len(data) > uploads.MAX_CHUNK_BYTES:
        raise uploads.UploadError('chunk 必须为 1 字节至 8 MiB', 413 if data else 400)
    meta, etag = _meta(prefix, upload_id)
    if meta['status'] != 'uploading':
        raise uploads.UploadError('上传已经取消', 409)
    if offset != meta['offset']:
        raise uploads.UploadError(f"offset 不连续：服务端当前为 {meta['offset']}", 409)
    if offset + len(data) > meta['size'] or meta['chunk_count'] >= MAX_CHUNKS:
        raise uploads.UploadError('chunk 超出声明 size 或远端分块数量上限', 413)
    base = directory(upload_id)+f'/chunks/{offset:010d}'
    record = {'offset': offset, **objects.digest(data)}
    # If the previous response was lost before metadata advanced, an identical
    # retry verifies and reuses these immutable objects; different bytes fail.
    try:
        _put_chunk_or_reuse(prefix,base+'/data',data)
        _put_chunk_or_reuse(prefix,base+'/record.json',json_text(record).encode())
    except ValueError as error:
        raise uploads.UploadError('已存在的远端分块与本次重试不一致，请保留会话检查', 409) from error
    meta.update(offset=offset+len(data), chunk_count=meta['chunk_count']+1, updated_at=time.time())
    _save_meta(prefix, meta, etag)
    return _status(meta)


def _put_chunk_or_reuse(prefix,path,data):
    """A verified immutable retry consumes no second payload allocation."""
    observed,_,_=snapshot(prefix,path,limit=len(data))
    if observed is not None:
        if observed!=objects.digest(data):raise ValueError('Immutable upload chunk differs')
        return
    objects.put_new(prefix,path,data)


async def write_chunk_reserved(checked, upload_id, offset, data):
    """Keep a durable case lease across a bounded chunk transfer."""
    from ..db import SessionLocal
    from ..models import Case
    from . import case_copy_io, case_read_capacity, storage_operations, upload_storage
    from .case_provision import io
    from .owner_storage_budget import hold as owner_capacity, owner_for_case, recover_owner
    from .request_deadline import STAGING_CLEANUP_SECONDS
    from .s3_storage_recovery import recover_reserved
    from .storage_admission import lock_owner
    from .upload_recovery import guard
    from .upload_recovery_source import guard_chunk

    directory(upload_id)

    async def same_owner(db):
        case = await checked(db)
        if (await owner_for_case(db, case)).id != owner_id:
            raise CaseWriteConflict('case_source_changed', '项目存储账户已变化，请刷新后重试')
        return case

    async with case_read_capacity.file_read_deadline('upload_chunk', '写入上传分块', seconds=STAGING_CLEANUP_SECONDS):
        with case_copy_io.admit(code='upload_chunk_busy', label='写入上传分块'):
            async with SessionLocal() as db:
                case = await checked(db)
                owner_id = (await owner_for_case(db, case)).id
            await recover_owner(owner_id, wait=True, require_available=True)
            async with storage_operations.hold(same_owner, purpose='staging') as lease:
                await recover_reserved(lease)
                async with lease.control() as db:
                    if await uploads.completion_record(db, lease.case, upload_id) is not None:
                        raise uploads.UploadError('上传已经提交，不能继续写入分块', 409)
                    selected = await guard(db, lease.case, upload_id)
                prefix = lease.case.s3_prefix
                if selected is not None:
                    await io(guard_chunk, prefix, upload_id, offset, data, selected)
                request = {'case_id':lease.case.id, 'upload_id':upload_id, 'offset':offset,
                           'size':len(data), 'sha256':hashlib.sha256(data).hexdigest()}
                async with SessionLocal() as owner_db:
                    async with owner_capacity(owner_db, owner_id, chunk=request):
                        await owner_db.commit()
                        result = await io(write_chunk, prefix, upload_id, offset, data)
                        async with lease.control() as db:
                            case = await db.get(Case, lease.case.id)
                            owner = await owner_for_case(db, case)
                            await lock_owner(db, owner_id)
                            row = await upload_storage.row_for(db, case, owner, upload_id)
                            if row is None or row.state != 'active':
                                raise uploads.UploadError('上传额度已结算，不能继续写入', 409)
                            await upload_storage.progress(db, row, result)
                return result


def _records(prefix, meta):
    deadline = current()
    offset = 0
    for _ in range(meta['chunk_count']):
        if deadline is not None:
            deadline.check()
        base = directory(meta['id'])+f'/chunks/{offset:010d}'
        encoded = objects.read(prefix, base+'/record.json', limit=1024)
        record = json.loads(encoded)
        if (record.get('offset') != offset or type(record.get('size')) is not int
                or not 1 <= record['size'] <= uploads.MAX_CHUNK_BYTES
                or offset+record['size'] > meta['size']
                or not re.fullmatch('[0-9a-f]{64}', record.get('sha256', ''))):
            raise uploads.UploadError('远端分块记录损坏', 409)
        yield base, record, objects.digest(encoded)
        offset += record['size']
    if offset != meta['offset']:
        raise uploads.UploadError('远端分块记录与进度不一致', 409)


def prepare_completion(prefix, upload_id, cache):
    meta, _ = _meta(prefix, upload_id)
    if meta['status'] != 'uploading' or meta['offset'] != meta['size']:
        raise uploads.UploadError('文件尚未收齐或上传已取消', 409)
    before, _, _ = snapshot(prefix, meta['path'])
    if (before['sha256'] if before else None) != meta['target_sha256']:
        raise CaseWriteConflict('file_conflict', '上传期间目标文件已变化，请重新创建上传会话', path=meta['path'])
    if before is not None and not meta['overwrite']:
        raise uploads.UploadError('目标文件已存在，拒绝覆盖', 409)
    from .node_storage_budget import reserve
    from .owned_staging import mark_owner

    reservation = ExitStack()
    reservation.enter_context(reserve(meta['size'] + 4096))
    cache['node_reservation'] = reservation
    case_root = storage._fs_path(prefix, '.')
    case_root.mkdir(parents=True, exist_ok=True)
    root = assembly_root(prefix, upload_id)
    temporary = root / uuid.uuid4().hex
    temporary.mkdir(parents=True, exist_ok=False)
    cache.update(directory=str(temporary), root=str(root))
    mark_owner(root, temporary, ASSEMBLY_OWNER_SCHEMA)
    relative = temporary.relative_to(case_root).as_posix()
    part, objects_plan = temporary/'data.part', []
    with part.open('xb') as stream:
        for base, record, record_hash in _records(prefix, meta):
            expected = {key: record[key] for key in ('size','sha256')}
            observed, _, _ = snapshot(prefix, base+'/data', output=stream, limit=expected['size'], rewind=False)
            if observed != expected:
                raise uploads.UploadError('远端分块内容与摘要不一致', 409)
            objects_plan.extend([{'path':base+'/data', **expected}, {'path':base+'/record.json', **record_hash}])
    deadline = current()
    captured = _capture(part, deadline=deadline)
    if captured['size'] != meta['size'] or meta['sha256'] and captured['sha256'] != meta['sha256']:
        raise uploads.UploadError('整文件 SHA-256 或长度不匹配', 409)
    # Reject an external metadata edit while deriving the assembled upload.
    latest, _ = _meta(prefix, upload_id)
    if latest != meta:
        raise uploads.UploadError('远端上传元数据在完成期间变化', 409)
    result = {**uploads._status(meta, meta['size']), 'sha256':captured['sha256'], 'status':'completed'}
    plan = {'schema':SCHEMA, 'storage':objects.binding(prefix), 'id':upload_id,
            'metadata':objects.digest(json_text(meta).encode()), 'objects':objects_plan}
    if meta.get('keyed_creation'):
        plan['keyed_creation'] = True
    encoded = json_text(plan).encode()
    if len(encoded)>MAX_PLAN:
        raise uploads.UploadError('远端上传清理清单过大', 413)
    if deadline is not None:
        deadline.check()
    path = directory(upload_id)+'/completions/'+uuid.uuid4().hex+'.json'
    objects.put_new(prefix, path, encoded)
    receipt = {'result':result, 'backend':'s3', 'cleanup_plan':path, 'cleanup_plan_digest':objects.digest(encoded)}
    return receipt, FileSource(relative+'/data.part', captured), before


def cleanup_cache(cache):
    from pathlib import Path

    try:
        if cache.get('directory'):
            remove_owned(Path(cache['directory']), root=Path(cache['root']))
    finally:
        reservation = cache.get('node_reservation')
        if reservation is not None:
            reservation.close()


@dataclass(frozen=True)
class CompletedCleanup:
    prefix: str
    upload_id: str
    plan_digest: str
    meta_etag: str | None
    keyed: bool
    closed: bool
    pending: tuple[tuple[str, str], ...]


def observe_completed(prefix, upload_id, receipt):
    """Verify the committed manifest and hash only its version-pinned objects."""
    if receipt.get('backend') != 's3':
        return None
    deadline = current()
    base = directory(upload_id)
    path = receipt['cleanup_plan']
    if not re.fullmatch(re.escape(base)+r'/completions/[0-9a-f]{32}\.json', path):
        raise uploads.UploadError('上传清理证明路径不匹配', 409)
    encoded = objects.read(prefix, path, limit=MAX_PLAN)
    objects.checked(encoded, receipt['cleanup_plan_digest'])
    plan = json.loads(encoded)
    if plan['schema'] != SCHEMA or plan['storage'] != objects.binding(prefix) or plan['id'] != upload_id:
        return None
    if not isinstance(plan['objects'],list) or len(plan['objects'])>2*MAX_CHUNKS:
        return None
    meta_bytes, meta_etag, _ = _read(prefix, base+'/meta.json', 65536)
    keyed = plan.get('keyed_creation') is True
    closed = meta_bytes == b'' and keyed
    if meta_bytes is not None and not closed and objects.digest(meta_bytes) != plan['metadata']:
        return None
    pending, names = [], set()
    for item in plan['objects']:
        if deadline is not None:
            deadline.check()
        path=item['path']
        if not re.fullmatch(re.escape(base)+r'/chunks/[0-9]{10}/(data|record\.json)',path) or path in names:
            return None
        names.add(path)
        observed, etag, _ = snapshot(prefix,path,limit=item['size'])
        if observed is not None:
            if observed['size']==0:
                continue  # Permanent closed key fences every late IfNoneMatch PUT.
            if observed != {key:item[key] for key in ('size','sha256')}:
                return None
            pending.append((path,etag))
    return CompletedCleanup(prefix, upload_id, receipt['cleanup_plan_digest'], meta_etag,
                            keyed, closed, tuple(pending))


def apply_completed(prefix, upload_id, receipt, observation):
    if observation is None or (observation.prefix, observation.upload_id) != (prefix, upload_id):
        return False
    if receipt.get('backend') != 's3' or observation.plan_digest != receipt['cleanup_plan_digest']:
        return False
    deadline = current()
    if deadline is not None:
        deadline.check()
    meta_path = directory(upload_id) + '/meta.json'
    head = snapshot_head(prefix, meta_path, 65536)
    if (head['ETag'] if head else None) != observation.meta_etag:
        return False
    for path, etag in observation.pending:
        if deadline is not None:
            deadline.check()
        _conditional(prefix, path, b'', etag, {'ContentType': 'application/vnd.vibeflow.closed-upload-chunk'})
    if deadline is not None:
        deadline.check()
    if observation.keyed and not observation.closed:
        _conditional(prefix, meta_path, b'', observation.meta_etag,
                     {'ContentType': 'application/vnd.vibeflow.closed-upload'})
    elif not observation.closed and observation.meta_etag is not None:
        _conditional(prefix, meta_path, None, observation.meta_etag, None)
    return True


def cleanup_completed(prefix, upload_id, receipt):
    return apply_completed(prefix, upload_id, receipt, observe_completed(prefix, upload_id, receipt))


async def _completion_quota(lease, owner_id, status):
    """Preserve the upload's preflight headroom check with an unlocked scan."""
    import asyncio
    from ..db import SessionLocal
    from ..models import Case
    from . import upload_storage
    from .owner_storage_budget import owner_for_case
    from .quota import check_locked_storage_quota, prepare_storage_inventory
    from .storage_admission import InventoryOutdated, lock_owner, quota_revision

    while True:
        async with SessionLocal() as db:
            revision = await quota_revision(db, owner_id)
        inventory = await prepare_storage_inventory(owner_id, revision)
        try:
            async with lease.control() as db:
                case = await db.get(Case, lease.case.id)
                owner = await owner_for_case(db, case)
                if owner.id != owner_id:
                    raise CaseWriteConflict('case_source_changed', '项目存储账户已变化，请刷新后重试')
                await lock_owner(db, owner.id, inventory_revision=inventory.revision)
                row = await upload_storage.row_for(db, case, owner, status['id'])
                if row is not None:
                    if row.state != 'active':
                        raise uploads.UploadError('上传额度已结算，不能继续写入', 409)
                    await upload_storage.progress(db, row, status)
                await check_locked_storage_quota(db, owner, 2 * status['size'], inventory=inventory)
            return
        except InventoryOutdated:
            await asyncio.sleep(.05)


async def complete(checked, upload_id):
    """Assemble and publish under the case reservation, with short SQL commits."""
    from ..models import Case
    from . import case_copy_io, case_read_capacity, storage_operations, upload_storage
    from .async_ops import finish_before_cancel
    from .case_provision import io
    from .durable_events import append_event
    from .owner_storage_budget import owner_for_case
    from .request_deadline import STAGING_CLEANUP_SECONDS
    from .s3_staging_transactions import write_reserved
    from .s3_storage_recovery import recover_reserved
    from .storage_admission import lock_owner
    from .upload_recovery import guard
    from .upload_recovery_source import guard_complete

    directory(upload_id)
    async with case_read_capacity.file_read_deadline('upload_complete', '完成上传', seconds=STAGING_CLEANUP_SECONDS):
        with case_copy_io.admit(code='upload_complete_busy', label='完成上传'):
            async with storage_operations.hold(checked, purpose='staging') as lease:
                await recover_reserved(lease)
                prefix = lease.case.s3_prefix
                await io(recover_assembly, prefix, upload_id)
                async with lease.control() as db:
                    case = await db.get(Case, lease.case.id)
                    receipt = await uploads.completion_record(db, case, upload_id)
                    if receipt is None:
                        owner = await owner_for_case(db, case)
                        owner_id = owner.id
                        row = await upload_storage.row_for(db, case, owner, upload_id)
                        if row is not None and row.state != 'active':
                            raise uploads.UploadError('上传额度已结算，不能继续写入', 409)
                        selected = await guard(db, case, upload_id)

                if receipt is None:
                    status = await io(session_status, prefix, upload_id)
                    if status['offset'] != status['size']:
                        raise uploads.UploadError('文件尚未收齐或上传已取消', 409)
                    await _completion_quota(lease, owner_id, status)
                    if selected is not None:
                        await io(guard_complete, prefix, upload_id, selected)

                    cache = {}
                    try:
                        receipt, source, before = await io(prepare_completion, prefix, upload_id, cache)
                        result = receipt['result']
                        if (result['path'], result['size']) != (status['path'], status['size']):
                            raise uploads.UploadError('远端上传元数据在完成期间变化', 409)

                        async def finalize(db, case, _transaction):
                            owner = await owner_for_case(db, case)
                            if owner.id != owner_id:
                                raise CaseWriteConflict('case_source_changed', '项目存储账户已变化，请刷新后重试')
                            await lock_owner(db, owner.id)
                            row = await upload_storage.row_for(db, case, owner, upload_id)
                            if row is None:
                                await upload_storage.register(db, case, owner, status)
                            elif (row.path, row.size, row.state) != (status['path'], status['size'], 'active'):
                                raise uploads.UploadError('上传额度与声明内容不一致', 409)
                            await upload_storage.settle(db, case, owner, upload_id, 'completed')
                            await append_event(db, case.id, uploads.COMPLETED_EVENT, receipt,
                                               event_id=uploads._completion_id(case.id, upload_id))

                        await write_reserved(lease, {result['path']: source}, {result['path']: before},
                                             scope='files', finalize=finalize)
                    finally:
                        if cache:
                            await finish_before_cancel(io(cleanup_cache, cache))

                result = dict(receipt['result'])
                try:
                    cleaned = await io(cleanup_completed, prefix, upload_id, receipt)
                except (OSError, ValueError, BotoCoreError, ClientError, CaseWriteConflict):
                    logging.getLogger(__name__).warning('Committed upload staging retained: case=%s upload=%s',
                                                        lease.case.id, upload_id, exc_info=True)
                    cleaned = False
                if not cleaned:
                    result['cleanup_pending'] = True
                return result


def cancel_metadata(prefix, upload_id, reservation, already_cancelled, initial):
    if initial is not None:
        from .upload_creation import materialize

        metadata_path = directory(upload_id) + '/meta.json'
        if snapshot_head(prefix, metadata_path, uploads.MAX_METADATA_BYTES) is None:
            materialize(prefix, initial)
    meta, etag = _meta(prefix, upload_id)
    if reservation is not None and (meta['path'], meta['size']) != reservation:
        raise uploads.UploadError('远端上传与额度记录不一致', 409)
    if meta['status'] != 'cancelled':
        if already_cancelled:
            raise uploads.UploadError('远端上传与已取消记录不一致，请保留现场检查', 409)
        meta.update(status='cancelled',updated_at=time.time())
        _save_meta(prefix,meta,etag)


def close_cancelled_chunks(prefix, upload_id):
    # The tombstone remains authoritative even if deletion is interrupted.
    root = directory(upload_id) + '/chunks/'
    stem = storage._s3_key(prefix, root)
    # Upload staging keys are private physical objects. Stream their listing
    # instead of allocating a merged public catalog and a complete key list.
    for item in storage.s3()._raw_pages(get_settings().s3_bucket, stem, page_size=CANCEL_PAGE_SIZE):
        key = item['Key']
        if not key.startswith(stem):
            raise uploads.UploadError('上传分块列表超出会话范围', 409)
        etag = item.get('ETag')
        if not key.endswith('/') and etag and item['Size'] != 0:
            _conditional(prefix, root + key[len(stem):], b'', '"'+etag.strip('"')+'"',
                         {'ContentType':'application/vnd.vibeflow.closed-upload-chunk'})


async def cancel(checked, upload_id, *, validate=None, verify=None, record=None):
    """Close the remote upload under a case lease and settle after its writes drain."""
    from ..db import SessionLocal
    from ..models import Case
    from . import case_copy_io, case_read_capacity, storage_operations, upload_storage
    from .case_provision import io
    from .owner_storage_budget import hold as owner_capacity, owner_for_case, recover_owner
    from .request_deadline import STAGING_CLEANUP_SECONDS
    from .s3_storage_recovery import recover_reserved
    from .storage_admission import lock_owner
    from .upload_chunk_credit import chunk_paths, close_pending_paths, registered_offsets
    from .upload_creation import pending_cancel_initial

    directory(upload_id)
    async def same_owner(db):
        case = await checked(db)
        if (await owner_for_case(db, case)).id != owner_id:
            raise CaseWriteConflict('case_source_changed', '项目存储账户已变化，请刷新后重试')
        return case

    async with case_read_capacity.file_read_deadline('upload_cancel', '取消上传', seconds=STAGING_CLEANUP_SECONDS):
        with case_copy_io.admit(code='upload_cancel_busy', label='取消上传'):
            # A killed chunk sender can leave its owner's whole pool open.
            # Reconcile it before the case lease attempts any physical PUT.
            async with SessionLocal() as db:
                case = await checked(db)
                owner_id = (await owner_for_case(db, case)).id
            await recover_owner(owner_id, wait=True, require_available=True)
            async with storage_operations.hold(same_owner, purpose='staging') as lease:
                await recover_reserved(lease)
                async with lease.control() as db:
                    case = await db.get(Case, lease.case.id)
                    if await uploads.completion_record(db, case, upload_id) is not None:
                        raise uploads.UploadError('上传已经提交，不能取消已完成文件', 409)
                    owner = await owner_for_case(db, case)
                    row = await upload_storage.row_for(db, case, owner, upload_id)
                    if validate is not None:
                        await validate(db, case, row)
                    bound = (row.path, row.size) if row is not None else None
                    already_cancelled = row is not None and row.state == 'cancelled'
                    initial = None
                    if row is not None and not already_cancelled:
                        initial = await pending_cancel_initial(db, case, upload_id)
                    offsets = await registered_offsets(db, upload_id)

                if verify is not None:
                    await verify(lease.case, already_cancelled)
                # Retain the existing exact-byte recovery proof for an
                # interrupted metadata PUT. Release the owner pool before
                # listing or closing any chunks.
                async with SessionLocal() as metadata_db:
                    async with owner_capacity(metadata_db, owner_id):
                        await metadata_db.commit()
                        await io(cancel_metadata, lease.case.s3_prefix, upload_id, bound, already_cancelled, initial)
                await io(close_cancelled_chunks, lease.case.s3_prefix, upload_id)
                await close_pending_paths(lease.case, chunk_paths(upload_id, offsets))

                async with lease.control() as db:
                    case = await db.get(Case, lease.case.id)
                    owner = await owner_for_case(db, case)
                    await lock_owner(db, owner.id)
                    await upload_storage.settle(db, case, owner, upload_id, 'cancelled')
                    if record is not None:
                        await record(db, case)
    return {'ok': True, 'cancelled': True}
