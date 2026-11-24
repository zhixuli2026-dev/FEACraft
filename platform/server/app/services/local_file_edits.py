"""Stage ordinary local file edits before entering the SQL write transaction."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
import uuid

from .. import storage
from . import file_transactions as files, tree_metadata_edits
from .attempt_workspace import ordinary
from .case_provision import io
from .case_write import CaseWriteConflict, _writes, record_write
from .durable_events import append_event, json_text
from .file_read import _identity
from .file_stage_locations import batches, locate
from .local_file_budget import check_quota, replacement_peak
from .request_deadline import current
from .tree_metadata_native import PATH as MODE_PATH


SCHEMA = 'vibeflow.local-file-edit-staging.v1'
CODE = 'file_write'
LABEL = '文件暂存'


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [('created', ctypes.c_longlong), ('accessed', ctypes.c_longlong),
                    ('written', ctypes.c_longlong), ('changed', ctypes.c_longlong),
                    ('attributes', wintypes.DWORD)]

    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL

    def _change_time(path):
        # Attribute-only, fully shared handle. Python 3.11 reports creation as st_ctime on Windows.
        handle = _kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result = FileBasicInfo()
            if not _kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(result), ctypes.sizeof(result)):
                raise ctypes.WinError(ctypes.get_last_error())
            return result.changed
        finally:
            _kernel.CloseHandle(handle)


def _version(path):
    try:
        info = ordinary(path)
        identity = _identity(info)
        if os.name == 'nt':
            change = _change_time(path)
            if _identity(ordinary(path)) != identity:
                raise files._conflict('File changed while checking its identity')
            identity = (*identity[:4], change)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('File transactions require regular files')
    return (*identity, stat.S_IMODE(info.st_mode))


def _check(path, expected):
    deadline = current()
    if deadline is not None:
        deadline.check()
    return _version(path) == expected


def _conflict(path, revision=None):
    return CaseWriteConflict('file_conflict', '文件内容已变化，请重新读取后再保存',
                             path=path, current_revision=revision)


def _plan(case, changes, expected):
    root = storage._fs_path(case.s3_prefix, '.')
    scratch = storage._fs_path('.', '.file-write-staging')
    journal = locate(root, files.PREFIX+'/pending', scratch)
    paths = (*changes, MODE_PATH)
    observations = {path: _version(files._path(root, path)) for path in paths}
    changes, expected, scope = tree_metadata_edits.expand(case.s3_prefix, changes, expected, 'files')
    max_files, bound, max_total, max_manifest = files.limits(scope)
    if len(changes) > max_files:
        raise ValueError('Invalid file transaction size')
    before_total, after_total, objects = 0, 0, {}
    for index, (relative, contents) in enumerate(changes.items()):
        version = observations[relative]
        old_size = version[2] if version is not None else 0
        new_size = contents.expected['size'] if isinstance(contents, files.FileSource) else len(contents or b'')
        if max(old_size, new_size) > bound:
            raise ValueError('File transaction object exceeds 1 GiB')
        before_total += old_size
        after_total += new_size
        if contents is not None:
            objects[index] = (locate(root, relative, scratch, shared=journal), new_size)
    if before_total + after_total > max_total:
        raise ValueError('File transaction exceeds 2 GiB including backups')
    reserve = before_total + after_total + max_manifest + 4096
    return (root, changes, expected, scope, observations), journal, objects, reserve


def _stage(case, plan, journal_location, objects, directories):
    root, changes, expected, scope, observations = plan
    stage = directories[journal_location]/'journal'
    stage.mkdir()
    records, staged, install = [], {}, {}
    deadline = current()
    for index, (relative, data) in enumerate(changes.items()):
        source = files._path(root, relative)
        version = observations[relative]
        if not _check(source, version):
            raise _conflict(relative)
        before = None
        if version is not None:
            backup = files._blob(stage, index, 'before')
            try:
                before = files._capture(source, backup, limit=version[2], deadline=deadline)
            except (CaseWriteConflict,files.CaptureLimitExceeded) as error:
                raise _conflict(relative) from error
            staged[f'before/{index}'] = _version(backup)
            if not _check(source, version):
                raise _conflict(relative)
        files.check_expected(relative, before, expected)
        after = None
        if data is not None:
            target = files._blob(stage, index, 'after')
            replacement = directories[objects[index][0]]/'install'/str(index)
            if isinstance(data, files.FileSource):
                after = data.expected
                if files._capture(data.resolve(case.s3_prefix), target, limit=after['size'], deadline=deadline) != after:
                    raise data.changed('File transaction source changed before staging', relative)
                if files._capture(target, replacement, limit=after['size'], deadline=deadline) != after:
                    raise data.changed('Prepared file changed before staging its replacement', relative)
            else:
                after = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                files._write_new(target, data)
                files._write_new(replacement, data)
            staged[f'after/{index}'] = _version(target)
            if os.name != 'nt' and version is not None:
                os.chmod(replacement, version[-1])
            install[index] = (replacement, _version(replacement))
        entry = {'path': relative, 'before': before, 'after': after}
        if os.name != 'nt' and version is not None:
            entry['before_mode'] = version[-1]
            if after is not None:
                entry['after_mode'] = version[-1]
        records.append(entry)
    tree_metadata_edits.check_records(scope, records)
    identifier, event_id = str(uuid.uuid4()), str(uuid.uuid4())
    journal = {'schema': files.SCHEMA, 'id': identifier, 'binding': files._binding(case), 'files': records}
    if scope != 'files':
        journal['scope'] = scope
    digest = hashlib.sha256(json_text(journal).encode()).hexdigest()
    document = {'plan': journal, 'event_id': event_id, 'plan_sha256': digest}
    encoded = json_text(document).encode()
    if len(encoded) > files.limits(scope)[3]:
        raise ValueError('Oversized file journal')
    receipt = {'transaction_id': identifier, 'decision': 'committed',
               'event_id': event_id, 'plan_sha256': digest}
    receipt_bytes = json_text(receipt).encode()
    journal_bytes = sum(stamp[2] for stamp in staged.values())
    # Materialize the receipt before SQL commit so its later rename consumes
    # no additional owner or node capacity. The SQL witness still decides.
    peak = journal_bytes + len(encoded) + len(receipt_bytes)
    peak += replacement_peak(((r['before'], r['after']) for r in records), 0)
    # The owner scan already sees the copies prepared inside the case.
    peak -= sum(size for location, size in objects.values() if location.local)
    if journal_location.local:
        peak -= journal_bytes
    return PreparedEdit(root, stage, observations, staged, install, document, encoded, receipt_bytes, peak)


@dataclass(frozen=True)
class InstalledEdit:
    pending: Path
    document: dict
    versions: dict

    def matches(self, pending):
        manifest = pending/'manifest.json'
        return self.pending == pending and _check(manifest, self.versions[manifest])

    def verify(self, case):
        if (self.document['plan']['binding'] != files._binding(case)
                or any(not _check(path, stamp) for path, stamp in self.versions.items())):
            raise files._conflict('Committed file journal or live bytes changed before settlement')


@dataclass(frozen=True)
class PreparedEdit:
    root: Path
    stage: Path
    observations: dict
    staged: dict
    install: dict
    document: dict
    encoded: bytes
    receipt_bytes: bytes
    peak: int

    def publish_files(self, case, pending):
        for relative, version in self.observations.items():
            if not _check(files._path(self.root, relative), version):
                raise _conflict(relative, case.dict_revision)
        for relative, version in self.staged.items():
            if not _check(files._path(self.stage, relative), version):
                raise files._conflict('Prepared file journal changed before publication')
        ready = self.stage/files.PREPARED_RESOLUTION
        files._write_new(ready, self.receipt_bytes)
        ready_version = _version(ready)
        try:
            files._publish_prepared(self.stage, pending, storage._fs_root(), files._base(case), self.encoded)
        except OSError as error:
            if error.errno == errno.EXDEV:
                raise files._conflict('File journal mount changed before publication') from error
            raise
        manifest = files._path(pending, 'manifest.json')
        manifest_version = _version(manifest)
        with manifest.open('rb') as stream:
            if stream.read(len(self.encoded)+1) != self.encoded or not _check(manifest, manifest_version):
                raise files._conflict('File journal manifest changed during publication')
        paid = files._path(pending, files.PREPARED_RESOLUTION)
        paid_version = _version(paid)
        if paid_version != ready_version:
            raise files._conflict('Prepared resolution bytes changed before publication')
        versions = {manifest: manifest_version, paid: paid_version}
        for relative, version in self.staged.items():
            backup = files._path(pending, relative)
            if not _check(backup, version):
                raise files._conflict('Prepared file journal changed during publication')
            versions[backup] = version
        for index, entry in enumerate(self.document['plan']['files']):
            target = files._path(self.root, entry['path'])
            versions[target] = self.install_file(case, target, index, entry)
        return InstalledEdit(pending, self.document, versions)

    def install_file(self, case, target, index, entry):
        relative = entry['path']
        if not _check(target, self.observations[relative]):
            raise _conflict(relative, case.dict_revision)
        try:
            if entry['after'] is None:
                target.unlink(missing_ok=True)
            else:
                replacement, version = self.install[index]
                replacement = files._path(replacement.parent.parent, f'install/{index}')
                if not _check(replacement, version):
                    raise files._conflict('Prepared replacement bytes changed before publication', relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(replacement, target)
        except OSError as error:
            if error.errno != errno.EXDEV and getattr(error, 'winerror', None) not in {5, 32, 33}:
                raise
            raise _conflict(relative, case.dict_revision) from error
        if target.parent.exists():
            files._sync(target.parent)
        installed = _version(target)
        if entry['after'] is None:
            confirmed = installed is None
        else:
            _, prepared = self.install[index]
            # Rename can change the metadata change time.
            confirmed = installed is not None and (*installed[:4], installed[-1]) == (*prepared[:4], prepared[-1])
        if not confirmed:
            raise files._conflict('File changed during publication', relative)
        return installed

    async def publish(self, db, case, conditions=None, *, inventory=None):
        if _writes.get() is None or _writes.get()[0] != case.s3_prefix:
            raise ValueError('File writes require the case coordinator')
        plan = self.document['plan']
        if plan['binding'] != files._binding(case):
            raise CaseWriteConflict('case_source_changed', '项目来源已变化，请先核对文件')
        for entry in plan['files']:
            relative, before = entry['path'], entry['before']
            if conditions and relative in conditions and (before['sha256'] if before is not None else None) != conditions[relative]:
                raise _conflict(relative, case.dict_revision)
        await check_quota(db, case, max(0, self.peak), inventory=inventory)
        pending = files._path(self.root, files.PREFIX+'/pending')
        if pending.exists():
            raise files._conflict('A file transaction is already pending for this case')
        event_id, digest = self.document['event_id'], self.document['plan_sha256']
        identifier = plan['id']
        async with db.begin_nested():
            await append_event(db, case.id, files.EVENT,
                               {'transaction_id': identifier, 'plan_sha256': digest}, event_id=event_id)
            installed = await io(self.publish_files, case, pending)
            db.info.setdefault('local_file_transactions', {})[case.id] = (db.sync_session.get_transaction(), identifier)
            db.info.setdefault('installed_local_file_edits', {})[case.id] = installed
            for entry in plan['files']:
                record_write(case.s3_prefix, entry['path'])


@asynccontextmanager
async def prepare_changes(case, changes, expected=None):
    """Caller owns the copy slot. Prepare the complete journal outside SQL."""
    plan, journal, objects, reserve = await io(_plan, case, changes, expected)
    async with batches(objects, SCHEMA, default=journal, tail=reserve) as directories:
        yield await io(_stage, case, plan, journal, objects, directories)
