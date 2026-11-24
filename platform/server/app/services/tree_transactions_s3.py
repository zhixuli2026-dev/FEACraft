"""S3 tree recovery through the existing conditional file journal."""
from dataclasses import dataclass
import hashlib
from pathlib import Path

from botocore.exceptions import ClientError

from .. import storage
from ..config import get_settings

from . import case_tree, file_transactions as files, s3_file_transactions as remote, tree_metadata_native as metadata
from .case_write import CaseWriteConflict, bounded_case_read
from .request_deadline import current as current_deadline
from .s3_live_store import Client, generation, visible


@dataclass(frozen=True)
class PreparedTree:
    prefix: str
    version: tuple
    entries: dict
    metadata: bytes | None


def _tree_version(snapshot):
    if snapshot is None:
        return None
    members = {path: entry for path, entry in snapshot.value['files'].items()
               if visible(entry) and (path == metadata.PATH or path.split('/')[0].casefold() != '.vibeflow')}
    # The journal may fence the logical manifest without changing any file.
    return snapshot.bucket, snapshot.prefix, generation(snapshot.value), members


def _catalog(client, prefix):
    return client.public_snapshot(get_settings().s3_bucket, prefix) if isinstance(client, Client) else None


def _stale():
    return CaseWriteConflict('recovery_plan_stale', '项目目录树在预览后变化，请重新预览')


def prepare_current(prefix):
    """Authenticate all ordinary file bodies without a SQL transaction."""
    client = storage.s3()
    catalog = _catalog(client, prefix)
    if catalog is None:
        return None
    version = _tree_version(catalog)
    members = version[3]
    state = members.get(metadata.PATH)
    if state is not None and state['size'] > metadata.MAX_BYTES:
        raise ValueError('Tree metadata exceeds its byte bound')
    raw = client.read_snapshot(catalog, metadata.PATH) if state is not None else None
    sizes = {path: entry['size'] for path, entry in members.items() if path != metadata.PATH}
    entries = case_tree.remote_entries(sizes, lambda path, _: client.digest_snapshot(catalog, path), raw)
    if _tree_version(_catalog(client, prefix)) != version:
        raise _stale()
    return PreparedTree(prefix, version, entries, raw)


def _require_current(prefix, prepared):
    client = storage.s3()
    if prepared.prefix != prefix or _tree_version(_catalog(client, prefix)) != prepared.version:
        raise _stale()
    bucket, _, _, members = prepared.version
    deadline = current_deadline()
    for entry in members.values():
        if deadline is not None:
            deadline.check()
        try:
            head = client.raw.head_object(Bucket=bucket, Key=entry['key'])
        except ClientError as error:
            if error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') in {404, 412}:
                raise _stale() from error
            raise
        if head.get('ETag') != entry['physical_etag'] or head['ContentLength'] != entry['size']:
            raise _stale()
    if _tree_version(_catalog(client, prefix)) != prepared.version:
        raise _stale()


async def write(db, case, present, target, snapshot_root, expected_metadata, *, prepared_dictionary=None,
                prepared_tree=None):
    from .tree_transactions import dictionary_files
    generated = dictionary_files(prepared_dictionary, target)
    if prepared_tree is None:
        observed, raw = await files._io(case_tree.current, case.s3_prefix)
        if observed != present or raw != expected_metadata:
            raise _stale()
    else:
        if prepared_tree.entries != present or prepared_tree.metadata != expected_metadata:
            raise _stale()
        await bounded_case_read(_require_current, case.s3_prefix, prepared_tree,
            code='sandbox_publication', label='沙箱发布源复核')
    changed = sorted(p for p in present.keys() | target.keys() if present.get(p) != target.get(p))
    if not changed:return None
    changes, expected = await files._io(_changes, case.s3_prefix, present, target, Path(snapshot_root),
                                        expected_metadata, generated, changed)
    return await remote.write(db, case, changes, expected_files=expected, scope='sandbox_tree')


async def current_reserved(lease, *, code, label):
    from .s3_storage_recovery import recover_reserved

    await recover_reserved(lease)
    snapshot = await bounded_case_read(prepare_current, lease.case.s3_prefix, code=code, label=label)
    if snapshot is not None:
        return snapshot.entries, snapshot.metadata, snapshot
    entries, raw = await remote._io(case_tree.current, lease.case.s3_prefix)
    return entries, raw, None


async def write_reserved(lease, present, target, snapshot_root, expected_metadata, prepared_tree, *,
                         prepared_dictionary=None, finalize):
    from .s3_staging_transactions import write_reserved as transfer
    from .tree_transactions import dictionary_files

    prefix = lease.case.s3_prefix
    if prepared_tree is None:
        fresh, raw = await remote._io(case_tree.current, prefix)
        if fresh != present or raw != expected_metadata:
            raise _stale()
    else:
        if prepared_tree.entries != present or prepared_tree.metadata != expected_metadata:
            raise _stale()
        await remote._io(_require_current, prefix, prepared_tree)
    changed = sorted(path for path in present.keys() | target.keys() if present.get(path) != target.get(path))
    if not changed:
        from ..models import Case
        async with lease.control() as db:
            return await finalize(db, await db.get(Case, lease.case.id), None)
    generated = dictionary_files(prepared_dictionary, target)
    changes, expected = await remote._io(_changes, prefix, present, target, Path(snapshot_root),
                                         expected_metadata, generated, changed)
    return await transfer(lease, changes, expected, scope='sandbox_tree', finalize=finalize)


def _changes(prefix, present, target, snapshot_root, raw, generated, changed):
    """Verify every selected source before creating any remote journal objects."""
    changes, expected = {}, {}
    for path in changed:
        if case_tree.protected(prefix, path) and path not in generated:
            raise CaseWriteConflict('recovery_protected_path', '项目配置及版本历史必须走专用流程', path=path)
        before, after = present.get(path, {}), target.get(path, {})
        old = {k: before[k] for k in ('size', 'sha256')} if before.get('kind') == 'file' else None
        new = {k: after[k] for k in ('size', 'sha256')} if after.get('kind') == 'file' else None
        if old == new:continue
        expected[path] = old
        if new is None:
            changes[path] = None
        elif path in generated:
            # dictionary_files verified these bytes against the same target.
            changes[path] = generated[path]
        else:
            source = remote.TreeSnapshotFile(snapshot_root, path, new)
            with source.open_verified():
                changes[path] = source
    changes[metadata.PATH] = case_tree.encoded(target)
    expected[metadata.PATH] = None if raw is None else {'size':len(raw), 'sha256':hashlib.sha256(raw).hexdigest()}
    return changes, expected
