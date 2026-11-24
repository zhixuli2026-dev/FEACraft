"""Explicit three-way directory, type and Linux mode restoration."""
import json
import os
from pathlib import Path
import uuid

from .. import storage
from ..db import SessionLocal
from ..models import Case, DurableEvent
from . import case_tree, file_transactions as files, sandbox_recovery_restore as common, tree_transactions
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, bounded_case_read
from .case_read_capacity import file_read_deadline, reading
from .case_write_capacity import committing
from .durable_events import append_event, lock_case_execution

SEMANTICS = 'sandbox_tree_changes'


def trees(value):
    initial = common.baseline_for(value, 'sandbox_file_changes')
    return case_tree.from_manifest(initial), case_tree.from_manifest(value['manifest'])


def plan(value, case, paths, present, raw):
    initial, after = trees(value)
    target, actions = case_tree.merge(case.s3_prefix, initial, after, present, paths)
    document = {'schema': 'vibeflow.sandbox-tree-restore.v1', 'semantics': SEMANTICS,
                'session_id': value['session_id'], 'recovery_id': value['id'], 'case_id': case.id,
                'source_prefix': case.s3_prefix, 'receipt': value['receipt'],
                'baseline_sha256': value['baseline_sha256'], 'current_sha256': common.digest(present),
                'metadata_sha256': common.digest(raw.decode() if raw is not None else None),
                'target_sha256': common.digest(target), 'files': actions,
                'directories_restored': True, 'permissions_restored': True, 'deletions_applied': True,
                'permissions_target': 'linux_execution_copy' if storage._use_s3() or os.name == 'nt' else 'filesystem'}
    return {**document, 'plan_sha256': common.digest(document)}, target


async def changes(sid, identifier, user_id):
    async with common.admitted():
        value, selected = await common.recovery.load(sid, identifier, user_id)
        initial, after = trees(value)
        entries = [{'path': p, 'initial': initial.get(p), 'snapshot': after.get(p),
                    'protected': case_tree.protected(selected['source_prefix'], p)}
                   for p in sorted(initial.keys() | after.keys()) if initial.get(p) != after.get(p)]
        return {'semantics': SEMANTICS, 'max_selection': 10000, 'files': entries,
                'directory_changes': [e['path'] for e in entries if any((e[s] or {}).get('kind') == 'directory' for s in ('initial','snapshot'))],
                'permission_changes': [e['path'] for e in entries if e['initial'] and e['snapshot'] and e['initial']['mode'] != e['snapshot']['mode']]}


async def preview(sid, identifier, user_id, paths):
    async with file_read_deadline('tree_preview', '目录树预览'), common.admitted():
        value, selected = await common.recovery.load(sid, identifier, user_id)
        async with SessionLocal() as db:
            async def authorize():
                return await common.authorized(db, sid, identifier, user_id, value['receipt'], selected)

            case = await authorize()
            async with reading(db, case):
                case = await authorize()
                prefix = case.s3_prefix
            await finish_before_cancel(db.commit())
            if storage._use_s3():
                from .tree_transactions_s3 import prepare_current, _require_current
                snapshot = await bounded_case_read(prepare_current, prefix, code='tree_preview', label='目录树预览')
            else:
                snapshot = await bounded_case_read(case_tree.local_snapshot, prefix, code='tree_preview', label='目录树预览')
            case = await authorize()
            async with reading(db, case):
                case = await authorize()
                if case.s3_prefix != prefix:
                    raise case_tree.stale_tree()
                if snapshot is None:
                    # Legacy S3 cases have no versioned tree catalog yet.
                    present, raw = await files._io(case_tree.current, prefix)
                else:
                    if storage._use_s3():
                        await bounded_case_read(_require_current, prefix, snapshot, code='tree_preview', label='目录树预览复核')
                    else:
                        await files._io(snapshot.verify)
                    present, raw = snapshot.entries, snapshot.metadata
                prepared, _ = plan(value, case, paths, present, raw)
                return prepared


async def _replayed(db, case, request_id, intent):
    previous = await db.get(DurableEvent, request_id)
    if previous is None:
        return None
    old = json.loads(previous.payload_json)
    if previous.case_id != case.id or previous.type != common.EVENT or old.get('intent') != intent:
        raise CaseWriteConflict('recovery_request_conflict', '确认标识已用于其他操作，请重新预览')
    return {**old['result'], 'replayed': True}


def _confirmed(value, case, paths, present, raw, plan_sha256):
    prepared, target = plan(value, case, paths, present, raw)
    if prepared['plan_sha256'] != plan_sha256:
        raise case_tree.stale_tree()
    if any(item['action'] == 'conflict' for item in prepared['files']):
        raise CaseWriteConflict('recovery_merge_conflict', '目录树存在内容、类型或权限冲突，请保留冲突并重新选择')
    return prepared, target


async def _receipt(db, case, intent, prepared, present, target, transaction, request_id):
    changed = [p for p in present.keys() | target.keys() if present.get(p) != target.get(p)]
    result = {'request_id': request_id, 'recovery_id': intent['recovery_id'], 'case_id': case.id,
              'semantics': SEMANTICS, 'written_files': sorted(changed),
              'unchanged_files': [e['path'] for e in prepared['files'] if e['path'] not in changed],
              'deleted_files': sorted(p for p in changed if p not in target),
              'file_transaction_id': transaction, 'directories_restored': True,
              'permissions_restored': True, 'deletions_applied': True,
              'permissions_target': prepared['permissions_target']}
    await append_event(db, case.id, common.EVENT, {'intent': intent, 'result': result}, event_id=request_id)
    return {**result, 'replayed': False}


async def _restore_local(db, case, location, authorize, value, intent, source, request_id):
    async with committing(db, case):
        case = await authorize()
        replayed = await _replayed(db, case, request_id, intent)
        if replayed is not None:
            return replayed
    snapshot = await bounded_case_read(case_tree.local_snapshot, case.s3_prefix,
        code='tree_prepare', label='目录树读取')
    prepared, target = _confirmed(value, case, intent['paths'], snapshot.entries, snapshot.metadata, intent['plan_sha256'])
    async with tree_transactions.prepare(db, case, location, snapshot, target, source) as staged:
        async with committing(db, case):
            case = await authorize()
            replayed = await _replayed(db, case, request_id, intent)
            if replayed is not None:
                return replayed
            if staged is None:
                await files._io(snapshot.verify)
            transaction = await staged.publish(db, case) if staged is not None else None
            return await _receipt(db, case, intent, prepared, snapshot.entries, target, transaction, request_id)


async def _restore_remote(authorize, value, intent, source, request_id):
    from . import case_copy_io, storage_operations, tree_transactions_s3
    from .request_deadline import SANDBOX_RECOVERY_SECONDS

    async def checked(db):
        case = await authorize(db)
        await lock_case_execution(db, case.id)
        return await authorize(db)

    code, label = 'sandbox_tree_restore', '沙箱目录恢复'
    async with file_read_deadline(code, label, seconds=SANDBOX_RECOVERY_SECONDS):
        async with SessionLocal() as db:
            case = await checked(db)
            replayed = await _replayed(db, case, request_id, intent)
            if replayed is not None:
                return replayed
        with case_copy_io.admit(code=code+'_busy', label=label):
            async with storage_operations.hold(checked, purpose='staging') as lease:
                present, raw, snapshot = await tree_transactions_s3.current_reserved(lease, code=code, label=label)
                async with lease.control() as db:
                    case = await db.get(Case, lease.case.id)
                    replayed = await _replayed(db, case, request_id, intent)
                    if replayed is not None:
                        return replayed
                prepared, target = await bounded_case_read(_confirmed, value, lease.case, intent['paths'], present, raw,
                                                            intent['plan_sha256'], code=code, label=label)

                async def finalize(db, case, transaction):
                    return await _receipt(db, case, intent, prepared, present, target, transaction, request_id)

                return await tree_transactions_s3.write_reserved(lease, present, target, source, raw, snapshot,
                                                                  finalize=finalize)


async def restore(sid, identifier, user_id, paths, plan_sha256, request_id):
    if str(uuid.UUID(request_id)) != request_id:
        raise ValueError('Invalid restore request identity')
    async with common.admitted():
        value, selected = await common.recovery.load(sid, identifier, user_id)
        intent = {'schema': 'vibeflow.sandbox-tree-restore.v1', 'session_id': sid, 'recovery_id': identifier,
                  'created_by': user_id, 'paths': sorted(paths), 'plan_sha256': plan_sha256, 'semantics': SEMANTICS}
        source = Path(selected['root'])/'files'

        async def authorize(db):
            return await common.authorized(db, sid, identifier, user_id, value['receipt'], selected)

        if storage._use_s3():
            return await _restore_remote(authorize, value, intent, source, request_id)

        async with SessionLocal() as db:
            async def authorize_local():
                return await authorize(db)

            case = await authorize_local()
            location = await files._io(tree_transactions.staging_root, case.s3_prefix)
            if location is not None:
                return await _restore_local(db, case, location, authorize_local, value, intent, source, request_id)
            async with committing(db, case):
                case = await authorize_local()
                replayed = await _replayed(db, case, request_id, intent)
                if replayed is not None:
                    return replayed
                present, raw = await files._io(case_tree.current, case.s3_prefix)
                prepared, target = _confirmed(value, case, paths, present, raw, plan_sha256)
                transaction = await tree_transactions.write(db, case, present, target, source, raw)
                return await _receipt(db, case, intent, prepared, present, target, transaction, request_id)
