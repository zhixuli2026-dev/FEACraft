"""Keep tracked Linux modes attached to bytes changed by a file transaction."""
import hashlib
import json

from .. import storage
from . import tree_metadata_native as metadata
from .case_write import CaseWriteConflict

SCOPES = {'files_with_modes': 'files', 'sandbox_recovery_with_modes': 'sandbox_recovery'}


def internal(scope, path):
    return scope in SCOPES and path == metadata.PATH


def check_records(scope, records):
    if scope not in SCOPES:
        return
    from .file_transactions import limits
    count, bound, total_bound, _ = limits(SCOPES[scope])
    user = [r for r in records if r['path'] != metadata.PATH]
    private = [r for r in records if r['path'] == metadata.PATH]
    if len(private) != 1 or not 0 < len(user) <= count:
        raise ValueError('Mode metadata requires a bounded ordinary file transaction')
    if any(r[s] is not None and r[s]['size'] > bound for r in user for s in ('before', 'after')):
        raise ValueError('Ordinary file bound exceeded')
    if sum(r[s]['size'] for r in user for s in ('before', 'after') if r[s] is not None) > total_bound:
        raise ValueError('Ordinary transaction bound exceeded')
    if any(private[0][s] is not None and private[0][s]['size'] > metadata.MAX_BYTES for s in ('before', 'after')):
        raise ValueError('Mode metadata exceeds its bound')


def expand(prefix, changes, expected_files, scope):
    """Under case coordination or its storage reservation, join modes to the journal."""
    if scope not in SCOPES.values():
        return changes, expected_files, scope
    from . import file_transactions as files
    from .attempt_workspace import assert_mutable_path
    count, bound, _, _ = files.limits(scope)
    if not isinstance(changes, dict) or not 0 < len(changes) <= count:
        raise ValueError('Invalid file transaction size')
    if expected_files is not None and (not isinstance(expected_files, dict) or set(expected_files)-changes.keys()):
        raise ValueError('File transaction preconditions must refer to changed paths')
    for path in changes:
        assert_mutable_path(prefix, path)
    if not storage.exists(prefix, metadata.PATH):
        return changes, expected_files, scope
    if storage.file_size(prefix, metadata.PATH) > metadata.MAX_BYTES:
        raise ValueError('Oversized tree metadata')
    raw = storage.get_bytes(prefix, metadata.PATH)
    document = metadata.decode(raw)
    entries = document['entries']
    expected = dict(expected_files or {})
    updated = False
    for path, data in changes.items():
        old = entries.get(path)
        if old is None:
            continue
        if old['kind'] == 'directory':
            raise CaseWriteConflict('file_conflict', '目录替换请使用目录树操作', path=path)
        if storage._use_s3():
            from .s3_streams import snapshot
            before, _, _ = snapshot(prefix, path, limit=bound)
        else:
            from .request_deadline import current
            before = files._capture(files._path(storage._fs_path(prefix, '.'), path),
                                    limit=bound, deadline=current())
        files.check_expected(path, before, expected)
        expected[path] = before
        if data is None or before != {k: old[k] for k in ('size', 'sha256')}:
            # Deleted or externally replaced files cannot resurrect an old mode.
            del entries[path]
        else:
            if isinstance(data, bytes):
                after = {'size':len(data), 'sha256':hashlib.sha256(data).hexdigest()}
            elif isinstance(data, files.FileSource):
                after = data.expected
            else:
                from .s3_file_transactions import RemoteSource
                if not isinstance(data, RemoteSource):
                    raise ValueError('Invalid file transaction source')
                after = data.expected
            if after['size'] > bound:
                raise ValueError('File transaction object exceeds its bound')
            entries[path] = {**old, **after}
        updated = updated or entries.get(path) != old
    if not updated:
        return changes, expected_files, scope
    metadata.validate(document)
    encoded = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
    if len(encoded) > metadata.MAX_BYTES:
        raise ValueError('Oversized tree metadata')
    expected[metadata.PATH] = {'size':len(raw), 'sha256':hashlib.sha256(raw).hexdigest()}
    return {**changes, metadata.PATH:encoded}, expected, next(k for k,v in SCOPES.items() if v==scope)
