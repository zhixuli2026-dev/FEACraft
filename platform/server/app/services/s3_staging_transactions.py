"""S3 staging transfers outside SQL transactions, with durable writer fencing.

The pending journal excludes other coordinated content writers while transfers
run. A live or unverifiable writer must never be rolled back. Recovery admits a
matching drain receipt or an OS-verified exited writer, never a lease timeout.
"""
from contextvars import ContextVar
import hashlib
import json
import os
import uuid

from . import s3_file_transactions as remote, process_identity_native as identity
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, record_write
from .durable_events import append_event, json_text

SCHEMA = 'vibeflow.staging-writer.v1'
_owner = ContextVar('s3_staging_transaction_owner', default=None)


def validate_writer(writer):
    if (not isinstance(writer, dict) or set(writer) != {'schema', 'process'}
            or writer['schema'] != SCHEMA):
        raise ValueError('Invalid staging transaction writer')
    identity.validate(writer['process'])


def _key(document):
    return document['plan']['id'], document['plan_sha256']


def owned_here(document):
    return 'writer' in document['plan'] and _owner.get() == _key(document)


def _receipt(document):
    return {'schema': SCHEMA, 'transaction_id': document['plan']['id'],
            'plan_sha256': document['plan_sha256'], 'event_id': document['event_id']}


def writer_state(case, document):
    writer = document['plan'].get('writer')
    if writer is None:
        return 'ordinary'
    validate_writer(writer)
    path = remote._directory(document['plan']['id']) + '/drained.json'
    data, _, _ = remote._read(case.s3_prefix, path, 16384)
    if data is not None:
        if json.loads(data) != _receipt(document):
            raise ValueError('Staging drain receipt differs from its transaction')
        return 'drained'
    observation = identity.probe(writer['process'])
    if observation['state'] == 'exited':
        return 'exited'
    if observation['state'] == 'match':
        return 'active'
    return 'unverified'


async def prepare(case, changes, expected, types, *, scope='staging'):
    plan = await remote.prepare(case, changes, expected_files=expected, content_types=types, scope=scope)
    plan['writer'] = {'schema': SCHEMA, 'process': identity.observe(os.getpid())}
    document = {'plan': plan, 'event_id': str(uuid.uuid4()),
                'plan_sha256': hashlib.sha256(json_text(plan).encode()).hexdigest()}
    if len(json_text(document).encode()) > remote.limits(scope)[3]:
        raise ValueError('Oversized staging transaction manifest')
    return document


async def publish_locked(case, document):
    """Caller owns case control; only the bounded manifest is written here."""
    from .case_write import _writes
    current = _writes.get()
    if current is None or current[0] != case.s3_prefix or document['plan']['binding'] != remote._binding(case):
        raise ValueError('Staging journal requires its case write coordinator')
    encoded = json_text(document).encode()
    directory = remote._directory(document['plan']['id'])
    await remote._io(remote.objects.put_new, case.s3_prefix, directory + '/manifest.json', encoded)
    await remote._io(remote._publish_pending, case.s3_prefix, encoded)


async def publish_reserved(lease,document):
    from .storage_operations import require_context
    require_context(lease)
    if (document['plan']['binding']!=remote._binding(lease.case)
            or lease.document_key!=(document['plan']['id'],document['plan_sha256'],document['event_id'])):
        raise ValueError('Pending journal differs from its SQL reservation')
    encoded=json_text(document).encode()
    directory=remote._directory(document['plan']['id'])
    lease.resolved=False
    await remote._io(remote.objects.put_new,lease.case.s3_prefix,directory+'/manifest.json',encoded)
    await remote._io(remote._publish_pending,lease.case.s3_prefix,encoded)


async def apply(case, document, checkpoint=None, *, retained_sources=None):
    """All live-object data transfers occur without an open SQL transaction."""
    directory = remote._directory(document['plan']['id'])
    for index, entry in enumerate(document['plan']['files']):
        if checkpoint is not None:
            await checkpoint()
        source = retained_sources.get(entry['path']) if retained_sources is not None else None
        options = {'retained_source': source} if source is not None else {}
        await remote._io(remote._apply, case, directory, index, entry, 'after', 'before', **options)


async def commit_locked(db, case, document):
    from .case_write import _writes
    current = _writes.get()
    if current is None or current[0] != case.s3_prefix or not owned_here(document):
        raise ValueError('Staging commit requires its case coordinator and writer')
    plan = document['plan']
    if plan['binding'] != remote._binding(case):
        raise CaseWriteConflict('file_conflict', 'Staging source binding changed before commit')
    await append_event(db, case.id, remote.journal.EVENT,
                       {'transaction_id': plan['id'], 'plan_sha256': document['plan_sha256']},
                       event_id=document['event_id'])
    db.info.setdefault('local_file_transactions', {})[case.id] = (db.sync_session.get_transaction(), plan['id'])
    for entry in plan['files']:
        record_write(case.s3_prefix, entry['path'])
    from .viz_scene_relay import record_scene_commit
    await record_scene_commit(db, case.id, case.s3_prefix, plan['files'])


async def drain(case, document):
    """Call only after all transfer threads and SQL commit/rollback have settled."""
    path = remote._directory(document['plan']['id']) + '/drained.json'
    await finish_before_cancel(remote._io(remote.objects.put_new, case.s3_prefix, path,
                                         json_text(_receipt(document)).encode()))


async def write_reserved(lease, changes, expected, types=None, *, scope='staging', verify=None, finalize=None,
                         expected_revision=None, on_commit=None):
    """Prepare and apply under the durable reservation, then commit the SQL witness."""
    from ..models import Case
    from .alias_invalidation import prepare as prepare_aliases
    from .case_write import coordinate
    from .request_deadline import STAGING_CLEANUP_SECONDS, current, scope as deadline_scope
    from .s3_storage_recovery import recover_reserved
    from .tree_metadata_edits import expand

    aliases = await prepare_aliases(lease)
    changes, expected, scope = await remote._io(expand, lease.case.s3_prefix, changes, expected, scope)
    document = await prepare(lease.case, changes, expected, types, scope=scope)
    await lease.bind(document)
    committed = False

    def confirm_commit():
        nonlocal committed
        committed = True
        if on_commit is not None:
            on_commit()

    try:
        await publish_reserved(lease, document)
        retained = {path: data for path, data in changes.items() if remote.is_retained_source(data)}
        if retained:
            await apply(lease.case, document, lease.checkpoint, retained_sources=retained)
        else:
            await apply(lease.case, document, lease.checkpoint)
        if verify is not None:
            await remote._io(verify)
        deadline = current()
        if deadline is not None:
            deadline.check()
        marker = _owner.set(_key(document))
        try:
            async with lease.control(on_commit=confirm_commit) as db:
                case = await db.get(Case, lease.case.id)
                async with coordinate(db, case, expected_revision=expected_revision, prepared_aliases=aliases):
                    await commit_locked(db, case, document)
                    if finalize is not None:
                        return await finalize(db, case, document['plan']['id'])
        finally:
            _owner.reset(marker)
    finally:
        async def settle():
            lease.transfers_drained = True
            # Settlement may finish an existing operation after the admission
            # deadline. It cannot apply or commit a new business write.
            async with deadline_scope(STAGING_CLEANUP_SECONDS, inherit=False):
                if committed:
                    # The same lease has drained its acknowledged transfers and
                    # observed the commit. Revalidate its SQL/S3 witnesses before
                    # resolving the journal, using the already-established fence.
                    await recover_reserved(lease, committed_document=document)
                    return
                try:
                    await drain(lease.case, document)
                except Exception:
                    # The exact durable reservation can independently prove drain.
                    pass
                await recover_reserved(lease)
        await finish_before_cancel(settle())
