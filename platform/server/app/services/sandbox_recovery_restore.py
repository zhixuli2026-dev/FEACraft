"""Explicit, optimistic restoration of selected snapshot file contents.

Directory and permission restoration are separate from these file operations.
Every selected destination is previewed, then rechecked under the case writer.
"""
import asyncio
from contextlib import asynccontextmanager
from functools import partial
import hashlib
import json
from pathlib import Path
import stat
import time
from types import SimpleNamespace
import uuid

from fastapi import HTTPException

from .. import storage
from ..db import SessionLocal
from ..models import Case, DurableEvent, NativeRequest, NativeSession, User
from . import sandbox_recovery_execution as recovery, sandbox_volume, file_transactions as files
from .attempt_workspace import assert_mutable_path, canonical_path
from .case_write import CaseWriteConflict, assert_case_available, bounded_case_read, is_case_dictionary
from .case_write_capacity import committing
from .case_read_capacity import file_read_deadline, reading
from .durable_events import append_event, json_text, lock_case_execution

SCHEMA = 'vibeflow.sandbox-file-content-restore.v1'
EVENT = 'terminal.recovery_files_restored'
_slots = asyncio.Semaphore(2)


@asynccontextmanager
async def admitted():
    try:
        await asyncio.wait_for(_slots.acquire(), .1)
    except TimeoutError:
        raise CaseWriteConflict('recovery_busy', '其他恢复预览或写回正在进行，请稍后重试')
    try:yield
    finally:_slots.release()


def digest(value):
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def selection(paths, manifest, prefix, *, baseline=None, semantics='file_contents_only'):
    if semantics == 'case_dictionary':
        from .casedict import CASE_DICT_PATH
        if paths != [CASE_DICT_PATH] or CASE_DICT_PATH not in manifest['files']:
            raise CaseWriteConflict('recovery_dictionary_selection', '请选择快照中的 caseDict；配置删除或类型变化需在配置编辑器处理')
        assert_mutable_path(prefix, CASE_DICT_PATH)
        return paths
    maximum = sandbox_volume.MAX_ENTRIES if baseline is not None else files.MAX_FILES
    available = manifest['files'].keys() | (baseline['files'].keys() if baseline is not None else set())
    if not isinstance(paths, list) or not 1 <= len(paths) <= maximum:
        raise ValueError('Snapshot file selection exceeds its admitted bound')
    seen = set()
    for path in paths:
        if not isinstance(path, str) or canonical_path(path) != path or path not in available:
            raise ValueError('Selection must refer to a file in this snapshot')
        assert_mutable_path(prefix, path)
        if is_case_dictionary(prefix, path):
            raise CaseWriteConflict('recovery_protected_path', 'caseDict 必须通过专用编辑流程恢复', path=path)
        if path.casefold() in seen:
            raise ValueError('Duplicate restore path')
        seen.add(path.casefold())
    return sorted(paths)


def baseline_for(value, semantics):
    if semantics == 'file_contents_only':return None
    if semantics not in {'sandbox_file_changes', 'case_dictionary'}:raise ValueError('Unknown restore semantics')
    if 'baseline' not in value:
        raise CaseWriteConflict('recovery_baseline_missing', '此旧快照没有登记的初始清单，请使用选中文件内容写回')
    return value['baseline']


async def authorized(db, sid, identifier, user_id, expected=None, expected_profile=None):
    from ..routers.terminal import _authorize_terminal
    terminal = await db.get(NativeSession, sid, populate_existing=True)
    if terminal is None or terminal.created_by != user_id:
        raise HTTPException(404, 'Terminal session not found')
    await db.get(User, user_id, populate_existing=True)
    case = await _authorize_terminal(db, terminal.case_id, user_id)
    assert_case_available(case)
    row = await db.get(NativeRequest, identifier, populate_existing=True)
    if (row is None or row.kind != 'terminal.recover' or row.case_id != case.id
            or row.created_by != user_id or json.loads(row.profile_json).get('session_id') != sid):
        raise HTTPException(404, 'Recovery operation not found')
    selected = recovery.snapshot_profile(json.loads(row.profile_json))
    if expected_profile is not None and selected != expected_profile:
        raise CaseWriteConflict('recovery_evidence_changed', '恢复快照来源记录已变化，请重新预览')
    if selected['source_prefix'] != case.s3_prefix:
        raise CaseWriteConflict('case_source_changed', '项目来源已变化，请先核对恢复目标')
    from .native_request_records import public
    if row.phase != 'succeeded' or public(row)['cleanup_verified'] is not True:
        raise CaseWriteConflict('recovery_not_complete', '快照尚未完成，不能写回')
    receipt = recovery.receipt(json.loads(row.result_json), selected)
    if expected is not None and receipt != expected:
        raise CaseWriteConflict('recovery_evidence_changed', '恢复快照记录已变化，请重新预览')
    return case


def current_files(prefix, paths):
    result, remaining = {}, sandbox_volume.MAX_BYTES
    deadline = time.monotonic() + 30
    for path in paths:
        if time.monotonic() >= deadline:
            raise TimeoutError('Restore preview exceeded its read deadline')
        if storage._use_s3():
            from .s3_streams import snapshot
            value, _, _ = snapshot(prefix, path, limit=remaining)
        else:
            target = files._path(storage._fs_path(prefix, '.'), path)
            if target.exists() and not stat.S_ISREG(target.stat().st_mode):
                raise CaseWriteConflict('recovery_destination_type', '目标已是目录，不能按文件内容覆盖', path=path)
            value = files._capture(target, limit=remaining)
        if value is not None: remaining -= value['size']
        result[path] = value
    return result


def _stale():
    return CaseWriteConflict('recovery_plan_stale', '项目文件在预览后已变化，请重新预览并确认')


def _version(head):
    return None if head is None else (head['ETag'], head['ContentLength'])


def _selected_s3_files(prefix, paths):
    from .request_deadline import current
    from .s3_streams import snapshot_from_head, snapshot_head

    records, versions = {}, {}
    remaining = sandbox_volume.MAX_BYTES
    deadline = current()
    for path in paths:
        if deadline is not None:
            deadline.check()
        head = snapshot_head(prefix, path, remaining)
        versions[path] = _version(head)
        if head is None:
            records[path] = None
            continue
        records[path], _, _ = snapshot_from_head(prefix, path, head)
        remaining -= records[path]['size']
    return records, versions


def _require_versions(prefix, versions):
    from .request_deadline import current
    from .s3_streams import snapshot_head

    deadline = current()
    for path, expected in versions.items():
        if deadline is not None:
            deadline.check()
        if _version(snapshot_head(prefix, path, sandbox_volume.MAX_BYTES)) != expected:
            raise _stale()


def _reference(case):
    return SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix, dict_revision=case.dict_revision,
                           workflow_state=case.workflow_state)


def plan(value, case, paths, current, semantics='file_contents_only'):
    baseline = baseline_for(value, semantics)
    entries = []
    for path in paths:
        target = value['manifest']['files'].get(path)
        present = current[path]
        original = baseline['files'].get(path) if baseline is not None else None
        action = ('already_matches' if present == target else 'create' if present is None
                  else 'delete' if target is None else 'overwrite')
        if baseline is not None:
            if original == target:action = 'keep_current'
            elif present != target and present != original:action = 'conflict'
        entries.append({'path':path, 'current':present, 'snapshot':target,
            'action':action,
            **({'initial': original} if baseline is not None else {}),
            'snapshot_mode':value['manifest']['modes'].get(path)})
    document = {'schema':SCHEMA, 'session_id':value['session_id'], 'recovery_id':value['id'],
        'case_id':case.id, 'source_prefix':case.s3_prefix, 'receipt':value['receipt'],
        'semantics':semantics, 'directories_restored':False, 'permissions_restored':False,
        'deletions_applied':baseline is not None, 'files':entries}
    if baseline is not None:document['baseline_sha256'] = value['baseline_sha256']
    if semantics == 'case_dictionary':
        document['dictionary_revision'] = case.dict_revision
        document['workflow_state'] = case.workflow_state
        document['deletions_applied'] = False
    return {**document, 'plan_sha256':digest(document)}


def _dictionary_content(selected, value, case):
    """Use the same syntax, schema and workflow gates as the dedicated editor."""
    from .casedict import CASE_DICT_PATH, _validate_update
    data = payload(selected, value['manifest'], [CASE_DICT_PATH])
    try:
        content = data[CASE_DICT_PATH].decode('utf8')
        _validate_update(case, content, source='sandbox-recovery')
    except (UnicodeError, ValueError, RecursionError):
        raise CaseWriteConflict('recovery_dictionary_invalid',
            '快照配置未通过语法、结构或阶段校验。请在配置编辑器核对；恢复不会跳过阶段确认') from None
    return content


async def _preview_remote(authorize, value, selected, paths, semantics):
    code, label = 'sandbox_restore_preview', '沙箱文件恢复预览'
    async with file_read_deadline(code, label), SessionLocal() as db:
        case = await authorize(db)
        async with reading(db, case):
            reference = _reference(await authorize(db))
        records, versions = await bounded_case_read(_selected_s3_files, reference.s3_prefix, paths, code=code, label=label)
        if semantics == 'case_dictionary':
            await bounded_case_read(_dictionary_content, selected, value, reference, code=code, label=label)
        case = await authorize(db)
        async with reading(db, case):
            case = await authorize(db)
            if reference.s3_prefix != case.s3_prefix or (semantics == 'case_dictionary' and reference != _reference(case)):
                raise _stale()
            await bounded_case_read(_require_versions, case.s3_prefix, versions, code=code, label=label)
            return plan(value, case, paths, records, semantics)


async def preview(sid, identifier, user_id, paths, semantics='file_contents_only'):
    if semantics == 'sandbox_tree_changes':
        from .sandbox_tree_recovery import preview as tree_preview
        return await tree_preview(sid, identifier, user_id, paths)
    async with admitted():
        value, selected = await recovery.load(sid, identifier, user_id)
        if 'manifest' not in value:
            raise CaseWriteConflict('recovery_not_complete', '快照尚未完成，不能预览写回')
        baseline = baseline_for(value, semantics)
        paths = selection(paths, value['manifest'], selected['source_prefix'], baseline=baseline, semantics=semantics)

        async def authorize(db):
            return await authorized(db, sid, identifier, user_id, value['receipt'], selected)

        if storage._use_s3():
            return await _preview_remote(authorize, value, selected, paths, semantics)
        async with SessionLocal() as db:
            case = await authorize(db)
            async with reading(db, case):
                case = await authorize(db)
                current = await files._io(current_files, case.s3_prefix, paths)
                if semantics == 'case_dictionary':
                    await files._io(_dictionary_content, selected, value, case)
                return plan(value, case, paths, current, semantics)


async def changes(sid, identifier, user_id):
    """Inventory sandbox file deltas; no automatic selection or publication."""
    async with admitted():
        value, selected = await recovery.load(sid, identifier, user_id)
        initial = baseline_for(value, 'sandbox_file_changes')
        after = value['manifest']
        names = sorted(initial['files'].keys() | after['files'].keys())
        return {'baseline_sha256': value['baseline_sha256'], 'semantics': 'sandbox_file_changes',
                'max_selection': sandbox_volume.MAX_ENTRIES,
                'files': [{'path': path, 'initial': initial['files'].get(path), 'snapshot': after['files'].get(path),
                           'protected': is_case_dictionary(selected['source_prefix'], path)}
                          for path in names if initial['files'].get(path) != after['files'].get(path)],
                'directory_changes': sorted(set(initial['directories']) ^ set(after['directories'])),
                'permission_baseline_available': 'modes' in initial,
                'permission_changes': sorted(path for path in initial.get('modes', {}).keys() & after['modes'].keys()
                                             if initial['modes'][path] != after['modes'][path])}


def payload(selected, manifest, paths):
    changes, total = {}, 0
    deadline = time.monotonic() + 30
    for path in paths:
        expected = manifest['files'].get(path)
        if expected is None:
            changes[path] = None
            continue
        total += expected['size']
        if total > sandbox_volume.MAX_BYTES or time.monotonic() >= deadline:
            raise ValueError('Restore payload exceeds its bound')
        source = files._path(Path(selected['root']) / 'files', path)
        with source.open('rb') as stream:
            data = stream.read(expected['size'] + 1)
        if {'size':len(data), 'sha256':hashlib.sha256(data).hexdigest()} != expected:
            raise CaseWriteConflict('recovery_evidence_invalid', '恢复文件未通过快照校验', path=path)
        changes[path] = data
    return changes


def file_sources(selected, manifest, paths):
    changes = {}
    total = 0
    snapshot_id = Path(selected['root']).name
    for path in paths:
        expected = manifest['files'].get(path)
        if expected is None:
            changes[path] = None
            continue
        total += expected['size']
        if total > sandbox_volume.MAX_BYTES:
            raise ValueError('Restore payload exceeds its bound')
        changes[path] = files.RecoveryFileSource(path, expected, snapshot_id, selected['source_prefix'])
    return changes


async def _replay(db, case, intent, request_id):
    previous = await db.get(DurableEvent, request_id)
    if previous is None:
        return None
    old = json.loads(previous.payload_json)
    if previous.case_id != case.id or previous.type != EVENT or old.get('intent') != intent:
        raise CaseWriteConflict('recovery_request_conflict', '这次确认标识已用于其他操作，请重新预览')
    return {**old['result'], 'replayed':True}


def _confirmed(value, case, intent, current):
    prepared = plan(value, case, intent['paths'], current, intent['semantics'])
    if prepared['plan_sha256'] != intent['plan_sha256']:
        raise _stale()
    conflicts = [entry['path'] for entry in prepared['files'] if entry['action'] == 'conflict']
    if conflicts:
        raise CaseWriteConflict('recovery_merge_conflict', '项目和沙箱都修改了此文件，请保留冲突文件并取消其选择', path=conflicts[0])
    changed = [entry['path'] for entry in prepared['files'] if entry['action'] not in {'already_matches', 'keep_current'}]
    return prepared, changed


async def _receipt(db, case, intent, request_id, changes, transaction):
    semantics = intent['semantics']
    result = {'request_id':request_id, 'recovery_id':intent['recovery_id'], 'case_id':case.id,
              'written_files':list(changes), 'unchanged_files':[p for p in intent['paths'] if p not in changes],
              'file_transaction_id':transaction, 'semantics':semantics,
              'deleted_files':[name for name,data in changes.items() if data is None],
              'directories_restored':False, 'permissions_restored':False,
              'deletions_applied':semantics == 'sandbox_file_changes'}
    if semantics == 'case_dictionary':
        result.update(dictionary_revision=case.dict_revision, workflow_state=case.workflow_state)
    await append_event(db, case.id, EVENT, {'intent':intent, 'result':result}, event_id=request_id)
    return {**result, 'replayed':False}


def _prepare_dictionary(selected, value, reference):
    from .casedict import prepare_case_dict

    content = _dictionary_content(selected, value, reference)
    return prepare_case_dict(reference, content, source='sandbox-recovery')


async def _restore_remote(authorize, value, selected, intent, request_id):
    from . import case_copy_io, s3_staging_transactions, storage_operations
    from .request_deadline import SANDBOX_RECOVERY_SECONDS
    from .s3_storage_recovery import recover_reserved

    async def checked(db):
        case = await authorize(db)
        await lock_case_execution(db, case.id)
        return await authorize(db)

    code, label = 'sandbox_file_restore', '沙箱文件恢复'
    semantics, paths = intent['semantics'], intent['paths']
    async with file_read_deadline(code, label, seconds=SANDBOX_RECOVERY_SECONDS):
        async with SessionLocal() as db:
            previous = await _replay(db, await checked(db), intent, request_id)
            if previous is not None:
                return previous
        with case_copy_io.admit(code=code+'_busy', label=label):
            async with storage_operations.hold(checked, purpose='staging') as lease:
                await recover_reserved(lease)
                async with lease.control() as db:
                    case = await db.get(Case, lease.case.id)
                    previous = await _replay(db, case, intent, request_id)
                    if previous is not None:
                        return previous
                    reference = _reference(case)
                current, versions = await bounded_case_read(_selected_s3_files, reference.s3_prefix, paths,
                                                            code=code, label=label)
                _, changed = _confirmed(value, reference, intent, current)
                changes = file_sources(selected, value['manifest'], changed)
                if semantics == 'case_dictionary':
                    dictionary = await bounded_case_read(_prepare_dictionary, selected, value, reference,
                                                         code=code, label=label) if changed else None
                else:
                    dictionary = None

                async def finalize(db, case, transaction):
                    if semantics == 'case_dictionary':
                        if reference != _reference(case):
                            raise _stale()
                        if dictionary is not None:
                            from .casedict import _apply_dict_mirror
                            _apply_dict_mirror(case, dictionary)
                            await db.flush()
                        transaction = None
                    return await _receipt(db, case, intent, request_id, changes, transaction)

                await bounded_case_read(_require_versions, reference.s3_prefix, versions, code=code, label=label)
                if not changes:
                    if semantics == 'case_dictionary':
                        await bounded_case_read(_dictionary_content, selected, value, reference, code=code, label=label)
                    async with lease.control() as db:
                        return await finalize(db, await db.get(Case, lease.case.id), None)
                expected = {name: current[name] for name in changed}
                transfer, types = changes, None
                if dictionary is not None:
                    transfer = dictionary.files
                    types = dict.fromkeys(transfer, 'text/plain; charset=utf-8')
                    expected.update(dictionary.expected_files)
                scope = 'sandbox_recovery' if semantics == 'sandbox_file_changes' else 'files'
                unchanged = {path: version for path, version in versions.items() if path not in transfer}
                return await s3_staging_transactions.write_reserved(lease, transfer, expected, types, scope=scope,
                    verify=partial(_require_versions, reference.s3_prefix, unchanged), finalize=finalize)


async def restore(sid, identifier, user_id, paths, plan_sha256, request_id, semantics='file_contents_only'):
    if semantics == 'sandbox_tree_changes':
        from .sandbox_tree_recovery import restore as tree_restore
        return await tree_restore(sid, identifier, user_id, paths, plan_sha256, request_id)
    async with admitted():
        value, selected = await recovery.load(sid, identifier, user_id)
        if 'manifest' not in value:
            raise CaseWriteConflict('recovery_not_complete', '快照尚未完成，不能写回')
        baseline = baseline_for(value, semantics)
        paths = selection(paths, value['manifest'], selected['source_prefix'], baseline=baseline, semantics=semantics)
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError('Invalid restore request identity')
        intent = {'schema':SCHEMA, 'session_id':sid, 'recovery_id':identifier, 'created_by':user_id,
                  'paths':paths, 'plan_sha256':plan_sha256, 'semantics':semantics}

        async def authorize(db):
            return await authorized(db, sid, identifier, user_id, value['receipt'], selected)

        if storage._use_s3():
            return await _restore_remote(authorize, value, selected, intent, request_id)
        async with SessionLocal() as db:
            case = await authorize(db)
            async with committing(db, case):
                case = await authorize(db)
                previous = await _replay(db, case, intent, request_id)
                if previous is not None:
                    return previous
                current = await files._io(current_files, case.s3_prefix, paths)
                prepared, changed = _confirmed(value, case, intent, current)
                changes = file_sources(selected, value['manifest'], changed)
                await authorize(db)
                transaction = None
                if semantics == 'case_dictionary':
                    from .casedict import write_case_dict
                    content = await files._io(_dictionary_content, selected, value, case)
                    if changes:
                        await write_case_dict(db, case, content, source='sandbox-recovery',
                                              expected_revision=prepared['dictionary_revision'])
                elif changes:
                    transaction = await files.write(db, case, changes,
                        expected_files={name:current[name] for name in changed},
                        scope='sandbox_recovery' if baseline is not None else 'files')
                return await _receipt(db, case, intent, request_id, changes, transaction)


async def status(sid, identifier, user_id, request_id):
    async with SessionLocal() as db:
        case = await authorized(db, sid, identifier, user_id)
        event = await db.get(DurableEvent, request_id)
        if event is None or event.type != EVENT or event.case_id != case.id:
            raise HTTPException(404, 'Restore confirmation not found')
        document = json.loads(event.payload_json)
        if (document['intent']['session_id'] != sid or document['intent']['recovery_id'] != identifier
                or document['intent']['created_by'] != user_id):
            raise HTTPException(404, 'Restore confirmation not found')
        return document['result']
