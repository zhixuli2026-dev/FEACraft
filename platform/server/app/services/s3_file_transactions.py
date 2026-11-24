"""Remote write-ahead journals; recovery needs SQL and S3, never a local cache."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import uuid
import tempfile
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError

from .. import storage
from . import attempt_objects as objects, attempt_workspace as workspace
from . import file_transactions as journal, s3_case_files as live
from .case_write import CaseWriteConflict, record_write
from .durable_events import append_event, json_text
from .async_ops import finish_before_cancel
from .s3_streams import object_metadata, snapshot, snapshot_head, snapshot_from_head, HEADERS

PENDING = journal.PREFIX + '/pending.json'
FILE_MANIFEST = 1024 * 1024
MAX_MANIFEST = 64 * 1024 * 1024
MAX_TRANSACTION_BYTES = 2 * journal.MAX_BYTES
STAGING_MAX_BYTES = (1 << 63) - 1
STAGING_MAX_FILES = 100000


@dataclass(frozen=True)
class RemoteSource:
    """A same-case source frozen by size/hash before publishing a copied file."""
    relative: str
    expected: dict


@dataclass(frozen=True)
class TreeSnapshotFile:
    root: Path
    relative: str
    expected: dict

    def open_verified(self):
        return _open_verified_file(lambda: journal._path(self.root, self.relative), self.expected, self.relative)


def is_retained_source(data):
    from .staging_sync import StagingFile

    return isinstance(data, (TreeSnapshotFile, journal.RecoveryFileSource, StagingFile))


@contextmanager
def _open_verified_file(resolve, expected, relative, *, staging=False):
    from .file_read import _identity
    from .remote_write_budget import BoundedBody
    from .request_deadline import current
    from .s3_streams import _digest_body

    def changed():
        if staging:
            return journal._conflict('S3 transaction staging changed before publication', relative)
        return CaseWriteConflict('recovery_evidence_invalid', '恢复文件未通过快照校验', path=relative)

    try:
        path = resolve()
        identity = _identity(workspace.ordinary(path))
        stream = path.open('rb')
    except (OSError, ValueError) as error:
        raise changed() from error
    with stream:
        if _identity(os.fstat(stream.fileno())) != identity:
            raise changed()
        body = BoundedBody(stream)
        if body.size != expected['size'] or _digest_body(body, body.size, deadline=current()) != expected:
            raise changed()
        body.seek(0)
        yield body
        try:
            stable = (_identity(os.fstat(stream.fileno())) == identity
                      and _identity(workspace.ordinary(resolve())) == identity)
        except (OSError, ValueError) as error:
            raise changed() from error
        if not stable:
            raise changed()


def _open_retained_source(prefix, source):
    from .staging_sync import StagingFile

    if isinstance(source, TreeSnapshotFile):
        return source.open_verified()
    return _open_verified_file(lambda: source.resolve(prefix), source.expected, source.relative,
                               staging=isinstance(source, StagingFile))


def limits(scope):
    from .tree_metadata_edits import SCOPES
    if scope in SCOPES:return journal.limits(scope)
    if scope == 'sandbox_tree':
        from .sandbox_volume import MAX_ENTRIES, MAX_BYTES
        return MAX_ENTRIES+1, MAX_BYTES+8*1024*1024, 2*(MAX_BYTES+8*1024*1024), 16*1024*1024
    if scope == 'sandbox_recovery':return journal.limits(scope)
    if scope == 'staging':
        return STAGING_MAX_FILES, STAGING_MAX_BYTES, 2 * STAGING_MAX_BYTES, MAX_MANIFEST
    if scope != 'files':
        raise ValueError('Unknown file transaction scope')
    return journal.MAX_FILES, journal.MAX_BYTES, MAX_TRANSACTION_BYTES, FILE_MANIFEST


async def _io(function, *args, **kwargs):
    from .case_copy_io import submit
    # Keep owned network work alive through cancellation so the caller can
    # settle its journal. Bulk callers can use the shared bounded pool.
    return await finish_before_cancel(submit(function, *args, **kwargs))


def _reference(case):
    return SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix)


def _record(data):
    from .staging_sync import StagingFile
    if isinstance(data, (journal.FileSource, StagingFile, RemoteSource, TreeSnapshotFile)):
        return data.expected
    return None if data is None else objects.digest(data)


def _binding(case):
    return dict(objects.binding(case.s3_prefix), case_id=case.id)


def _directory(identifier):
    if str(uuid.UUID(identifier)) != identifier:
        raise ValueError('Invalid file transaction identity')
    return journal.PREFIX + '/objects/' + identifier


def pending_version(prefix):
    head = snapshot_head(prefix, PENDING, MAX_MANIFEST)
    return None if head is None else head['ETag']


def _read(prefix, path, limit=journal.MAX_BYTES):
    """Bound the HEAD-admitted body and retain its conditional mutation token."""
    try:
        info = storage.s3().head_object(**objects.arguments(prefix, path))
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] == 404:
            return None, None, None
        raise
    if not info.get('ETag') or not 0 <= info['ContentLength'] <= limit:
        raise ValueError('S3 file transaction object has no admitted size/version')
    data = objects.read(prefix, path, etag=info['ETag'], limit=info['ContentLength'])
    if len(data) != info['ContentLength']:
        raise ValueError('S3 file transaction object length changed')
    metadata = {key: info[key] for key in HEADERS if key in info}
    # S3 providers may omit an empty Metadata map on HEAD.
    if not metadata.get('Metadata'):
        metadata.pop('Metadata', None)
    return data, info['ETag'], metadata


def _blob(prefix, directory, index, side, expected, object_path=None, output=None):
    observed, _, _ = snapshot(prefix, object_path or f'{directory}/{side}/{index}', output=output, limit=expected['size'])
    if observed != expected:
        raise journal._conflict('Immutable S3 file backup changed')
    return observed


@contextmanager
def _temporary_copy(size):
    from .node_storage_budget import reserve
    with reserve(size), tempfile.TemporaryFile(dir=storage._fs_root()) as stream:
        yield stream


def _backup_before(prefix, path, backup, limit, expected_files):
    head = snapshot_head(prefix, path, limit)
    if head is None:
        journal.check_expected(path, None, expected_files)
        return None, None
    with _temporary_copy(head['ContentLength']) as stream:
        observed, _, metadata = snapshot_from_head(prefix, path, head, output=stream)
        journal.check_expected(path, observed, expected_files)
        storage.s3().put_object(**objects.arguments(prefix, backup), Body=stream, IfNoneMatch='*')
        verified, _, _ = snapshot(prefix, backup, limit=observed['size'])
        if verified != observed:
            raise journal._conflict('S3 before-image backup differs from its source', path)
        return observed, metadata


def _after_object(directory, index, path):
    return f'{directory}/after/{index}/' + PurePosixPath(path).name


def _prepare_after(prefix, path, data, metadata):
    """Freeze the metadata actually accepted by the provider before live writes.

    Some stores infer MIME for application/octet-stream. The private key keeps
    the destination basename, and the observed headers become part of the plan.
    """
    expected = _record(data)
    if isinstance(data, RemoteSource):
        if workspace.canonical_path(data.relative) != data.relative:
            raise ValueError('Invalid remote copy source')
        with _temporary_copy(expected['size']) as stream:
            observed, _, _ = snapshot(prefix, data.relative, output=stream, limit=expected['size'])
            if observed != expected:
                raise CaseWriteConflict('input_stale', 'Remote copy source changed', path=data.relative)
            storage.s3().put_object(**objects.arguments(prefix, path), Body=stream, **metadata, IfNoneMatch='*')
    elif is_retained_source(data):
        with _open_retained_source(prefix, data) as stream:
            storage.s3().put_object(**objects.arguments(prefix, path), Body=stream, **metadata, IfNoneMatch='*')
    elif isinstance(data, journal.FileSource):
        from .request_deadline import current
        deadline = current()
        if deadline is not None:
            deadline.check()
        source = data.resolve(prefix)
        if journal._capture(source, limit=expected['size'], deadline=deadline) != expected:
            raise journal._conflict('S3 transaction staging changed before publication')
        if deadline is not None:
            deadline.check()
        with source.open('rb') as stream:
            storage.s3().put_object(**objects.arguments(prefix, path), Body=stream, **metadata, IfNoneMatch='*')
    else:
        storage.s3().put_object(**objects.arguments(prefix, path), Body=data, **metadata, IfNoneMatch='*')
    observed, _, accepted = snapshot(prefix, path, limit=expected['size'])
    if observed != expected:
        raise journal._conflict('S3 transaction staging changed during publication')
    return accepted


def _validate_paths(prefix, files, scope='files'):
    from .tree_metadata_edits import internal
    names = set()
    for entry in files:
        path = entry['path']
        if workspace.canonical_path(path) != path:
            raise ValueError('Invalid transaction destination')
        if not internal(scope, path) and (scope != 'sandbox_tree' or path != '.vibeflow/tree-state.json'):
            workspace.assert_mutable_path(prefix, path)
        key = path.casefold()
        if key in names:
            raise ValueError('Conflicting transaction destinations')
        names.add(key)
    for key in names if scope != 'sandbox_tree' else ():
        parts = key.split('/')
        if any('/'.join(parts[:index]) in names for index in range(1, len(parts))):
            raise ValueError('Conflicting transaction destinations')


def _load(case, *, verify_blobs=True):
    encoded, etag, _ = _read(case.s3_prefix, PENDING, MAX_MANIFEST)
    if encoded is None:
        return None, None
    document = json.loads(encoded)
    plan = document['plan']
    directory = _directory(plan['id'])
    if (plan['schema'] != journal.SCHEMA or plan['binding'] != _binding(case)
            or str(uuid.UUID(document['event_id'])) != document['event_id']
            or hashlib.sha256(json_text(plan).encode()).hexdigest() != document['plan_sha256']):
        raise ValueError('File transaction identity, storage binding or digest changed')
    if objects.read(case.s3_prefix, directory+'/manifest.json', limit=MAX_MANIFEST) != encoded:
        raise ValueError('Pending pointer differs from its immutable file manifest')
    if 'writer' in plan:
        from .s3_staging_transactions import validate_writer
        if plan.get('scope', 'files') not in {'staging', 'files', 'files_with_modes', 'sandbox_tree',
                                              'sandbox_recovery', 'sandbox_recovery_with_modes'}:
            raise ValueError('Invalid reserved file transaction scope')
        validate_writer(plan['writer'])
    if 'manual_recovery' in plan:
        from .manual_file_recovery_s3 import validate_marker
        validate_marker(plan['manual_recovery'])
    files = plan['files']
    max_files, max_bytes, max_total, max_manifest = limits(plan.get('scope', 'files'))
    if len(encoded) > max_manifest:
        raise ValueError('File transaction manifest exceeds its scope')
    if not isinstance(files, list) or not 0 < len(files) <= max_files:
        raise ValueError('Invalid file transaction size')
    _validate_paths(case.s3_prefix, files, plan.get('scope', 'files'))
    from .tree_metadata_edits import check_records
    check_records(plan.get('scope', 'files'), files)
    total = 0
    for index, entry in enumerate(files):
        for side in ('before', 'after'):
            expected = entry[side]
            metadata = entry[side+'_metadata']
            if expected is None:
                if metadata is not None:
                    raise ValueError('Absent transaction object has metadata')
                continue
            if (not isinstance(expected, dict) or type(expected.get('size')) is not int
                    or not 0 <= expected['size'] <= max_bytes
                    or not isinstance(metadata, dict) or set(metadata) - set(HEADERS)):
                raise ValueError('Invalid file transaction object record')
            total += expected['size']
            if total > max_total:
                raise ValueError('File transaction exceeds 2 GiB including backups')
            object_path = entry.get(side+'_object')
            if object_path is not None and (side != 'after' or object_path != _after_object(directory, index, entry['path'])):
                raise ValueError('File transaction backup path differs from its owner')
            if verify_blobs:
                _blob(case.s3_prefix, directory, index, side, expected, object_path)
    return document, etag


def _matches(data, metadata, entry, side):
    return data == entry[side] and metadata == entry[side+'_metadata']


def _conditional(prefix, path, data, etag, metadata, *, expected=None):
    try:
        args = objects.arguments(prefix, path)
        if data is None:
            if etag is not None:
                storage.s3().delete_object(**args, IfMatch=etag)
        else:
            condition = {'IfMatch': etag} if etag is not None else {'IfNoneMatch': '*'}
            client = storage.s3()
            if expected is not None:
                return client.put_verified_object(**args, Body=data, **metadata, **condition, expected=expected)
            return client.put_object(**args, Body=data, **metadata, **condition)
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] == 412:
            raise journal._conflict('S3 object changed during conditional mutation; preserve it for review', path) from error
        raise


def _apply(case, directory, index, entry, side, expected_side, *, retained_source=None):
    bound = max((value['size'] for value in (entry['before'], entry['after']) if value is not None), default=0)
    data, etag, metadata = snapshot(case.s3_prefix, entry['path'], limit=bound)
    if not _matches(data, metadata, entry, expected_side):
        raise journal._conflict('S3 object changed outside the file transaction', entry['path'])
    target = entry[side]
    if retained_source is not None:
        if (side != 'after' or not is_retained_source(retained_source)
                or retained_source.relative != entry['path'] or retained_source.expected != target):
            raise ValueError('Retained source differs from the journal')
        # The persisted after-image remains independently verifiable after exit.
        _blob(case.s3_prefix, directory, index, side, target, entry.get(side+'_object'))
        with _open_retained_source(case.s3_prefix, retained_source) as stream:
            version = _conditional(case.s3_prefix, entry['path'], stream, etag, entry[side+'_metadata'],
                                   expected=target)['ETag']
        head = snapshot_head(case.s3_prefix, entry['path'], bound)
        if head is None or head['ETag'] != version or head['ContentLength'] != target['size'] or (
                object_metadata(head) != entry[side+'_metadata']):
            raise journal._conflict('S3 object changed after the conditional mutation', entry['path'])
        return
    if target is None:
        _conditional(case.s3_prefix, entry['path'], None, etag, None)
    else:
        with _temporary_copy(target['size']) as stream:
            _blob(case.s3_prefix, directory, index, side, target, entry.get(side+'_object'), stream)
            _conditional(case.s3_prefix, entry['path'], stream, etag, entry[side+'_metadata'])
    observed, _, observed_metadata = snapshot(case.s3_prefix, entry['path'], limit=bound)
    if not _matches(observed, observed_metadata, entry, side):
        raise journal._conflict('S3 object changed after the conditional mutation', entry['path'])


def _publish_pending(prefix, encoded):
    """The single pending slot is published only after all immutable objects."""
    try:
        storage.s3().put_object(**objects.arguments(prefix, PENDING), Body=encoded,
                                ContentType='application/json', IfNoneMatch='*')
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] == 412:
            raise journal._conflict('Another S3 file transaction is pending') from error
        raise
    actual, _, _ = _read(prefix, PENDING, MAX_MANIFEST)
    if actual != encoded:
        raise journal._conflict('S3 pending file transaction changed after creation')


async def prepare(case, changes, *, expected_files=None, content_types=None, scope='files'):
    """Freeze immutable before/after images; never mutate live files or SQL."""
    case = _reference(case)
    max_files, max_bytes, max_total, max_manifest = limits(scope)
    if not isinstance(changes, dict) or not 0 < len(changes) <= max_files:
        raise ValueError('Invalid file transaction size')
    if expected_files is not None and (not isinstance(expected_files, dict) or set(expected_files) - changes.keys()):
        raise ValueError('File preconditions must refer to changed paths')
    if content_types is not None and (not isinstance(content_types, dict) or set(content_types) - changes.keys()):
        raise ValueError('Content types must refer to changed paths')
    if any(not isinstance(value, str) or not value or len(value) > 1024 or '\r' in value or '\n' in value
           for value in (content_types or {}).values()):
        raise ValueError('Invalid file content type')
    _validate_paths(case.s3_prefix, [{'path': path} for path in changes], scope)
    from .staging_sync import StagingFile
    allowed = (bytes, journal.FileSource, RemoteSource)
    if scope == 'staging':
        allowed += (StagingFile,)
    if scope == 'sandbox_tree':
        allowed += (TreeSnapshotFile,)
    if any(data is not None and not isinstance(data, allowed) for data in changes.values()):
        raise ValueError('Invalid transaction bytes')
    after_records = [_record(data) for data in changes.values() if data is not None]
    if any(type(record.get('size')) is not int or not 0 <= record['size'] <= max_bytes for record in after_records):
        raise ValueError('File transaction object exceeds 1 GiB')
    if sum(record['size'] for record in after_records) > max_total:
        raise ValueError('File transaction exceeds 2 GiB including backups')
    # Never silently fall back to unconditional writes on an incompatible store.
    await _io(live.require_conditions, case.s3_prefix)
    existing, _, _ = await _io(_read, case.s3_prefix, PENDING, MAX_MANIFEST)
    if existing is not None:
        raise journal._conflict('A file transaction is already pending for this case')
    identifier = str(uuid.uuid4())
    directory = _directory(identifier)
    records, total = [], 0
    for index, (path, data) in enumerate(changes.items()):
        before, before_metadata = await _io(_backup_before, case.s3_prefix, path,
                                            f'{directory}/before/{index}', max_bytes, expected_files)
        after = _record(data)
        total += sum(item['size'] for item in (before, after) if item is not None)
        if total > max_total:
            raise ValueError('File transaction exceeds 2 GiB including backups')
        after_metadata = None if data is None else {'ContentType': (content_types or {}).get(path, 'application/octet-stream')}
        after_object = None
        if data is not None:
            after_object = _after_object(directory, index, path)
            after_metadata = await _io(_prepare_after, case.s3_prefix, after_object, data, after_metadata)
        records.append({'path': path, 'before': before, 'after': after,
                        'before_metadata': before_metadata, 'after_metadata': after_metadata,
                        'after_object': after_object})
    from .tree_metadata_edits import check_records
    check_records(scope, records)
    plan = {'schema': journal.SCHEMA, 'id': identifier, 'binding': _binding(case), 'files': records}
    if scope != 'files':
        plan['scope'] = scope
    return plan


async def write(db, case, changes, *, expected_files=None, content_types=None, scope='files'):
    from .case_write import _writes
    case = _reference(case)
    current = _writes.get()
    if current is None or current[0] != case.s3_prefix:
        raise ValueError('S3 file journal requires the case write coordinator')
    from .tree_metadata_edits import expand
    changes, expected_files, scope = await _io(expand, case.s3_prefix, changes, expected_files, scope)
    plan = await prepare(case, changes, expected_files=expected_files, content_types=content_types, scope=scope)
    from .manual_file_recovery_s3 import mark_plan
    mark_plan(plan)
    identifier, records = plan['id'], plan['files']
    directory = _directory(identifier)
    max_manifest = limits(scope)[3]
    plan_hash = hashlib.sha256(json_text(plan).encode()).hexdigest()
    async with db.begin_nested():
        event = await append_event(db, case.id, journal.EVENT, {'transaction_id': identifier, 'plan_sha256': plan_hash})
        document = {'plan': plan, 'event_id': event.id, 'plan_sha256': plan_hash}
        encoded = json_text(document).encode()
        if len(encoded) > max_manifest:
            raise ValueError('Oversized file transaction manifest')
        await _io(objects.put_new, case.s3_prefix, directory+'/manifest.json', encoded)
        await _io(_publish_pending, case.s3_prefix, encoded)
        db.info.setdefault('local_file_transactions', {})[case.id] = (db.sync_session.get_transaction(), identifier)
        for index, entry in enumerate(records):
            await _io(_apply, case, directory, index, entry, 'after', 'before')
            record_write(case.s3_prefix, entry['path'])
        from .viz_scene_relay import record_scene_commit
        await record_scene_commit(db, case.id, case.s3_prefix, records)
    return identifier


async def recover_locked(db, case):
    case = _reference(case)
    try:
        document, pending_etag = await _io(_load, case, verify_blobs=False)
        from .s3_staging_transactions import writer_state, owned_here
        if document is not None:
            if owned_here(document):
                return {'transaction_id': document['plan']['id'], 'decision': 'in_transaction'}
            state = await _io(writer_state, case, document)
            if state in {'active', 'unverified'}:
                raise CaseWriteConflict('file_sync_busy', 'S3副本正在同步或写入者停止尚未核验，请稍后重试')
        from .s3_live_store import fence
        await _io(fence, case.s3_prefix)
        document, pending_etag = await _io(_load, case)
        if document is None:
            return None
        plan = document['plan']
        directory = _directory(plan['id'])
        event = await journal._witness(db, case, document)
        owner = db.info.get('local_file_transactions', {}).get(case.id)
        if event is not None and owner == (db.sync_session.get_transaction(), plan['id']):
            return {'transaction_id': plan['id'], 'decision': 'in_transaction'}
        from .case_validation import assert_writable_locked
        await assert_writable_locked(db, case.id)
        await _io(live.require_conditions, case.s3_prefix)
        observed = []
        for entry in plan['files']:
            data, _, metadata = await _io(snapshot, case.s3_prefix, entry['path'], limit=limits(plan.get('scope', 'files'))[1])
            before, after = _matches(data, metadata, entry, 'before'), _matches(data, metadata, entry, 'after')
            if not (after if event is not None else before or after):
                raise journal._conflict('Pending S3 transaction conflicts with current bytes; nothing was overwritten', entry['path'])
            observed.append(before)
        if event is None:
            for index, (entry, before) in enumerate(zip(plan['files'], observed)):
                if not before:
                    await _io(_apply, case, directory, index, entry, 'before', 'after')
        receipt = {'transaction_id': plan['id'], 'decision': 'committed' if event else 'rolled_back',
                   'event_id': document['event_id'], 'plan_sha256': document['plan_sha256']}
        await _io(objects.put_new, case.s3_prefix, directory+'/resolution.json', json_text(receipt).encode())
        await _io(_conditional, case.s3_prefix, PENDING, None, pending_etag, None)
        return receipt
    except CaseWriteConflict:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OSError, ClientError, BotoCoreError) as error:
        raise journal._conflict('S3 file transaction cannot be verified; preserve remote objects for review') from error
