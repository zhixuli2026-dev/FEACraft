"""Publish verified terminal trees and dictionary revisions in one transaction."""
import hashlib
import json
from types import SimpleNamespace
import uuid

from .. import storage
from ..db import SessionLocal
from ..models import Case, DurableEvent
from . import case_tree, file_transactions as files, tree_transactions
from .case_write import CaseWriteConflict, assert_case_available, bounded_case_read
from .case_write_capacity import committing
from .durable_events import append_event, lock_case_execution

SCHEMA = 'vibeflow.terminal-export-published.v1'
EVENT = 'terminal.export_published'
CODEX_SCHEMA = 'vibeflow.codex-docker-case-published.v1'
PUBLISHED_EVENTS = {SCHEMA: EVENT, CODEX_SCHEMA: 'codex.docker.case_published'}


def payload(exporter, session_id, schema=SCHEMA):
    raw = (exporter.root / 'manifest.json').read_bytes()
    return {'schema': schema, 'session_id': session_id, 'request_id': exporter.request_id,
            'manifest_sha256': hashlib.sha256(raw).hexdigest()}


def prepare_dictionary(exporter, case, present, target):
    from .casedict import CASE_DICT_PATH, prepare_case_dict
    before, after = present.get(CASE_DICT_PATH), target.get(CASE_DICT_PATH)
    if before == after:
        return None
    if after is None or after['kind'] != 'file':
        raise CaseWriteConflict('export_dictionary_invalid', '项目配置不能删除或替换为目录，导出副本已保留')
    with files._path(exporter.root / 'files', CASE_DICT_PATH).open('rb') as stream:
        data = stream.read(after['size'] + 1)
    if len(data) != after['size'] or hashlib.sha256(data).hexdigest() != after['sha256']:
        raise CaseWriteConflict('export_dictionary_invalid', '项目配置未通过导出校验，副本已保留')
    try:
        prepared = prepare_case_dict(case, data.decode('utf8'), source='sandbox-export')
    except (UnicodeError, ValueError, RecursionError) as error:
        raise CaseWriteConflict('export_dictionary_invalid', '项目配置未通过语法、结构或阶段校验，导出副本已保留') from error
    for path, content in prepared.files.items():
        target[path] = {'kind': 'file', 'mode': target.get(path, present.get(path, {})).get('mode', 0o777),
                        'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    case_tree.validate(target)
    return prepared


async def _bound(db, case_id, prefix, authorize, identifier, receipt):
    case = await authorize(db)
    if case.id != case_id or case.s3_prefix != prefix:
        raise CaseWriteConflict('input_stale', '终端算例来源已变化，已保留本次副本')
    return case, await _previous(db, case_id, identifier, receipt)


async def _previous(db, case_id, identifier, receipt):
    previous = await db.get(DurableEvent, identifier)
    if previous is None:
        return None
    saved = json.loads(previous.payload_json)
    if (previous.case_id != case_id or previous.type != PUBLISHED_EVENTS[receipt['schema']]
            or any(saved.get(key) != value for key, value in receipt.items())):
        raise CaseWriteConflict('export_receipt_conflict', '导出回执与本次副本不一致')
    return {**saved, 'event_id': identifier}


def _target(case, exporter, initial, after, names, present):
    target, actions = (case_tree.merge(case.s3_prefix, initial, after, present, names, allow_dictionary=True)
                       if names else (dict(present), []))
    if any(item['action'] == 'conflict' for item in actions):
        raise CaseWriteConflict('export_merge_conflict', '项目已发生并行修改，沙箱导出副本已保留供恢复')
    return target, prepare_dictionary(exporter, case, present, target)


async def _record(db, case, prepared, receipt, identifier):
    if prepared is not None:
        from .casedict import _apply_dict_mirror
        _apply_dict_mirror(case, prepared)
        await db.flush()
    await append_event(db, case.id, PUBLISHED_EVENTS[receipt['schema']], receipt, event_id=identifier)
    return {**receipt, 'event_id': identifier}


async def _local_fallback_publish(db, case, exporter, initial, after, names, receipt, identifier):
    present, raw = await files._io(case_tree.current, case.s3_prefix)
    target, prepared = _target(case, exporter, initial, after, names, present)
    await tree_transactions.write(db, case, present, target, exporter.root / 'files', raw,
                                  prepared_dictionary=prepared)
    return await _record(db, case, prepared, receipt, identifier)


async def _prepared_local_publish(db, case, location, exporter, case_id, prefix, authorize, initial, after, names,
                                  receipt, identifier):
    async with committing(db, case):
        case, previous = await _bound(db, case_id, prefix, authorize, identifier, receipt)
        if previous is not None:
            return previous
        revision, state = case.dict_revision, case.workflow_state
    snapshot = await bounded_case_read(case_tree.local_snapshot, prefix,
        code='sandbox_publication', label='沙箱发布源读取')
    target, prepared = await bounded_case_read(_target, case, exporter, initial, after, names, snapshot.entries,
        code='sandbox_publication', label='沙箱发布校验')
    async with tree_transactions.prepare(db, case, location, snapshot, target, exporter.root / 'files',
                                         prepared_dictionary=prepared) as staged:
        async with committing(db, case):
            case, previous = await _bound(db, case_id, prefix, authorize, identifier, receipt)
            if previous is not None:
                return previous
            if prepared is not None and (case.dict_revision, case.workflow_state) != (revision, state):
                raise case_tree.stale_tree()
            if staged is not None:
                await staged.publish(db, case)
            else:
                await files._io(snapshot.verify)
            return await _record(db, case, prepared, receipt, identifier)


async def _prepared_s3_publish(exporter, case_id, prefix, authorize, initial, after, names, receipt, identifier):
    from . import case_copy_io, storage_operations, tree_transactions_s3
    from .case_read_capacity import file_read_deadline
    from .request_deadline import STAGING_CLEANUP_SECONDS

    async def checked(db):
        await lock_case_execution(db, case_id)
        case = await authorize(db)
        assert_case_available(case)
        if case.id != case_id or case.s3_prefix != prefix:
            raise CaseWriteConflict('input_stale', '终端算例来源已变化，已保留本次副本')
        return case

    code, label = 'sandbox_publication', '沙箱发布'
    async with file_read_deadline(code, label, seconds=STAGING_CLEANUP_SECONDS):
        async with SessionLocal() as db:
            await checked(db)
            previous = await _previous(db, case_id, identifier, receipt)
            if previous is not None:
                await db.commit()
                return previous
        with case_copy_io.admit(code=code+'_busy', label=label):
            async with storage_operations.hold(checked, purpose='staging') as lease:
                present, raw, snapshot = await tree_transactions_s3.current_reserved(lease, code=code, label=label)
                async with lease.control() as db:
                    previous = await _previous(db, case_id, identifier, receipt)
                    if previous is not None:
                        return previous
                    case = await db.get(Case, case_id)
                    reference = SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix,
                                                dict_revision=case.dict_revision, workflow_state=case.workflow_state)
                target, prepared = await bounded_case_read(_target, reference, exporter, initial, after, names, present,
                                                           code=code, label=label)

                async def finalize(db, case, _transaction):
                    if prepared is not None and (case.dict_revision, case.workflow_state) != (
                            reference.dict_revision, reference.workflow_state):
                        raise case_tree.stale_tree()
                    return await _record(db, case, prepared, receipt, identifier)

                return await tree_transactions_s3.write_reserved(lease, present, target, exporter.root / 'files', raw,
                    snapshot, prepared_dictionary=prepared, finalize=finalize)


async def publish(exporter, session_id, case_id, prefix, authorize):
    if exporter.manifest is None:
        raise ValueError('Sandbox has not completed its verified export')
    initial = case_tree.from_manifest(exporter.initial)
    after = case_tree.from_manifest(exporter.manifest)
    return await publish_tree(exporter, session_id, case_id, prefix, authorize, initial, after)


async def publish_tree(exporter, session_id, case_id, prefix, authorize, initial, after, *, schema=SCHEMA):
    names = sorted(p for p in initial.keys() | after.keys() if initial.get(p) != after.get(p))
    receipt = payload(exporter, session_id, schema)
    identifier = str(uuid.uuid5(uuid.UUID(session_id), schema + ':' + exporter.request_id))
    if storage._use_s3():
        return await _prepared_s3_publish(exporter, case_id, prefix, authorize, initial, after, names, receipt, identifier)
    async with SessionLocal() as db:
        case = await db.get(Case, case_id)
        assert_case_available(case)
        location = await files._io(tree_transactions.staging_root, prefix)
        if location is not None:
            return await _prepared_local_publish(db, case, location, exporter, case_id, prefix, authorize,
                                                 initial, after, names, receipt, identifier)
        async with committing(db, case):
            case, previous = await _bound(db, case_id, prefix, authorize, identifier, receipt)
            if previous is not None:
                return previous
            return await _local_fallback_publish(db, case, exporter, initial, after, names, receipt, identifier)
