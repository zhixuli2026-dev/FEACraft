"""Bounded complete trees for sandbox three-way recovery and portable modes."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import time
from dataclasses import dataclass

from .. import storage
from ..storage_paths import contains_file_stage, is_file_stage
from . import file_transactions as files, sandbox_volume, tree_metadata_native as metadata
from .case_write import CaseWriteConflict, is_case_dictionary
from .request_deadline import current as current_deadline


def protected(prefix, path):
    return is_case_dictionary(prefix, path) or path in {'0-caseDict/case-events.jsonl', '0-caseDict/provenance.jsonl'}


def from_manifest(manifest):
    if 'modes' not in manifest:
        raise CaseWriteConflict('recovery_baseline_missing', '初始快照没有完整权限信息，不能恢复目录树')
    result = {p: {'kind': 'directory', 'mode': manifest['modes'][p]} for p in manifest['directories']}
    result.update({p: {'kind': 'file', 'mode': manifest['modes'][p], **v} for p, v in manifest['files'].items()})
    validate(result)
    return result


def validate(entries):
    metadata.validate({'schema': metadata.SCHEMA, 'materialize_directories': False, 'entries': entries})
    if sum(e.get('size', 0) for e in entries.values()) > sandbox_volume.MAX_BYTES:
        raise ValueError('Tree exceeds sandbox volume byte bound')
    return entries


def physical(root, relative=''):
    """Snapshot an ordinary local subtree, including empty dirs and modes."""
    if contains_file_stage(relative):
        return None
    root = Path(root)
    start = files._path(root, relative) if relative else root
    if not start.exists():
        return None
    entries, size = {}, 0
    deadline = time.monotonic()+30
    request = current_deadline()

    def visit(path, name):
        nonlocal size
        if request is not None:
            request.check()
        if len(entries) >= sandbox_volume.MAX_ENTRIES or time.monotonic() >= deadline or name.count('/') > 32:
            raise ValueError('Tree snapshot exceeds its bound')
        info = metadata.ordinary(path)
        record = {'kind': 'directory' if stat.S_ISDIR(info.st_mode) else 'file', 'mode': stat.S_IMODE(info.st_mode)}
        if record['kind'] == 'file':
            value = files._capture(path, limit=sandbox_volume.MAX_BYTES-size, deadline=request)
            record.update(value); size += value['size']
        entries[name] = record
        if record['kind'] == 'directory':
            for child in sorted(path.iterdir()):
                if is_file_stage(child.name):
                    continue
                metadata.name(child.name)
                visit(child, name+'/'+child.name if name else child.name)
    visit(start, '')
    return entries


def versions(root, *, public=False):
    """Stat-only identities, including directory names and native permissions."""
    entries = {}
    request = current_deadline()

    def visit(path, name):
        if request is not None:
            request.check()
        if len(entries) > sandbox_volume.MAX_ENTRIES or name.count('/') > 32:
            raise ValueError('Tree version inventory exceeds its bound')
        info = metadata.ordinary(path)
        entries[name] = (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                         info.st_mtime_ns, info.st_ctime_ns)
        if stat.S_ISDIR(info.st_mode):
            for child in path.iterdir():
                if is_file_stage(child.name):
                    continue
                metadata.name(child.name)
                visit(child, name+'/'+child.name if name else child.name)

    try:
        if not public:
            if not root.exists():
                return None
            visit(root, '')
        else:
            metadata.ordinary(root)
            for path in root.iterdir():
                if path.name.casefold() != '.vibeflow' and not is_file_stage(path.name):
                    metadata.name(path.name)
                    visit(path, path.name)
            state = files._path(root, metadata.PATH)
            if state.exists():
                visit(state, metadata.PATH)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise stale_tree() from error
    return entries


def stale_tree():
    return CaseWriteConflict('recovery_plan_stale', '项目目录树在预览后变化，请重新预览')


@dataclass(frozen=True)
class LocalSnapshot:
    prefix: str
    entries: dict
    metadata: bytes | None
    versions: dict

    def verify(self):
        if versions(storage._fs_path(self.prefix, '.'), public=True) != self.versions:
            raise stale_tree()


def local_snapshot(prefix):
    root = storage._fs_path(prefix, '.')
    observed = versions(root, public=True)
    entries, raw = current(prefix)
    snapshot = LocalSnapshot(prefix, entries, raw, observed)
    snapshot.verify()
    return snapshot


def remote_entries(sizes, read, raw):
    saved = metadata.decode(raw)['entries'] if raw is not None else {}
    if len(sizes) > sandbox_volume.MAX_ENTRIES or sum(sizes.values()) > sandbox_volume.MAX_BYTES:
        raise ValueError('Tree exceeds sandbox volume bounds')
    entries = {}
    for path, size in sorted(sizes.items()):
        metadata.name(path)
        if path in entries:
            raise ValueError('S3 contains a file/directory collision')
        entries[path] = {'kind': 'file', 'mode': 0o777, 'size': size}
        for parent in PurePosixPath(path).parents:
            if str(parent) != '.':
                previous = entries.setdefault(str(parent), {'kind': 'directory', 'mode': 0o777})
                if previous['kind'] != 'directory':
                    raise ValueError('S3 contains a file/directory collision')
        if len(entries) > sandbox_volume.MAX_ENTRIES:
            raise ValueError('Tree entry bound exceeded')
    for path, entry in saved.items():
        if entry['kind'] == 'directory':
            entries.setdefault(path, dict(entry))
    if len(entries) > sandbox_volume.MAX_ENTRIES:
        raise ValueError('Tree entry bound exceeded')
    for path, size in sizes.items():
        record = read(path, size)
        if record is None or record['size'] != size:
            raise stale_tree()
        entries[path].update(record)
    _restore_saved_modes(entries, saved)
    return validate(entries)


def _restore_saved_modes(entries, saved):
    for path, entry in entries.items():
        old = saved.get(path)
        if old and all(entry.get(k) == old.get(k) for k in ('kind', 'size', 'sha256')):
            entry['mode'] = old['mode']


def current(prefix):
    if storage.exists(prefix, metadata.PATH) and storage.file_size(prefix, metadata.PATH) > metadata.MAX_BYTES:
        raise ValueError('Tree metadata exceeds its byte bound')
    raw = storage.get_bytes(prefix, metadata.PATH) if storage.exists(prefix, metadata.PATH) else None
    if storage._use_s3():
        from .s3_streams import snapshot
        sizes = {item['path']: item['size'] for item in storage._s3_list(prefix)
                 if item['path'].split('/')[0].casefold() != '.vibeflow'}
        return remote_entries(sizes, lambda path, size: snapshot(prefix, path, limit=size)[0], raw), raw
    saved = metadata.decode(raw)['entries'] if raw is not None else {}
    entries = {}
    root = storage._fs_path(prefix, '.')
    for path in sorted(root.iterdir()):
        if path.name.casefold() == '.vibeflow' or is_file_stage(path.name):
            continue
        metadata.name(path.name)
        for name, entry in physical(root, path.name).items():
            entries[path.name+('/'+name if name else '')] = entry
        validate(entries)
    if os.name == 'nt':
        # DrvFS source copy historically enters the sandbox as mode 0777.
        for entry in entries.values():
            entry['mode'] = 0o777
        _restore_saved_modes(entries, saved)
    return validate(entries), raw


def directory_entries(prefix, paths=None):
    """Directory rows for the file browser without hashing ordinary file bodies."""
    from .storage_listing_budget import ListingBudget
    budget = ListingBudget()
    names = set()
    if storage._use_s3():
        if storage.exists(prefix, metadata.PATH):
            if storage.file_size(prefix, metadata.PATH) > metadata.MAX_BYTES:
                raise ValueError('Oversized tree metadata')
            saved = metadata.decode(storage.get_bytes(prefix, metadata.PATH))
            names.update(p for p, e in saved['entries'].items() if e['kind'] == 'directory' and (paths is None or p in paths))
    else:
        root = storage._fs_path(prefix, '.')
        pending = [root]
        while pending:
            budget.check()
            for child in pending.pop().iterdir():
                budget.visit()
                if is_file_stage(child.name) or (child.parent == root and child.name.casefold() == '.vibeflow'):
                    continue
                info = metadata.ordinary(child)
                if stat.S_ISDIR(info.st_mode):
                    names.add(child.relative_to(root).as_posix()); pending.append(child)
    return [{'path': p, 'kind': 'directory', 'size': 0, 'modified': ''} for p in sorted(names)]


def full_entries(prefix):
    return storage.list_tree(prefix) + directory_entries(prefix)


def listing_marker(prefix):
    """Observe publication and rollback without enumerating the file tree."""
    if storage._use_s3():
        from botocore.exceptions import ClientError
        from ..config import get_settings
        from .s3_live_store import HEAD, missing

        try:
            return storage.s3().raw.head_object(Bucket=get_settings().s3_bucket,
                                               Key=prefix.rstrip('/')+'/'+HEAD)['ETag']
        except ClientError as error:
            if not missing(error):
                raise
            return None

    from .file_read import _identity
    from .tree_transactions import PREFIX as TREE_JOURNAL

    root = storage._fs_path(prefix, '.')

    def identity(path):
        try:
            return _identity(path.lstat())
        except FileNotFoundError:
            return None

    # A journal is installed before any live change. Both settlement paths
    # move it to history, even if rollback leaves the SQL sequence unchanged.
    return (identity(root), identity(files._path(root, files.PREFIX)),
            identity(files._path(root, TREE_JOURNAL)))


def selected_entries(prefix, paths):
    """Resolve validated hints from current storage, including deleted entries."""
    if storage._use_s3():
        from ..config import get_settings
        from .s3_live_store import split, visible

        prefix = prefix.rstrip('/')
        if split(prefix+'/') != (prefix, ''):
            return None
        snapshot = storage.s3().public_snapshot(get_settings().s3_bucket, prefix)
        if snapshot is None:
            # Older prefixes have no manifest. Let the existing full-tree
            # reader handle both raw files and explicit directory metadata.
            return None
        files = snapshot.value['files']
        entries = [{'path': path, 'size': files[path]['size'], 'modified': files[path]['modified']}
                   for path in paths if visible(files.get(path))]
        present = {item['path'] for item in entries}
        return entries + directory_entries(prefix, set(paths) - present) if len(present) < len(paths) else entries

    from .storage_usage import _ordinary_info
    from .storage_listing_budget import ListingBudget

    budget = ListingBudget()
    entries = []
    root = storage._fs_path(prefix, '.')
    for relative in paths:
        budget.visit()
        if contains_file_stage(relative):
            continue
        path = storage._fs_path(prefix, relative)
        if path != root/relative:
            return None
        try:
            info = _ordinary_info(path.stat(follow_symlinks=False))
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISDIR(info.st_mode):
            entries.append({'path': relative, 'kind': 'directory', 'size': 0, 'modified': ''})
        else:
            entries.append({'path': relative, 'size': info.st_size,
                'modified': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(info.st_mtime))})
    return entries


def encoded(entries):
    validate(entries)
    raw = json.dumps({'schema': metadata.SCHEMA, 'materialize_directories': storage._use_s3(), 'entries': entries},
                     sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > metadata.MAX_BYTES:
        raise ValueError('Tree metadata exceeds its byte bound')
    return raw


def merge(prefix, initial, after, present, paths, *, allow_dictionary=False):
    """Selecting a directory includes its changed descendants, never new user data."""
    available = initial.keys() | after.keys()
    if not isinstance(paths, list) or not 1 <= len(paths) <= sandbox_volume.MAX_ENTRIES or len(set(paths)) != len(paths):
        raise ValueError('Invalid tree selection')
    selected = set()
    for path in paths:
        metadata.name(path)
        if path not in available:
            raise ValueError('Selection does not refer to a sandbox entry')
        selected.add(path)
        if any(tree.get(path, {}).get('kind') == 'directory' for tree in (initial, after)):
            selected.update(p for p in available if p.startswith(path+'/') and initial.get(p) != after.get(p))
    if len(selected) > sandbox_volume.MAX_ENTRIES:
        raise ValueError('Expanded tree selection exceeds its bound')
    target, actions = dict(present), []
    for path in sorted(selected):
        old, new, live = initial.get(path), after.get(path), present.get(path)
        if protected(prefix, path) and not (allow_dictionary and path == '0-caseDict/caseDict'):
            raise CaseWriteConflict('recovery_protected_path', '配置和版本历史请使用专用配置恢复', path=path)
        action = ('keep_current' if old == new else 'already_matches' if live == new else
                  'conflict' if live != old else 'delete' if new is None else
                  'create' if live is None else 'replace_type' if live['kind'] != new['kind'] else 'overwrite')
        if action not in {'keep_current', 'conflict', 'already_matches'}:
            if new is None:target.pop(path, None)
            else:target[path] = new
        actions.append({'path': path, 'initial': old, 'snapshot': new, 'current': live, 'action': action})
    # A directory deletion/type replacement must preserve concurrently added
    # children, and a partial selection cannot strand a child under a file.
    bad = set()
    for path in target:
        for parent in PurePosixPath(path).parents:
            if str(parent) != '.' and target.get(str(parent), {}).get('kind') != 'directory':
                bad.update(p for p in selected if p == str(parent) or p == path or p.startswith(str(parent)+'/'))
    for item in actions:
        if item['path'] in bad:item['action'] = 'conflict'
    if not any(item['action'] == 'conflict' for item in actions):validate(target)
    return target, actions
