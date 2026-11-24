"""New case prefixes: immutable creation plan plus a witness in the caller's SQL txn.

Creation and recovery take the owner row lock before workspace/case locks. A
rolled-back numeric ID can never reuse an earlier physical prefix. Recovery
removes only admitted, unchanged files; committed content is never replayed.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tempfile
import time
import uuid

from sqlalchemy import select, update
from botocore.exceptions import ClientError

from .. import storage
from ..models import Case, DurableEvent, User
from . import attempt_objects as objects, attempt_workspace as workspace
from . import case_copy_io
from . import file_transactions as journal
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .durable_events import append_event, ensure_parent_transaction, json_text
from .request_deadline import current, scope

SCHEMA = 'vibeflow.case-provision.v1'
REMOTE_COPY_SCHEMA = 'vibeflow.case-provision-remote-copy.v1'
REMOTE_IMPORT_SCHEMA = 'vibeflow.case-provision-remote-import.v1'
REMOTE_RESTORE_SCHEMA = 'vibeflow.case-provision-remote-restore.v1'
EVENT = 'cases.provisioned'
MAX_PLAN = 16 * 1024 * 1024
MAX_FILES = 100000
MAX_FILE_BYTES = (1 << 63) - 1  # Copying existing solver outputs must not inherit the upload API's 1 GiB cap.


def conflict(message):
    return CaseWriteConflict('case_provision_recovery_required', message)


async def io(function, *args):
    return await finish_before_cancel(case_copy_io.submit(function, *args))


def check_deadline():
    deadline = current()
    if deadline is not None:
        deadline.check()


def digest(data):
    return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def owner_prefix(user_id):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError('Invalid provision owner')
    return f'workspaces/{user_id}/.case-provisions'


def prefix_for(user_id, case_id, identifier):
    return f'workspaces/{user_id}/cases/{uuid.UUID(identifier).hex}/{case_id}'


def reserve_prefix(user_id, case_id, identifier):
    """Keep the first-generation layout; permanently reserve its numeric name."""
    owner = owner_prefix(user_id)
    canonical = f'workspaces/{user_id}/cases/{case_id}'
    occupied = storage.list_tree(canonical) or (not storage._use_s3() and storage._fs_path(canonical, '.').exists())
    if not occupied:
        try:
            put_new(owner, f'claims/{case_id}.json', json_text({'id': identifier}).encode('utf8'))
            return canonical
        except FileExistsError:
            pass
        except ClientError as error:
            if error.response['ResponseMetadata']['HTTPStatusCode'] != 412:
                raise
    return prefix_for(user_id, case_id, identifier)


def binding(prefix):
    if storage._use_s3():
        return objects.binding(prefix)
    return {'backend': 'fs', 'prefix': prefix, 'root': str(storage._fs_path(prefix, '.'))}


def local_path(prefix, path):
    # Check unresolved parents too: a link into another in-root case is unsafe.
    root = storage._fs_root()
    return journal._path(root, prefix + '/' + path)


def snapshot(prefix, path):
    check_deadline()
    if storage._use_s3():
        from .s3_streams import snapshot as remote
        observed, etag, _ = remote(prefix, path, limit=MAX_FILE_BYTES)
        return observed, etag
    return journal._capture(local_path(prefix, path), limit=MAX_FILE_BYTES, deadline=current()), None


def read_record(prefix, path):
    check_deadline()
    if storage._use_s3():
        from .s3_file_transactions import _read
        data, etag, _ = _read(prefix, path, MAX_PLAN)
        return data, etag
    target = local_path(prefix, path)
    try:
        with target.open('rb') as stream:
            data = stream.read(MAX_PLAN + 1)
    except FileNotFoundError:
        return None, None
    if len(data) > MAX_PLAN:
        raise ValueError('Oversized creation plan')
    return data, None


def put_new(prefix, path, data, content_type='application/json'):
    from .case_source import CopySource
    source = data if isinstance(data, CopySource) else None
    check_deadline()
    if storage._use_s3():
        from .owner_storage_budget import current
        capacity=current()
        if capacity is None or capacity.closed or not prefix.startswith(f"workspaces/{capacity.ticket['user_id']}/"):
            raise ValueError('Remote creation requires its owner capacity context')
        def put(body):
            check_deadline()
            storage.s3().put_object(**objects.arguments(prefix, path), Body=body,
                                    ContentType=content_type, IfNoneMatch='*')
        if source is not None:
            from .node_storage_budget import reserve
            with reserve(source.size), tempfile.TemporaryFile(dir=storage._fs_root()) as stream:
                source.copy_into(stream)
                stream.seek(0)
                put(stream)
        else:
            put(data)
        return
    from .process_identity_native import observe
    target = local_path(prefix, path)
    owner = owner_prefix(int(prefix.split('/')[1]))
    identifier = uuid.uuid4().hex
    temporary = local_path(owner, 'staging/' + identifier + '.part')
    intent = local_path(owner, 'staging/' + identifier + '.json')
    expected = source.record if source is not None else digest(data)
    encoded = json_text({'schema': 'vibeflow.provision-stage.v2',
        'id': identifier, 'owner': owner, 'binding': binding(owner),
        'process': observe(os.getpid()), 'target': {'prefix': prefix, 'path': path},
        'created_at': time.time(), 'file': expected}).encode('utf8')
    from .local_provision_budget import admit,node_write
    peak = 2 * expected['size'] + len(encoded)
    admit(prefix, peak)
    # The physical scanner counts both names during hard-link installation.
    # Include that peak and the recovery intent before allocating either one.
    with node_write(prefix,peak,expected['size']):
        check_deadline()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.parent.mkdir(parents=True, exist_ok=True)
        try:
            journal._write_new(intent, encoded)
            if source is not None:
                source.copy_into(temporary)
                journal._sync(temporary.parent)
            else:
                journal._write_new(temporary, data)
            check_deadline()
            local_path(prefix, path)
            os.link(temporary, target)
            journal._sync(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
            intent.unlink(missing_ok=True)


def prepare(case_id, workspace_id, user_id, files, kind, identifier=None):
    if not files or len(files) > MAX_FILES:
        raise ValueError('Invalid creation file count')
    identifier = identifier or str(uuid.uuid4())
    if str(uuid.UUID(identifier))!=identifier:raise ValueError('Invalid creation identity')
    prefix = prefix_for(user_id, case_id, identifier)
    _validate_inventory(prefix, files)
    owner = owner_prefix(user_id)
    if storage._use_s3():
        from .s3_case_files import require_conditions
        try:
            require_conditions(owner)
        except ValueError as error:
            raise CaseWriteConflict('storage_conditions_unavailable',
                '对象存储不支持创建恢复所需的条件写入和删除，请使用兼容的服务端版本') from error
    prefix = reserve_prefix(user_id, case_id, identifier)
    plan = {'schema': SCHEMA, 'id': identifier, 'user_id': user_id, 'case_id': case_id,
            'workspace_id': workspace_id, 'kind': kind, 'binding': binding(prefix), 'files': files}
    data = json_text(plan).encode('utf8')
    if len(data) > MAX_PLAN:
        raise ValueError('Creation plan exceeds 16 MiB')
    if storage.list_tree(prefix):
        raise conflict('新算例目标目录已被占用，请保留现场后重试')
    put_new(owner, f'pending/{identifier}.json', data)
    return plan


def write_file(plan, path, data):
    from .case_source import CopySource
    observed = data.record if isinstance(data, CopySource) else digest(data)
    if plan['files'].get(path) != observed:
        raise conflict('创建期间来源内容已变化，未写入目标文件')
    prefix = plan['binding']['prefix']
    put_new(prefix, path, data, mimetypes.guess_type(path)[0] or 'application/octet-stream')


def prepare_remote_copy(user_id, source_id, files, kind):
    """Claim an unpublished S3 generation before any destination body is sent."""
    identifier = str(uuid.uuid4())
    prefix = prefix_for(user_id, source_id, identifier)
    plan = {'schema': REMOTE_COPY_SCHEMA, 'id': identifier, 'user_id': user_id,
            'case_id': None, 'workspace_id': None, 'source_case_id': source_id,
            'kind': kind, 'binding': binding(prefix), 'files': files}
    return _persist_remote_draft(plan)


def prepare_remote_import(user_id, workspace_id, files):
    identifier = str(uuid.uuid4())
    plan = {'schema': REMOTE_IMPORT_SCHEMA, 'id': identifier, 'user_id': user_id,
            'case_id': None, 'workspace_id': None, 'target_workspace_id': workspace_id,
            'kind': 'case.import', 'binding': binding(prefix_for(user_id, workspace_id, identifier)), 'files': files}
    return _persist_remote_draft(plan)


def prepare_remote_restore(user_id, case_number, files):
    identifier = str(uuid.uuid4())
    plan = {'schema': REMOTE_RESTORE_SCHEMA, 'id': identifier, 'user_id': user_id,
            'case_id': None, 'workspace_id': None, 'case_number': case_number,
            'kind': 'backup.restore', 'binding': binding(prefix_for(user_id, case_number, identifier)), 'files': files}
    return _persist_remote_draft(plan)


def _persist_remote_draft(plan):
    from .s3_case_files import require_conditions

    user_id, identifier = plan['user_id'], plan['id']
    validate_plan(user_id, identifier, plan)
    owner = owner_prefix(user_id)
    try:
        require_conditions(owner)
    except ValueError as error:
        raise CaseWriteConflict('storage_conditions_unavailable',
            '对象存储不支持复制恢复所需的条件写入和删除') from error
    if storage.list_tree(plan['binding']['prefix']):
        raise conflict('新算例目标目录已被占用，请保留现场后重试')
    put_new(owner, f'pending/{identifier}.json', _encoded_plan(plan))
    return plan


def _encoded_plan(plan):
    encoded = json_text(plan).encode('utf8')
    if len(encoded) > MAX_PLAN:
        raise ValueError('Creation plan exceeds 16 MiB')
    return encoded


def bind_remote_copy(draft, case_id, workspace_id, files, *, file_moves=None):
    """Extend the recovery record before writing any generated metadata."""
    check_deadline()
    user_id, identifier = draft['user_id'], draft['id']
    owner, path = owner_prefix(user_id), f'pending/{identifier}.json'
    raw, version = read_record(owner, path)
    if raw != _encoded_plan(draft) or not version:
        raise conflict('远端复制准备记录已变化')
    plan = {**draft, 'case_id': case_id, 'workspace_id': workspace_id, 'files': files}
    if draft['schema'] == REMOTE_RESTORE_SCHEMA:
        plan['file_moves'] = file_moves or {}
        if _restore_staged_files(plan) != draft['files']:
            raise conflict('备份恢复的已校验正文清单已变化')
    elif file_moves is not None or any(files.get(path) != expected for path, expected in draft['files'].items()):
        raise conflict('远端复制的已校验正文清单已变化')
    validate_plan(user_id, identifier, plan)
    storage.s3().put_object(**objects.arguments(owner, path), Body=_encoded_plan(plan),
                           ContentType='application/json', IfMatch=version)
    return plan


def verify_files(plan):
    prefix = plan['binding']['prefix']
    actual = {item['path'] for item in storage.list_tree(prefix)}
    if actual != set(plan['files']):
        raise conflict('新算例文件清单与创建记录不一致')
    for path, expected in plan['files'].items():
        if snapshot(prefix, path)[0] != expected:
            raise conflict('新算例内容与创建记录不一致')


async def lock_owner(db, user_id):
    await ensure_parent_transaction(db)
    result = await db.execute(update(User).where(User.id == user_id).values(name=User.name)
                              .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise conflict('创建记录的账户已不存在')


async def seal(db, case, plan):
    _assert_case_binding(case, plan)
    await io(verify_files, plan)
    await record_witness(db, case, plan)


def _assert_case_binding(case, plan):
    if case.id != plan['case_id'] or case.workspace_id != plan['workspace_id'] or case.s3_prefix != plan['binding']['prefix']:
        raise ValueError('Creation plan differs from SQL case')


async def record_witness(db, case, plan):
    """Record a verified provision after its storage implementation installs it."""
    _assert_case_binding(case, plan)
    await append_event(db, case.id, EVENT, {'provision_id': plan['id'], 'prefix': case.s3_prefix,
        'plan_sha256': hashlib.sha256(json_text(plan).encode('utf8')).hexdigest()}, event_id=plan['id'])
    db.info.setdefault('case_provisions', {})[plan['id']] = db.get_transaction()


def load(user_id, path):
    if not path.startswith('pending/') or not path.endswith('.json') or path.count('/') != 1:
        raise ValueError('Unexpected pending creation entry')
    identifier = path[len('pending/'):-len('.json')]
    if str(uuid.UUID(identifier)) != identifier:
        raise ValueError('Invalid creation identity')
    raw, etag = read_record(owner_prefix(user_id), path)
    if raw is None:
        raise ValueError('Creation record vanished')
    plan = json.loads(raw)
    validate_plan(user_id, identifier, plan)
    return plan, raw, etag


def validate_plan(user_id, identifier, plan):
    """Shared validation for pending plans and retained recovery receipts."""
    remote_schemas = {REMOTE_COPY_SCHEMA, REMOTE_IMPORT_SCHEMA, REMOTE_RESTORE_SCHEMA}
    if plan['schema'] not in remote_schemas | {SCHEMA} or plan['id'] != identifier or plan['user_id'] != user_id:
        raise ValueError('Creation record owner/identity differs')
    draft = plan['schema'] in remote_schemas and plan['case_id'] is None and plan['workspace_id'] is None
    if not draft and (type(plan['case_id']) is not int or plan['case_id'] <= 0
            or type(plan['workspace_id']) is not int or plan['workspace_id'] <= 0):
        raise ValueError('Invalid creation case/workspace')
    prefix = plan['binding']['prefix']
    canonical = f"workspaces/{user_id}/cases/{plan['case_id']}"
    if plan['schema'] == REMOTE_COPY_SCHEMA:
        source_id = plan['source_case_id']
        if (not storage._use_s3() or type(source_id) is not int or source_id <= 0
                or plan['kind'] not in {'case.copy','workspace.copy'}
                or prefix != prefix_for(user_id, source_id, identifier)):
            raise ValueError('Invalid remote copy generation')
    elif plan['schema'] == REMOTE_IMPORT_SCHEMA:
        target_id = plan['target_workspace_id']
        if (not storage._use_s3() or type(target_id) is not int or target_id <= 0
                or plan['kind'] != 'case.import' or (not draft and plan['workspace_id'] != target_id)
                or prefix != prefix_for(user_id, target_id, identifier)):
            raise ValueError('Invalid remote import generation')
    elif plan['schema'] == REMOTE_RESTORE_SCHEMA:
        number = plan['case_number']
        if (not storage._use_s3() or type(number) is not int or not 1 <= number <= MAX_FILES
                or plan['kind'] != 'backup.restore' or prefix != prefix_for(user_id, number, identifier)
                or draft == ('file_moves' in plan)):
            raise ValueError('Invalid remote restore generation')
    elif prefix == canonical:
        claim, _ = read_record(owner_prefix(user_id), f"claims/{plan['case_id']}.json")
        if claim != json_text({'id': identifier}).encode('utf8'):
            raise ValueError('Numeric prefix reservation differs')
    elif prefix != prefix_for(user_id, plan['case_id'], identifier):
        raise ValueError('Creation prefix differs from its generation')
    if plan['binding'] != binding(prefix):
        raise ValueError('Creation storage binding differs')
    files = plan['files']
    if not isinstance(files, dict) or len(files) > MAX_FILES or (not files and not draft):
        raise ValueError('Invalid creation file inventory')
    _validate_inventory(prefix, files)
    if plan['schema'] == REMOTE_RESTORE_SCHEMA and not draft:
        _validate_inventory(prefix, _restore_staged_files(plan))


def _validate_inventory(prefix, files):
    paths = set()
    for name, expected in files.items():
        check_deadline()
        workspace.assert_mutable_path(prefix, name)
        if workspace.canonical_path(name) != name or name.startswith('.provision-tmp/'):
            raise ValueError('Invalid creation path')
        folded = name.casefold()
        if folded in paths:
            raise ValueError('Overlapping creation paths')
        paths.add(folded)
        if set(expected) != {'size', 'sha256'} or type(expected['size']) is not int or not 0 <= expected['size'] <= MAX_FILE_BYTES:
            raise ValueError('Invalid creation size')
        if not isinstance(expected['sha256'], str) or len(expected['sha256']) != 64 or any(c not in '0123456789abcdef' for c in expected['sha256']):
            raise ValueError('Invalid creation digest')
    for path in paths:
        check_deadline()
        parent = path.rpartition('/')[0]
        while parent:
            if parent in paths:
                raise ValueError('Overlapping creation paths')
            parent = parent.rpartition('/')[0]


def _restore_staged_files(plan):
    moves, files = plan['file_moves'], plan['files']
    if not isinstance(moves, dict) or len(moves) > len(files):
        raise ValueError('Invalid restored log moves')
    previous = {}
    for source, target in moves.items():
        check_deadline()
        if (not re.fullmatch(r'\.restore-[0-9a-f]{32}/[1-9][0-9]*\.log', source)
                or not isinstance(target, str) or not re.fullmatch(r'\.vibeflow/jobs/job-[1-9][0-9]*\.log', target)
                or source in files or target not in files or target in previous):
            raise ValueError('Invalid restored log move')
        previous[target] = source
    return {previous.get(path, path): record for path, record in files.items()}


def observe_rollback(plan):
    """Verify the whole unfinished generation without holding a SQL writer."""
    from .local_file_edits import _version

    prefix = plan['binding']['prefix']
    actual = {item['path'] for item in storage.list_tree(prefix)}
    inventories = [plan['files']]
    if plan['schema'] == REMOTE_RESTORE_SCHEMA and plan.get('file_moves'):
        # A durable bind can outlive the following catalog CAS. Both possible
        # layouts are recoverable, including an interrupted partial cleanup.
        inventories.append(_restore_staged_files(plan))
    inventories = [inventory for inventory in inventories if actual <= inventory.keys()]
    if not inventories:
        raise conflict('未提交算例中存在不属于创建记录的文件，已保留现场')
    observed = {}
    for path in actual:
        check_deadline()
        local = None if storage._use_s3() else local_path(prefix, path)
        before = _version(local) if local is not None else None
        value, etag = snapshot(prefix, path)
        if local is not None and (before is None or _version(local) != before):
            raise conflict('未提交算例在核验期间变化，已保留现场')
        inventories = [inventory for inventory in inventories if value == inventory[path]]
        if not inventories:
            raise conflict('未提交算例内容已被外部修改，已保留现场')
        if local is None and not etag:
            raise conflict('未提交算例缺少对象版本，已保留现场')
        observed[path] = before if local is not None else etag
    return observed


def rollback_files(plan, observed):
    """Recheck only identities under the owner lock, then conditionally delete."""
    from .local_file_edits import _version

    prefix = plan['binding']['prefix']
    remote = storage._use_s3()
    actual = {item['path']: item for item in storage.list_tree(prefix)}
    if actual.keys() != observed.keys():
        raise conflict('清理期间文件清单变化，已保留现场')
    for path, version in observed.items():
        check_deadline()
        if remote:
            etag = actual[path].get('etag')
            matches = etag and etag.strip('"') == version.strip('"')
        else:
            matches = _version(local_path(prefix, path)) == version
        if not matches:
            raise conflict('清理期间文件版本变化，已保留现场')
    for path, version in observed.items():
        check_deadline()
        if remote:
            storage.s3().delete_object(**objects.arguments(prefix, path), IfMatch=version)
        else:
            local = local_path(prefix, path)
            if _version(local) != version:
                raise conflict('清理期间文件变化，停止并保留剩余现场')
            check_deadline()
            local.unlink()
    if storage.list_tree(prefix):
        raise conflict('清理期间出现额外内容，已保留创建记录')
    from .storage_maintenance import empty_case_directories
    empty_case_directories(prefix, plan['files'])


def finish(plan, raw, etag, outcome):
    check_deadline()
    owner, identifier = owner_prefix(plan['user_id']), plan['id']
    history = f'history/{identifier}.json'
    receipt = json_text({'plan': plan, 'outcome': outcome}).encode('utf8')
    previous, _ = read_record(owner, history)
    if previous is None:
        put_new(owner, history, receipt)
    elif previous != receipt:
        raise conflict('创建恢复回执不一致，已保留现场')
    check_deadline()
    pending = f'pending/{identifier}.json'
    if storage._use_s3():
        storage.s3().delete_object(**objects.arguments(owner, pending), IfMatch=etag)
    else:
        if read_record(owner, pending)[0] != raw:
            raise conflict('创建记录在清理期间变化')
        local_path(owner, pending).unlink()


_RECOVERY_ERRORS = (ValueError, OSError, KeyError, TypeError, ClientError)


async def recover_owner(db, user_id, *, wait=False):
    """Recover before business writes, then claim owner control for the caller."""
    from ..config import get_settings
    from .storage_admission import lock_owner as lock_quota
    from .storage_maintenance import recover_stages
    active = db.get_transaction()
    if db.new or db.dirty or db.deleted or (active is not None and active in db.info.get('case_provisions', {}).values()):
        raise conflict('当前事务已有修改，不能提前恢复创建记录')
    try:
        async with scope(get_settings().storage_measurement_timeout_seconds):
            # Only the route's authentication/selection transaction precedes
            # recovery. Each application below owns and commits its own work.
            await finish_before_cancel(db.commit())
            with case_copy_io.admit(code='case_provision_busy', label='构造恢复'):
                cleaned = await recover_stages(db, user_id, wait=wait)
                entries = await io(storage.list_tree, owner_prefix(user_id), 'pending')
                results = await recover_pending(db, user_id, (entry['path'] for entry in entries), wait=wait)
            await lock_quota(db, user_id, wait=wait)
            await lock_owner(db, user_id)
            return cleaned + results
    except TimeoutError as error:
        raise conflict('创建记录恢复超时，稍后可重试或先运行存储维护') from error


async def recover_pending(db, user_id, paths, *, wait=False):
    """Observe and settle each pending record separately, before caller writes."""
    from .local_provision_budget import hold

    owner = owner_prefix(user_id)
    paths = list(paths)
    results = []
    if paths and storage._use_s3():
        from .s3_case_files import require_conditions
        try:
            await io(require_conditions, owner)
        except ValueError as error:
            return [{'status': 'recovery_required', 'reason': str(error)}]
    for path in paths:
        check_deadline()
        try:
            plan, raw, etag = await io(load, user_id, path)
            payload = {'provision_id': plan['id'], 'prefix': plan['binding']['prefix'],
                       'plan_sha256': hashlib.sha256(json_text(plan).encode('utf8')).hexdigest()}
            witnessed = await db.scalar(select(DurableEvent.id).where(DurableEvent.id == plan['id']))
            await finish_before_cancel(db.commit())
            observation, observed_error = None, None
            if witnessed is None:
                try:
                    observation = await io(observe_rollback, plan)
                except TimeoutError:
                    raise
                except _RECOVERY_ERRORS as error:
                    observed_error = error

            async with hold(db, user_id, wait=wait):
                await lock_owner(db, user_id)
                latest = await io(read_record, owner, path)
                if latest[0] is None:
                    await finish_before_cancel(db.rollback())
                    continue  # Another observer has already settled this record.
                if latest != (raw, etag):
                    raise conflict('创建记录在核验期间发生变化，稍后可重试')
                witness = await db.get(DurableEvent, plan['id'], populate_existing=True)
                if witness is not None:
                    if (witness.type != EVENT or witness.case_id != plan['case_id']
                            or json.loads(witness.payload_json) != payload):
                        raise conflict('创建提交证明与文件记录不一致')
                    outcome = 'committed'
                else:
                    case_id = await db.scalar(select(Case.id).where(Case.s3_prefix == plan['binding']['prefix']).limit(1))
                    if case_id is not None:
                        raise conflict('算例已存在但缺少创建提交证明，已保留现场')
                    if observed_error is not None:
                        raise observed_error
                    if observation is None:
                        raise conflict('创建提交证明在核验期间变化，稍后可重试')
                    await io(rollback_files, plan, observation)
                    outcome = 'rolled_back'
                await io(finish, plan, raw, etag, outcome)
                await finish_before_cancel(db.commit())
            results.append({'id': plan['id'], 'case_id': plan['case_id'], 'status': outcome})
        except TimeoutError:
            raise
        except _RECOVERY_ERRORS as error:
            await finish_before_cancel(db.rollback())
            results.append({'record': path, 'status': 'recovery_required', 'reason': str(error)})
    return results


async def create_files(db, case, user_id, files, kind='create'):
    """Caller holds owner/workspace locks; commit remains the caller's responsibility."""
    return await create_from_manifest(db, case, user_id,
        {path: digest(data) for path, data in files.items()}, files.__getitem__, kind)


async def create_from_manifest(db, case, user_id, manifest, reader, kind, verify_source=None):
    """Read one admitted file at a time; never hold a whole copied case in memory."""
    from .local_provision_budget import hold
    async with hold(db, user_id):
        return await _create_from_manifest(db, case, user_id, manifest, reader, kind, verify_source)


async def _create_from_manifest(db, case, user_id, manifest, reader, kind, verify_source):
    if not storage._use_s3():
        from .local_provision_budget import node_batch
        identifier=str(uuid.uuid4())
        maximum=await io(node_creation_peak,case.id,case.workspace_id,user_id,manifest,kind,identifier)
        async with node_batch(user_id, maximum, reserve_owner=(kind == 'create')):
            return await _write_manifest(db,case,user_id,manifest,reader,kind,verify_source,identifier)
    return await _write_manifest(db,case,user_id,manifest,reader,kind,verify_source)


def node_creation_peak(case_id,workspace_id,user_id,manifest,kind,identifier):
    """Bound retained outputs plus one temporary/hardlink installation peak.

    Use the longer generation prefix even if the numeric prefix is available.
    Actual writes check this bound again; no inventory result is cached.
    """
    from .process_identity_native import observe
    if not manifest or len(manifest)>MAX_FILES:raise ValueError('Invalid creation inventory')
    owner=owner_prefix(user_id);prefix=prefix_for(user_id,case_id,identifier)
    plan={'schema':SCHEMA,'id':identifier,'user_id':user_id,'case_id':case_id,
        'workspace_id':workspace_id,'kind':kind,'binding':binding(prefix),'files':manifest}
    encoded=json_text(plan).encode('utf8')
    if len(encoded)>MAX_PLAN:raise ValueError('Creation plan exceeds its bound')
    claim=json_text({'id':identifier}).encode('utf8')
    writes=[(owner,f'claims/{case_id}.json',digest(claim)),(owner,f'pending/{identifier}.json',digest(encoded))]
    writes.extend((prefix,path,value) for path,value in manifest.items())
    birth=observe(os.getpid());retained=0;maximum=0
    for target,path,value in writes:
        if (not isinstance(value,dict) or set(value)!={'size','sha256'}
                or type(value['size']) is not int or not 0<=value['size']<=MAX_FILE_BYTES):
            raise ValueError('Invalid creation file size')
        metadata=json_text({'schema':'vibeflow.provision-stage.v2','id':'0'*32,'owner':owner,
            'binding':binding(owner),'process':birth,'target':{'prefix':target,'path':path},
            'created_at':0.0,'file':value}).encode('utf8')
        # JSON's finite float timestamp representation is shorter than this
        # allowance; the real encoded record is still checked by node_write.
        peak=retained+2*value['size']+len(metadata)+32
        maximum=max(maximum,peak);retained+=value['size']
    return maximum


async def _write_manifest(db,case,user_id,manifest,reader,kind,verify_source,identifier=None):
    plan = await io(prepare, case.id, case.workspace_id, user_id,
                    manifest, kind,identifier)
    case.s3_prefix = plan['binding']['prefix']
    for path in manifest:
        data = await io(reader, path)
        await io(write_file, plan, path, data)
        del data
    if verify_source is not None:
        await io(verify_source)
    await seal(db, case, plan)
    return plan
