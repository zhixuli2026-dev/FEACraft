"""Read-only file-journal evidence. Verification of bodies precedes SQL control."""
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError

from .. import storage
from . import file_transactions as journal
from .request_deadline import current


INVALID = (ValueError, KeyError, TypeError, AttributeError, OSError)
CODE = 'file_transaction_status'
LABEL = '文件事务诊断'


def _local_identity(path):
    from .file_read import _identity

    try:
        return _identity(path.lstat())
    except FileNotFoundError:
        return None


def _local_token(base):
    if not base.exists():
        return None
    pending = journal._path(base, 'pending')
    if not pending.exists():
        return None
    manifest = _local_identity(journal._path(pending, 'manifest.json'))
    return manifest or ('incomplete' if pending.exists() else None)


def markers(case, backend):
    if backend == 's3':
        from .s3_file_transactions import pending_version
        return pending_version(case.s3_prefix)
    from . import tree_transactions

    if not storage._fs_path(case.s3_prefix, '.').exists():
        return None, None
    return _local_token(tree_transactions._base(case)), _local_token(journal._base(case))


@dataclass(frozen=True)
class Inspection:
    status: dict
    marker: object
    document: dict | None = None
    matches: tuple = ()
    tree: object = None


def _invalid(backend):
    return {'backend': backend, 'status': 'journal_invalid', 'code': 'file_recovery_required'}


def _remote(case, marker):
    from . import s3_file_transactions as remote
    from .s3_staging_transactions import writer_state

    document, etag = remote._load(case, verify_blobs=False)
    if document is None or etag != marker:
        raise journal._conflict('Pending S3 journal changed while inspecting it')
    state = writer_state(case, document)
    identifier = document['plan']['id']
    if state in {'active', 'unverified'}:
        return Inspection({'backend': 's3', 'status': 'busy', 'code': 'file_sync_busy',
                           'transaction_id': identifier, 'writer_state': state}, marker)
    verified, etag = remote._load(case)
    if verified != document or etag != marker:
        raise journal._conflict('Pending S3 journal changed while inspecting it')
    bound = remote.limits(document['plan'].get('scope', 'files'))[1]
    matches = []
    for entry in document['plan']['files']:
        record, etag, metadata = remote.snapshot(case.s3_prefix, entry['path'], limit=bound)
        matches.append((entry['path'], remote._matches(record, metadata, entry, 'before'),
                        remote._matches(record, metadata, entry, 'after'), etag))
    return Inspection({'backend': 's3', 'transaction_id': identifier}, marker, document, tuple(matches))


def _local(case, marker):
    from . import tree_transactions

    if marker[0] is not None:
        tree = tree_transactions.observe_recovery(case)
        if tree is None:
            raise journal._conflict('Pending tree journal changed while inspecting it')
        return Inspection({'backend': 'fs', 'kind': 'tree', 'transaction_id': tree.document['plan']['id']},
                          marker, tree.document, tree=tree)
    root = storage._fs_path(case.s3_prefix, '.')
    deadline = current()
    document = journal._load(case, journal._path(journal._base(case), 'pending'), deadline=deadline)
    matches = []
    for entry in document['plan']['files']:
        path = journal._path(root, entry['path'])
        version = _local_identity(path)
        record = journal._capture(path, deadline=deadline)
        matches.append((entry['path'], record == entry['before'], record == entry['after'], version))
    return Inspection({'backend': 'fs', 'transaction_id': document['plan']['id']}, marker, document, tuple(matches))


def observe(case):
    backend = 's3' if storage._use_s3() else 'fs'
    marker = None
    try:
        marker = markers(case, backend)
        if backend == 's3' and marker is not None:
            return _remote(case, marker)
        if backend == 'fs' and any(marker):
            return _local(case, marker)
        return Inspection({'backend': backend, 'status': 'idle'}, marker)
    except TimeoutError:
        raise
    except (ClientError, BotoCoreError):
        return Inspection({'backend': backend, 'status': 'unavailable', 'code': 'storage_unavailable'}, marker)
    except INVALID:
        return Inspection(_invalid(backend), marker)


async def read_witness(db, case, observed):
    if observed.document is None:
        return False
    try:
        return await journal._witness(db, case, observed.document) is not None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _live_versions(case, observed):
    if observed.status['backend'] == 's3':
        from . import s3_file_transactions as remote

        bound = remote.limits(observed.document['plan'].get('scope', 'files'))[1]
        for path, _, _, etag in observed.matches:
            deadline = current()
            if deadline is not None:
                deadline.check()
            head = remote.snapshot_head(case.s3_prefix, path, bound)
            if (None if head is None else head['ETag']) != etag:
                raise journal._conflict('A live S3 object changed during diagnosis', path)
    else:
        root = storage._fs_path(case.s3_prefix, '.')
        for path, _, _, version in observed.matches:
            if _local_identity(journal._path(root, path)) != version:
                raise journal._conflict('A live file changed during diagnosis', path)


def confirm(case, observed, committed):
    backend = observed.status['backend']
    if observed.status.get('status') in {'unavailable', 'journal_invalid'}:
        return observed.status
    try:
        if markers(case, backend) != observed.marker:
            raise journal._conflict('The pending file transaction changed during diagnosis')
        if observed.document is None:
            return observed.status
        if committed is None:
            return _invalid(backend)
        if observed.tree is not None:
            from . import tree_transactions

            pending = journal._path(tree_transactions._base(case), 'pending')
            observed.tree.verify(case, pending, committed=committed)
            return {**observed.status, 'status': 'pending', 'database_committed': committed}
        _live_versions(case, observed)
        files = [{'path': path, 'state': 'after' if after else 'before' if before else 'external',
                  'conflict': not after if committed else not (before or after)}
                 for path, before, after, _ in observed.matches]
        return {**observed.status, 'status': 'conflict' if any(item['conflict'] for item in files) else 'pending',
                'database_committed': committed, 'files': files}
    except TimeoutError:
        raise
    except (ClientError, BotoCoreError):
        return {'backend': backend, 'status': 'unavailable', 'code': 'storage_unavailable'}
    except INVALID:
        return _invalid(backend)
