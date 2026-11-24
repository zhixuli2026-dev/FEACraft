"""Local subtree swaps, journaled under the existing case lock and SQL witness.

Whole ordinary subtrees are renamed on the same filesystem. The two-rename
gap is explicit journal state; restart either retains the committed tree or
puts back the original tree. No recursive deletion is needed for recovery.
"""
import hashlib
import json
import os
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace
import uuid
from typing import TYPE_CHECKING

from .. import storage
from . import case_tree, file_transactions as files, tree_metadata_native as metadata
from .case_write import CaseWriteConflict, record_write, _writes
from .async_ops import finish_before_cancel
from .durable_events import append_event, json_text
from .file_stage_locations import MOUNTED_SCHEMA, existing_parent, locate
from .fs_directory import move_new, move_file_new
from .local_file_budget import allocation, check_quota
from .request_deadline import current as current_deadline

if TYPE_CHECKING:
    from .run_derive import DerivedRun

PREFIX = '.vibeflow/tree-transactions'
SCHEMA = 'vibeflow.tree-transaction.v1'
MAX_MANIFEST = 16*1024*1024
STAGE_SCHEMA = 'vibeflow.local-tree-stage.v1'
DERIVED_RUN = 'run-derive'


def _base(case):
    return files._path(storage._fs_path(case.s3_prefix, '.'), PREFIX)


def _compact(record):
    after = record.get('after')
    return isinstance(after, dict) and set(after) == {'bytes', 'sha256'}


def _derived_inventory(root, *, content):
    """Inventory a derived directory without the sandbox snapshot size limit."""
    from .storage_listing_budget import ListingBudget

    budget = ListingBudget()
    versions = {}
    digest, total = hashlib.sha256(), 0
    pending = [(root, '')]
    budget.visit()
    while pending:
        budget.check()
        path, relative = pending.pop()
        info = metadata.ordinary(path)
        versions[relative] = (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                              info.st_mtime_ns, info.st_ctime_ns)
        directory = stat.S_ISDIR(info.st_mode)
        if directory:
            children = []
            for child in path.iterdir():
                budget.visit()
                children.append(child)
            for child in sorted(children, key=lambda item: item.name, reverse=True):
                pending.append((child, relative+'/'+child.name if relative else child.name))
        elif content:
            total += info.st_size
        if content:
            signature = None
            if not directory:
                with budget.processing_entry():
                    signature = files._capture(path, limit=info.st_size, deadline=current_deadline())
            digest.update(json_text((relative, stat.S_IMODE(info.st_mode), signature)).encode()+b'\n')
    return versions, {'bytes':total, 'sha256':digest.hexdigest()}


def _versions(path, *, compact=False):
    if compact:
        return _derived_inventory(path, content=False)[0] if path.exists() else None
    return case_tree.versions(path)


def _tree(path, *, compact=False):
    if not path.exists():return None
    if compact:
        return _derived_inventory(path, content=True)[1]
    return case_tree.physical(path.parent, path.name)


def _move(source, destination, *, root):
    function = move_new if source.is_dir() else move_file_new
    return function(source, destination, root=root)


def _load(case, pending, installed=None, *, verify_retained=True):
    with files._path(pending, 'manifest.json').open('rb') as stream:
        encoded = stream.read(MAX_MANIFEST+1)
    if len(encoded) > MAX_MANIFEST:
        raise ValueError('Tree journal exceeds its manifest bound')
    document = json.loads(encoded); plan = document['plan']
    if installed is not None and installed.document == document and plan['binding'] == files._binding(case):
        # The same live request verified the installed identities and has a
        # durable witness for this exact document. A cold reader hashes files.
        installed.verify(case, pending)
        return document
    if (plan['schema'] != SCHEMA or plan['binding'] != files._binding(case) or
            str(uuid.UUID(plan['id'])) != plan['id'] or str(uuid.UUID(document['event_id'])) != document['event_id'] or
            hashlib.sha256(json_text(plan).encode()).hexdigest() != document['plan_sha256']):
        raise ValueError('Tree journal identity or digest differs')
    records = plan['roots']
    if not isinstance(records, list) or not 1 <= len(records) <= metadata.MAX_ENTRIES+1:
        raise ValueError('Tree journal exceeds its entry bound')
    scope = plan.get('scope')
    if scope not in (None, DERIVED_RUN) or (scope == DERIVED_RUN and len(records) != 1):
        raise ValueError('Invalid tree journal scope')
    seen, total = set(), 0
    for index, record in enumerate(records):
        path = record['path']
        compact = _compact(record)
        if scope == DERIVED_RUN:
            from .run_derive import destination

            parent, _, name = path.partition('/')
            _, validated = destination('derive-source', name)
            after = record['after']
            if (parent != 'C-run' or validated != path or not compact or record['before'] is not None
                    or type(after['bytes']) is not int or after['bytes'] < 0
                    or not isinstance(after['sha256'], str) or len(after['sha256']) != 64):
                raise ValueError('Invalid derived run journal')
            int(after['sha256'], 16)
        elif compact:
            raise ValueError('Derived directory requires its journal scope')
        elif path != metadata.PATH:
            metadata.name(path)
            if '/' in path:
                raise ValueError('Tree swaps require disjoint top-level roots')
        if path.casefold() in seen:
            raise ValueError('Duplicate tree journal root')
        seen.add(path.casefold())
        files._path(storage._fs_path(case.s3_prefix, '.'), path)
        for side in ('before', 'after'):
            tree = record[side]
            if tree is not None:
                if not compact:
                    if not isinstance(tree, dict) or '' not in tree or len(tree) > metadata.MAX_ENTRIES:
                        raise ValueError('Invalid journal subtree')
                    # Validate the root by placing it under a harmless canonical name.
                    case_tree.validate({'root'+('/'+p if p else ''): e for p, e in tree.items()})
                    total += sum(e.get('size', 0) for e in tree.values())
                retained = files._path(pending, side+'/'+str(index))
                if verify_retained and retained.exists() and _tree(retained, compact=compact) != tree:
                    raise ValueError('Retained tree journal copy differs')
    if total > 2*128*1024*1024+2*metadata.MAX_BYTES:
        raise ValueError('Tree journal exceeds its byte bound')
    return document


def _build(root, target, relative, destination, snapshot_root, present, generated, *, scan=_tree):
    subtree = {p: e for p, e in target.items() if p == relative or p.startswith(relative+'/')}
    if not subtree:
        return None
    for path in sorted(subtree, key=lambda p: (p.count('/'), p)):
        deadline = current_deadline()
        if deadline is not None:
            deadline.check()
        entry = subtree[path]
        suffix = path[len(relative):].lstrip('/')
        output = destination/suffix if suffix else destination
        if entry['kind'] == 'directory':
            output.mkdir(parents=True)
        else:
            expected = {k: entry[k] for k in ('size', 'sha256')}
            if path in generated:
                data = generated[path]
                captured = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                if captured == expected:
                    files._write_new(output, data)
            else:
                live = present.get(path, {})
                source = root if live.get('kind') == 'file' and all(live.get(k) == v for k, v in expected.items()) else snapshot_root
                captured = files._capture(files._path(source, path), output, limit=expected['size'], deadline=deadline)
            if captured != expected:
                raise CaseWriteConflict('recovery_plan_stale', '准备目录树时文件已变化，请重新预览', path=path)
    if os.name != 'nt':
        for path in sorted(subtree, key=lambda p: (-p.count('/'), p)):
            suffix = path[len(relative):].lstrip('/')
            os.chmod(destination/suffix if suffix else destination, subtree[path]['mode'])
    return scan(destination)


def _moved(expected, observed):
    if expected is None or observed is None or expected.keys() != observed.keys():
        return False
    # Renaming a root may update its ctime. Descendants keep their full identity.
    return all(value == observed[key] if key else value[:-1] == observed[key][:-1]
               for key, value in expected.items())


def _install(case, pending, index, record, identities=None):
    root = storage._fs_path(case.s3_prefix, '.')
    path = files._path(root, record['path'])
    compact = _compact(record)
    observed = _tree(path, compact=compact) if identities is None else _versions(path, compact=compact)
    if observed != (record['before'] if identities is None else identities[0]):
        raise files._conflict('Tree changed before its journal swap', record['path'])
    retained = live = None
    if record['before'] is not None:
        backup = files._path(pending, 'before/'+str(index))
        backup.parent.mkdir(exist_ok=True)
        _move(path, backup, root=root)
        files._sync(path.parent); files._sync(backup.parent)
        retained = _tree(backup) if identities is None else case_tree.versions(backup)
        if (retained != record['before'] if identities is None else not _moved(identities[0], retained)):
            raise files._conflict('Tree changed while moving its before-image', record['path'])
    if record['after'] is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = files._path(pending, 'after/'+str(index))
        if identities is not None and _versions(staged, compact=compact) != identities[1]:
            raise files._conflict('Prepared tree changed before installation', record['path'])
        _move(staged, path, root=root)
        files._sync(path.parent)
        if compact:
            files._sync(path.parent.parent)
            files._sync(staged.parent)
        if identities is not None:
            live = _versions(path, compact=compact)
            if not _moved(identities[1], live):
                raise files._conflict('Prepared tree changed while installing', record['path'])
    return retained, live


def dictionary_files(prepared, target):
    if prepared is None:
        return {}
    from .casedict import PreparedCaseDict
    if not isinstance(prepared, PreparedCaseDict):
        raise ValueError('Dictionary publication requires validated revision preparation')
    for path, data in prepared.files.items():
        entry = target.get(path, {})
        if (entry.get('kind') != 'file' or entry.get('size') != len(data)
                or entry.get('sha256') != hashlib.sha256(data).hexdigest()):
            raise ValueError('Prepared dictionary revision differs from target tree')
    return prepared.files


def staging_root(prefix):
    if storage._use_s3():
        return None
    root = storage._fs_root().resolve()
    return locate(root, f"{prefix.rstrip('/')}/{PREFIX}/pending",
                  files._path(root, '.tree-transaction-staging'))


def _check_swap_volume(root, snapshot, roots, device):
    # Replacing a subtree must leave any independent mount inside it intact.
    targets = set(roots)
    for path, version in snapshot.versions.items():
        if (path == metadata.PATH or path.split('/')[0] in targets) and version[0] != device:
            raise CaseWriteConflict('tree_mount_boundary', '目录树事务不能替换独立挂载的子目录，请调整选择', path=path)
    if (root.stat().st_dev != device
            or existing_parent(files._path(root, metadata.PATH)).stat().st_dev != device):
        raise CaseWriteConflict('tree_mount_boundary', '目录树和事务日志须在同一卷，请检查挂载')


def _verified_tree(path, *, compact=False):
    versions = _versions(path, compact=compact)
    tree = _tree(path, compact=compact)
    if versions != _versions(path, compact=compact):
        raise case_tree.stale_tree()
    return tree, versions


def _stage_tree(root, snapshot, target, source, generated, stage, roots, raw):
    snapshot.verify()
    records, identities = [], []
    stage.mkdir()
    for index, relative in enumerate(roots):
        before, old = _verified_tree(files._path(root, relative))
        after, new = (_build(root, target, relative, stage/'after'/str(index), source,
                             snapshot.entries, generated, scan=_verified_tree) or (None, None))
        records.append({'path': relative, 'before': before, 'after': after})
        identities.append((old, new))
    before, old = _verified_tree(files._path(root, metadata.PATH))
    path = stage/'after'/str(len(roots))
    files._write_new(path, raw)
    after, new = _verified_tree(path)
    records.append({'path': metadata.PATH, 'before': before, 'after': after})
    identities.append((old, new))
    snapshot.verify()
    return records, identities


@dataclass(frozen=True)
class InstalledTree:
    document: dict
    identities: list

    def verify(self, case, pending):
        root = storage._fs_path(case.s3_prefix, '.')
        for index, (record, (retained, live)) in enumerate(zip(self.document['plan']['roots'], self.identities)):
            compact = _compact(record)
            if (_versions(files._path(root, record['path']), compact=compact) != live
                    or _versions(files._path(pending, 'before/'+str(index)), compact=compact) != retained
                    or _versions(files._path(pending, 'after/'+str(index)), compact=compact) is not None):
                raise files._conflict('Committed tree changed before journal settlement', record['path'])


@dataclass(frozen=True)
class PreparedTree:
    snapshot: 'case_tree.LocalSnapshot | DerivedRun'
    stage: Path
    records: list
    identities: list
    changed: list
    peak: int
    scope: str | None = None
    staged: tuple[Path, int] | None = None

    async def publish(self, db, case):
        current = _writes.get()
        if current is None or current[0] != case.s3_prefix:
            raise ValueError('Tree writes require the case coordinator')
        if case.s3_prefix != self.snapshot.prefix:
            raise case_tree.stale_tree()
        root = storage._fs_path(case.s3_prefix, '.')
        base = _base(case)
        pending = files._path(root, PREFIX+'/pending')
        if pending.exists() or files._path(root, files.PREFIX+'/pending').exists():
            raise files._conflict('A file or tree transaction is already pending')
        await check_quota(db, case, self.peak, staged=self.staged)
        await files._io(self.snapshot.verify)
        identifier, event_id = str(uuid.uuid4()), str(uuid.uuid4())
        plan = {'schema': SCHEMA, 'id': identifier, 'binding': files._binding(case), 'roots': self.records}
        if self.scope is not None:
            plan['scope'] = self.scope
        plan_hash = hashlib.sha256(json_text(plan).encode()).hexdigest()
        document = {'plan': plan, 'event_id': event_id, 'plan_sha256': plan_hash}
        encoded = json_text(document).encode()
        if len(encoded) > MAX_MANIFEST:
            raise ValueError('Tree transaction manifest exceeds its bound')
        async with db.begin_nested():
            await append_event(db, case.id, files.EVENT, {'transaction_id': identifier, 'plan_sha256': plan_hash}, event_id=event_id)
            await files._io(files._publish_prepared, self.stage, pending, storage._fs_root(), base, encoded)
            db.info.setdefault('local_file_transactions', {})[case.id] = (db.sync_session.get_transaction(), identifier)
            installed = []
            for index, record in enumerate(self.records):
                installed.append(await files._io(_install, case, pending, index, record, self.identities[index]))
            db.info.setdefault('installed_local_trees', {})[case.id] = InstalledTree(document, installed)
            if self.scope == DERIVED_RUN:
                await append_event(db, case.id, 'files.changed',
                                   {'paths': self.changed, 'changes': [], 'rescan': True})
            else:
                for path in self.changed:
                    record_write(case.s3_prefix, path)
        return identifier


@asynccontextmanager
async def prepare(db, case, location, snapshot, target, snapshot_root, *, prepared_dictionary=None):
    from .case_write import bounded_case_read
    from .owned_staging import batch, recover_abandoned

    generated = dictionary_files(prepared_dictionary, target)
    case_tree.validate(target)
    changed = sorted(p for p in snapshot.entries.keys() | target.keys() if snapshot.entries.get(p) != target.get(p))
    for path in changed:
        if case_tree.protected(snapshot.prefix, path) and path not in generated:
            raise CaseWriteConflict('recovery_protected_path', '项目配置及版本历史必须走专用流程', path=path)
    roots = sorted({p.split('/')[0] for p in changed})
    if not roots:
        yield None
        return
    raw = case_tree.encoded(target)
    materialized = sum(e.get('size', 0) for p, e in target.items() if p.split('/')[0] in roots) + len(raw)
    peak = materialized + MAX_MANIFEST + 16384
    live = storage._fs_path(snapshot.prefix, '.')
    await bounded_case_read(_check_swap_volume, live, snapshot, roots, location.device,
                            code='tree_prepare', label='目录树挂载校验')
    if location.local:
        # An exited process's staged bodies count toward this account's usage.
        await files._io(recover_abandoned, location.scratch, MOUNTED_SCHEMA)
    await check_quota(db, case, peak)
    await finish_before_cancel(db.commit())
    async with batch(location.scratch, peak, MOUNTED_SCHEMA if location.local else STAGE_SCHEMA,
                     target_root=location.scratch if location.local else None) as directory:
        stage = directory/'journal'
        records, identities = await bounded_case_read(_stage_tree, live, snapshot, target, Path(snapshot_root),
            generated, stage, roots, raw, code='tree_prepare', label='目录树暂存')
        yield PreparedTree(snapshot, stage, records, identities, changed, peak,
                           staged=(stage, materialized) if location.local else None)


async def write(db, case, present, target, snapshot_root, expected_metadata, *, prepared_dictionary=None):
    generated = dictionary_files(prepared_dictionary, target)
    if storage._use_s3():
        from .tree_transactions_s3 import write as remote
        return await remote(db, case, present, target, snapshot_root, expected_metadata,
                            prepared_dictionary=prepared_dictionary)
    current = _writes.get()
    if current is None or current[0] != case.s3_prefix:
        raise ValueError('Tree writes require the case coordinator')
    case_tree.validate(present); case_tree.validate(target)
    changed = sorted(p for p in present.keys() | target.keys() if present.get(p) != target.get(p))
    for path in changed:
        if case_tree.protected(case.s3_prefix, path) and path not in generated:
            raise CaseWriteConflict('recovery_protected_path', '项目配置及版本历史必须走专用流程', path=path)
    roots = sorted({p.split('/')[0] for p in changed})
    if not roots:
        return None
    root = storage._fs_path(case.s3_prefix, '.')
    base = _base(case); pending = files._path(root, PREFIX+'/pending')
    if pending.exists():
        raise files._conflict('A tree transaction is already pending')
    if files._path(root, files.PREFIX+'/pending').exists():
        raise files._conflict('A file transaction is already pending')
    identifier = str(uuid.uuid4()); stage = files._path(root, PREFIX+'/preparing-'+identifier)
    raw = case_tree.encoded(target)
    bytes_after = sum(e.get('size', 0) for p, e in target.items() if p.split('/')[0] in roots)
    # One additional tree is built; old roots are moved, never copied.
    async with allocation(db, case, bytes_after+len(raw)+MAX_MANIFEST+16384):
        stage.mkdir(parents=True)
        observed, old_metadata = await files._io(case_tree.current, case.s3_prefix)
        if observed != present or old_metadata != expected_metadata:
            raise CaseWriteConflict('recovery_plan_stale', '项目目录树在预览后变化，请重新预览')
        records = []
        for index, relative in enumerate(roots):
            before = await files._io(case_tree.physical, root, relative)
            after = await files._io(_build, root, target, relative, files._path(stage, 'after/'+str(index)), Path(snapshot_root), present, generated)
            records.append({'path': relative, 'before': before, 'after': after})
        index = len(records)
        before = await files._io(case_tree.physical, root, metadata.PATH)
        await files._io(files._write_new, files._path(stage, 'after/'+str(index)), raw)
        records.append({'path': metadata.PATH, 'before': before, 'after': await files._io(_tree, stage/'after'/str(index))})
        plan = {'schema': SCHEMA, 'id': identifier, 'binding': files._binding(case), 'roots': records}
        plan_hash = hashlib.sha256(json_text(plan).encode()).hexdigest()
        event_id = str(uuid.uuid4())
        document = {'plan': plan, 'event_id': event_id, 'plan_sha256': plan_hash}
        encoded = json_text(document).encode()
        if len(encoded) > MAX_MANIFEST:
            raise ValueError('Tree transaction manifest exceeds its bound')
        async with db.begin_nested():
            await append_event(db, case.id, files.EVENT, {'transaction_id': identifier, 'plan_sha256': plan_hash}, event_id=event_id)
            await files._io(files._publish_prepared, stage, pending, root, base, encoded)
            db.info.setdefault('local_file_transactions', {})[case.id] = (db.sync_session.get_transaction(), identifier)
            reference = SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix)
            for index, record in enumerate(records):
                await files._io(_install, reference, pending, index, record)
            for path in changed:record_write(case.s3_prefix, path)
    return identifier


def _observed(case, pending, records, committed):
    root = storage._fs_path(case.s3_prefix, '.')
    observed = []
    for index, record in enumerate(records):
        compact = _compact(record)
        if not committed and compact and files._path(pending, 'after/'+str(index)).exists():
            # The validated after-image is still staged, so the no-replace move
            # never installed it. A concurrently created live root is unowned.
            observed.append(None)
            continue
        live = _tree(files._path(root, record['path']), compact=compact)
        valid = live == record['after'] if committed else live in (record['before'], record['after'], None)
        if not valid:
            raise files._conflict('Pending tree conflicts with current data; preserve it for review', record['path'])
        if not committed and live != record['before'] and record['before'] is not None:
            if _tree(files._path(pending, 'before/'+str(index))) != record['before']:
                raise files._conflict('Tree before-image is unavailable', record['path'])
        observed.append(live)
    return observed


def _rollback(case, pending, index, record, live):
    root = storage._fs_path(case.s3_prefix, '.')
    path = files._path(root, record['path'])
    compact = _compact(record)
    if compact and files._path(pending, 'after/'+str(index)).exists():
        return
    if _tree(path, compact=compact) != live:
        raise files._conflict('Tree changed before rollback', record['path'])
    if live == record['before']:
        return
    if live is not None:
        discarded = files._path(pending, 'discarded/'+str(index))
        discarded.parent.mkdir(exist_ok=True)
        _move(path, discarded, root=root); files._sync(path.parent)
    if record['before'] is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _move(files._path(pending, 'before/'+str(index)), path, root=root); files._sync(path.parent)


def _recovery_paths(case, pending, index, record):
    root = storage._fs_path(case.s3_prefix, '.')
    return (files._path(root, record['path']), files._path(pending, 'before/'+str(index)),
            files._path(pending, 'after/'+str(index)))


@dataclass(frozen=True)
class RecoveryRead:
    document: dict
    manifest_version: dict
    observed: list
    identities: list

    def verify(self, case, pending, *, committed):
        plan = self.document['plan']
        if plan['binding'] != files._binding(case):
            raise files._conflict('Pending tree binding changed during recovery')
        if case_tree.versions(files._path(pending, 'manifest.json')) != self.manifest_version:
            raise files._conflict('Pending tree journal changed during recovery')
        for index, record in enumerate(plan['roots']):
            live, before, after = self.observed[index]
            compact = _compact(record)
            staged = compact and after == record['after']
            valid = live == record['after'] and not staged if committed else (
                staged or live in (record['before'], record['after'], None))
            if not valid:
                raise files._conflict('Pending tree conflicts with current data; preserve it for review', record['path'])
            if not committed and live != record['before'] and before != record['before']:
                raise files._conflict('Tree before-image is unavailable', record['path'])
            paths = _recovery_paths(case, pending, index, record)
            if tuple(_versions(path, compact=compact) for path in paths) != self.identities[index]:
                raise files._conflict('Pending tree versions changed during recovery', record['path'])

    def rollback(self, case, pending, index, record):
        observed, _, staged = self.observed[index]
        compact = _compact(record)
        if observed == record['before'] or (compact and staged == record['after']):
            return
        path, backup, _ = _recovery_paths(case, pending, index, record)
        live, before, _ = self.identities[index]
        if _versions(path, compact=compact) != live or _versions(backup, compact=compact) != before:
            raise files._conflict('Tree changed before rollback', record['path'])
        root = storage._fs_path(case.s3_prefix, '.')
        if live is not None:
            discarded = files._path(pending, 'discarded/'+str(index))
            discarded.parent.mkdir(exist_ok=True)
            _move(path, discarded, root=root)
            files._sync(path.parent)
            if not _moved(live, _versions(discarded, compact=compact)):
                raise files._conflict('Tree changed during rollback', record['path'])
        if before is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            _move(backup, path, root=root)
            files._sync(path.parent)
            if not _moved(before, _versions(path, compact=compact)):
                raise files._conflict('Before-image changed during rollback', record['path'])


def pending_reference(case):
    try:
        root = storage._fs_path(case.s3_prefix, '.')
        if not root.exists():
            return None
        base = _base(case)
        if base.exists() and files._path(base, 'pending').exists():
            return SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix)
        return None
    except (ValueError, OSError) as error:
        raise files._conflict('Tree transaction cannot be verified; preserve it for review') from error


def read_recovery(case):
    return observe_recovery(case, clean_staging=True)


def observe_recovery(case, *, clean_staging=False):
    """Read directory journal evidence without cleaning any private copies."""
    try:
        base = _base(case)
        if not base.exists():
            return None
        pending = files._path(base, 'pending')
        if clean_staging:
            from .owned_staging import recover_abandoned
            location = staging_root(case.s3_prefix)
            if location is not None:
                recover_abandoned(location.scratch, MOUNTED_SCHEMA if location.local else STAGE_SCHEMA)
        manifest = files._path(pending, 'manifest.json')
        manifest_version = case_tree.versions(manifest)
        if manifest_version is None and not pending.exists():
            return None
        document = _load(case, pending, verify_retained=False)
        observed, identities = [], []
        for index, record in enumerate(document['plan']['roots']):
            paths = _recovery_paths(case, pending, index, record)
            compact = _compact(record)
            live, before, after = (_verified_tree(path, compact=compact) for path in paths)
            for side, (retained, _) in [('before', before), ('after', after)]:
                if retained is not None and record[side] is not None and retained != record[side]:
                    raise ValueError('Retained tree journal copy differs')
            observed.append((live[0], before[0], after[0]))
            identities.append((live[1], before[1], after[1]))
        if case_tree.versions(manifest) != manifest_version:
            raise files._conflict('Pending tree journal changed during recovery')
        return RecoveryRead(document, manifest_version, observed, identities)
    except (TimeoutError, CaseWriteConflict):
        raise
    except FileNotFoundError as error:
        if pending_reference(case) is None:
            return None
        raise files._conflict('Tree transaction cannot be verified; preserve it for review') from error
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        raise files._conflict('Tree transaction cannot be verified; preserve it for review') from error


async def recover_locked(db, case, *, inspect_only=False):
    if not storage._fs_path(case.s3_prefix, '.').exists():return None
    base = _base(case)
    if not base.exists():return None
    pending = files._path(base, 'pending')
    if not pending.exists():
        return None
    reference = SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix)
    try:
        prepared = db.info.pop('local_tree_recovery_read', None)
        installed = db.info.get('installed_local_trees', {}).pop(case.id, None)
        if (installed is not None and (db.info.get('local_file_transactions', {}).get(case.id) ==
                (db.sync_session.get_transaction(), installed.document['plan']['id'])
                or await files._witness(db, case, installed.document) is None)):
            installed = None
        document = (prepared.document if prepared is not None
                    else await files._io(_load, reference, pending, installed))
        plan = document['plan']
        event = await files._witness(db, case, document)
        if db.info.get('local_file_transactions', {}).get(case.id) == (db.sync_session.get_transaction(), plan['id']):
            return {'transaction_id': plan['id'], 'decision': 'in_transaction'}
        if prepared is not None:
            await files._io(partial(prepared.verify, reference, pending, committed=event is not None))
        elif installed is None or document != installed.document or event is None:
            observed = await files._io(_observed, reference, pending, plan['roots'], event is not None)
        else:
            observed = []  # The live installer verified the same exact tree above.
        if inspect_only:
            return {'backend': 'fs', 'status': 'pending', 'kind': 'tree', 'transaction_id': plan['id'], 'database_committed': event is not None}
        from .case_validation import assert_writable_locked
        await assert_writable_locked(db, case.id)
        receipt = {'transaction_id': plan['id'], 'decision': 'committed' if event else 'rolled_back',
                   'event_id': document['event_id'], 'plan_sha256': document['plan_sha256']}
        encoded = json_text(receipt).encode()
        # Rollback renames retained roots on the same volume. Only the receipt
        # adds file bytes, and its admission must precede the first rename.
        async with allocation(db, case, len(encoded), target_root=base):
            if event is None:
                for index in reversed(range(len(plan['roots']))):
                    record = plan['roots'][index]
                    if prepared is None:
                        await files._io(_rollback, reference, pending, index, record, observed[index])
                    else:
                        await files._io(prepared.rollback, reference, pending, index, record)
            await files._io(files._write_new, files._path(pending, 'resolution-'+uuid.uuid4().hex+'.json'), encoded)
            history = files._path(base, 'history'); history.mkdir(exist_ok=True)
            await files._io(move_new, pending, history/plan['id'], root=storage._fs_path(case.s3_prefix, '.'))
            await files._io(files._sync, base)
        return receipt
    except (TimeoutError, CaseWriteConflict):
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        raise files._conflict('Tree transaction cannot be verified; preserve it for review') from error
