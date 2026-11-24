"""Process-owned input caches, recoverable without deleting unknown contents.

Only a verified exited owner permits background cleanup. Each file's digest is
flushed before materialization; a partial write or changed inventory is retained.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import logging
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from contextvars import copy_context
from contextlib import contextmanager
from functools import lru_cache, wraps

from .. import storage
from ..config import get_settings
from . import process_identity_native as identity

SCHEMA = 'vibeflow.private-cache.v1'
KINDS = {'.case-read-views', '.attempt-sources'}
MAX_LOG = 64 * 1024 * 1024
MAX_OWNER = 16384
RECOVERY_PAGE_SIZE = 32
STREAM_STAGE = re.compile(r'stream-[0-9a-f]{32}\.part')
logger=logging.getLogger(__name__)


class _CleanupSharing(Exception):
    def __init__(self,error):self.winerror=error.winerror


def _writing(method):
    @wraps(method)
    def guarded(self,*args,**kwargs):
        with self._operation:
            if self._closed:raise ValueError('Private cache writers are closed')
            return method(self,*args,**kwargs)
    return guarded


@contextmanager
def _deletion_handle(path):
    """Keep metadata deletable until its final unlink; verify the opened object.

    Windows DELETE access excludes new handles that deny deletion. Reading the
    metadata through this same descriptor avoids reopening it without sharing.
    """
    import stat
    from .attempt_workspace import ordinary
    before=ordinary(path)
    if os.name=='nt':
        import ctypes,msvcrt
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
            wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
        handle=kernel.CreateFileW(str(path),0x80010000,0x1|0x4,None,3,0x02000000|0x00200000,None)
        if handle in {None,ctypes.c_void_p(-1).value}:raise ctypes.WinError(ctypes.get_last_error())
        try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
        except BaseException:kernel.CloseHandle(handle);raise
    else:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        actual=os.fstat(fd)
        if ((actual.st_dev,actual.st_ino)!=(before.st_dev,before.st_ino)
                or stat.S_ISDIR(actual.st_mode)!=stat.S_ISDIR(before.st_mode)
                or getattr(actual,'st_file_attributes',0)&0x400
                or (stat.S_ISREG(actual.st_mode) and actual.st_nlink!=1)):
            raise ValueError('Private cache deletion handle differs from its ordinary path')
        yield fd
    finally:os.close(fd)


def _check(deadline):
    if deadline is not None:
        deadline.check()


def _fingerprint(read, limit, deadline):
    digest, size = hashlib.sha256(), 0
    while True:
        _check(deadline)
        block = read(min(1024 * 1024, limit + 1 - size))
        if not block:
            return {'size': size, 'sha256': digest.hexdigest()}
        size += len(block)
        if size > limit:
            raise ValueError('Private cache file exceeds its recorded size')
        digest.update(block)


def _fingerprint_path(path, limit, deadline):
    with path.open('rb') as stream:
        return _fingerprint(stream.read, limit, deadline)


def _read_owner(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_OWNER + 1)
    if len(raw) > MAX_OWNER:
        raise ValueError('Oversized cache owner record')
    return raw


@lru_cache(maxsize=1)
def _linux_statx():
    import ctypes

    # Linux statx ABI: the creation timestamp starts at byte 0x50.
    class FileStat(ctypes.Structure):
        _fields_ = [('mask', ctypes.c_uint32), ('before_birth', ctypes.c_byte * 76),
                    ('birth_seconds', ctypes.c_int64), ('birth_ns', ctypes.c_uint32),
                    ('after_birth', ctypes.c_byte * 164)]

    statx = getattr(ctypes.CDLL(None, use_errno=True), 'statx', None)
    if statx is not None:
        statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint,
                          ctypes.POINTER(FileStat)]
        statx.restype = ctypes.c_int
    return statx, FileStat


def _stage_identity(fd, info):
    birth = getattr(info, 'st_birthtime_ns', None)
    if birth is None and os.name == 'nt':
        birth = info.st_ctime_ns  # CPython before st_birthtime_ns on Windows.
    if birth is None and sys.platform.startswith('linux'):
        statx, result_type = _linux_statx()
        result = result_type()
        at_empty_path, statx_btime = 0x1000, 0x800
        if (statx is not None and statx(fd, b'', at_empty_path, statx_btime, result) == 0
                and result.mask & statx_btime):
            birth = result.birth_seconds * 1_000_000_000 + result.birth_ns
    if not birth or not info.st_ino:
        return None
    return hashlib.sha256(json.dumps([info.st_dev, info.st_ino, birth]).encode()).hexdigest()


def _stage_record(name, size, file_id):
    return {'path':name, 'stage':True, 'size':size, 'file_id':file_id}


def parent_for(kind):
    from .file_transactions import _path
    if kind not in KINDS:
        raise ValueError('Unknown private cache kind')
    root = storage._fs_root()
    root.mkdir(parents=True, exist_ok=True)
    return _path(root, kind)


class Cache:
    def __init__(self, kind):
        from .file_transactions import _write_new
        from .node_storage_budget import reserve
        self._operation=threading.RLock();self._closed=False;self._cleanup_running=False
        self._journal_lock = threading.Lock()
        self.parent = parent_for(kind)
        self.parent.mkdir(parents=True, exist_ok=True)
        self.container = self.parent / ('owned-' + uuid.uuid4().hex)
        self.record = {'schema': SCHEMA, 'kind': kind, 'name': self.container.name,
                       'parent': str(self.parent.resolve()), 'owner': identity.observe(os.getpid())}
        encoded=json.dumps(self.record, sort_keys=True).encode()
        with reserve(len(encoded)):
            self.container.mkdir()
            _write_new(self.container / 'owner.json', encoded)
            _write_new(self.container / 'files.jsonl', b'')
        self.root = self.container / 'data'
        self.root.mkdir()
        self.name = str(self.root)

    def write(self, relative, data):
        self.fetch(relative,len(data),lambda:data)

    @_writing
    def fetch(self, relative, size, reader):
        """Reserve data and journal bytes before invoking the network reader."""
        from .node_storage_budget import reserve
        if type(size) is not int or size<0:raise ValueError('Invalid cache input size')
        predicted={'path':relative,'size':size,'sha256':'0'*64}
        with reserve(size+len(json.dumps(predicted,sort_keys=True).encode())+1):
            data=reader()
            if not isinstance(data,bytes) or len(data)!=size:raise ValueError('Cache input differs from admitted size')
            self._write(relative,data)

    @_writing
    def fetch_many(self,items,*,stream=False):
        """Admit once, overlap four reads, and journal each materialized file.

        Stream readers journal stage identity before downloading. Only the
        calling thread installs files in the data view. Any failure stops
        new scheduling and drains started readers before cleanup and release.
        """
        from .node_storage_budget import reserve
        from .attempt_workspace import canonical_path,MAX_FILES,MAX_BYTES
        items=list(items);seen=set();total=metadata=0
        if len(items)>MAX_FILES:raise ValueError('Cache view exceeds the file-count limit')
        for relative,size,reader in items:
            if canonical_path(relative)!=relative or relative in seen or type(size) is not int or size<0 or not callable(reader):
                raise ValueError('Invalid cache view selection')
            seen.add(relative);total+=size
            metadata+=len(json.dumps({'path':relative,'size':size,'sha256':'0'*64},sort_keys=True).encode())+1
            if stream:
                stage = _stage_record('stream-'+'0'*32+'.part',size,'0'*64)
                metadata += len(json.dumps(stage, sort_keys=True).encode()) + 1
        if total>MAX_BYTES:raise ValueError('Cache view exceeds its size limit')
        with reserve(total+metadata):
            remaining=iter(items);pending={};failure=None
            with ThreadPoolExecutor(max_workers=4,thread_name_prefix='case-input-read') as pool:
                def fill():
                    while len(pending)<4:
                        item=next(remaining,None)
                        if item is None:break
                        relative,size,reader=item
                        if stream:
                            future=pool.submit(copy_context().run,self._stage_stream,size,reader)
                        else:
                            future=pool.submit(copy_context().run,reader)
                        pending[future]=(relative,size)
                fill()
                while pending:
                    done,_=wait(pending,return_when=FIRST_COMPLETED)
                    for future in done:
                        relative,size=pending.pop(future)
                        try:
                            data=future.result()
                            if stream:
                                try:
                                    if failure is None:self._install_stage(relative,data)
                                finally:
                                    self._discard_stage(data)
                            else:
                                if not isinstance(data,bytes) or len(data)!=size:
                                    raise ValueError('Cache input differs from admitted view')
                                if failure is None:self._write(relative,data)
                        except BaseException as error:
                            if failure is None:failure=error
                    if failure is None:fill()
            if failure is not None:raise failure

    def _stage_stream(self, size, reader):
        from .file_transactions import _path
        from .request_deadline import current
        deadline = current()
        _check(deadline)
        stage = _path(self.container, 'stream-' + uuid.uuid4().hex + '.part')
        written = 0
        digest = hashlib.sha256()
        file_id = None
        try:
            with stage.open('xb') as output:
                info = os.fstat(output.fileno())
                file_id = (info.st_dev, info.st_ino)
                stage_identity = _stage_identity(output.fileno(), info)
                if stage_identity is not None:
                    self._append_record(_stage_record(stage.name, size, stage_identity))
                with reader() as chunks:
                    for chunk in chunks:
                        _check(deadline)
                        if written + len(chunk) > size:
                            raise ValueError('Cache stream exceeded its admitted size')
                        if output.write(chunk) != len(chunk):
                            raise OSError('Incomplete private cache stage write')
                        written += len(chunk)
                        digest.update(chunk)
                _check(deadline)
                if written != size:
                    raise ValueError('Cache stream differs from its admitted size')
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            if file_id is not None:
                self._discard_stage((stage, file_id, {'size':written,'sha256':digest.hexdigest()}))
            raise
        return stage, file_id, {'size':written,'sha256':digest.hexdigest()}

    def _checked_stage(self, staged, deadline=None):
        from .attempt_workspace import ordinary
        from .file_transactions import _capture
        stage, file_id, expected = staged
        info = ordinary(stage)
        if (info.st_dev, info.st_ino) != file_id or _capture(
                stage, limit=expected['size'], deadline=deadline) != expected:
            raise ValueError('Private cache stage changed')

    def _discard_stage(self, staged):
        stage, _, _ = staged
        try:
            self._checked_stage(staged)
        except FileNotFoundError:
            return  # A successfully installed stage no longer has this name.
        stage.unlink()

    def _install_stage(self, relative, staged):
        from .file_transactions import _path
        from .fs_directory import move_file_new
        from .request_deadline import current
        deadline = current()
        self._checked_stage(staged, deadline)
        target = _path(self.root, relative)
        if target.exists():
            raise ValueError('Cache input already exists')
        _check(deadline)
        stage, _, record = staged
        self._append_record({'path':relative, **record})
        move_file_new(stage, target, root=self.container)
        target.chmod(0o644)

    def _append_record(self, record):
        with self._journal_lock, (self.container / 'files.jsonl').open('ab') as stream:
            stream.write(json.dumps(record, sort_keys=True).encode() + b'\n')
            stream.flush()
            os.fsync(stream.fileno())

    @_writing
    def _write(self, relative, data):
        from .file_transactions import _path
        target = _path(self.root, relative)
        if target.exists():
            raise ValueError('Cache input already exists')
        record = {'path': relative, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        self._append_record(record)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
        target.chmod(0o644)

    def cleanup(self):
        # Wait for fetch_many's reader pool and every admitted writer before
        # allowing current-owner deletion. No future write can reopen this cache.
        with self._operation:
            self._closed=True;self._cleanup_running=True
            deadline=time.monotonic()+3
            from .request_deadline import current
            budget=current()
            if budget is not None:deadline=min(deadline,budget.deadline)
            try:
                while True:
                    try:
                        if not self.container.exists():return {'status':'removed'}
                        return _recover(self.parent,self.container,_current=self)
                    except _CleanupSharing as error:
                        if time.monotonic()>=deadline:
                            # Physical bytes and original owner/journal remain.
                            # Background recovery still requires an exited owner.
                            logger.warning('Private cache cleanup deferred (cache=%s winerror=%s)',self.container.name,error.winerror)
                            return {'status':'retained','reason':'sharing_violation','winerror':error.winerror}
                        time.sleep(min(.05,max(0,deadline-time.monotonic())))
            finally:self._cleanup_running=False

    @_writing
    def directories(self, paths):
        """Journal explicit (including empty) directories before creating them."""
        from .attempt_workspace import canonical_path, ordinary
        from .file_transactions import _path
        from .node_storage_budget import reserve
        paths = sorted(set(paths))
        if any(canonical_path(path) != path for path in paths):
            raise ValueError('Invalid private cache directory')
        encoded = b''.join(json.dumps({'path': path, 'directory': True}, sort_keys=True).encode() + b'\n' for path in paths)
        with reserve(len(encoded)):
            with (self.container / 'files.jsonl').open('ab') as journal:
                journal.write(encoded); journal.flush(); os.fsync(journal.fileno())
            for path in paths:
                target = _path(self.root, path)
                target.mkdir(parents=True, exist_ok=True)
                ordinary(target)

    @_writing
    def _write_stream(self,relative,expected,reader):
        """Caller holds whole-view node admission; journal before network data."""
        from .file_transactions import _path
        if (type(expected.get('size')) is not int or expected['size']<0 or
                not re.fullmatch('[0-9a-f]{64}',expected.get('sha256',''))):
            raise ValueError('Invalid streamed cache record')
        target=_path(self.root,relative)
        if target.exists():raise ValueError('Cache input already exists')
        record={'path':relative,'size':expected['size'],'sha256':expected['sha256']}
        with (self.container/'files.jsonl').open('ab') as journal:
            journal.write(json.dumps(record,sort_keys=True).encode()+b'\n');journal.flush();os.fsync(journal.fileno())
        target.parent.mkdir(parents=True,exist_ok=True);size=0;digest=hashlib.sha256()
        with reader() as chunks,target.open('xb') as output:
            for chunk in chunks:
                size+=len(chunk)
                if size>expected['size']:raise ValueError('Streamed cache exceeded its admitted size')
                digest.update(chunk);output.write(chunk)
            output.flush();os.fsync(output.fileno())
        if size!=expected['size'] or digest.hexdigest()!=expected['sha256']:
            raise ValueError('Streamed cache differs from the captured object')
        target.chmod(0o755 if expected.get('executable') else 0o644)


def _recover(parent, container, *, _current=None, deadline=None):
    from .file_transactions import _path
    owner = _path(parent, container.name + '/owner.json')
    _check(deadline)
    raw = _read_owner(owner)
    record = json.loads(raw)
    if (not isinstance(record, dict) or record.get('schema') != SCHEMA or record.get('kind') != parent.name or
            record.get('name') != container.name or record.get('parent') != str(parent.resolve())):
        raise ValueError('Cache ownership binding changed')
    observed = identity.probe(record.get('owner'))
    if _current is not None:
        if (not isinstance(_current,Cache) or not _current._closed or not _current._cleanup_running
                or _current.parent!=parent or _current.container!=container or _current.record!=record
                or observed['state']!='match' or record['owner']!=identity.observe(os.getpid())):
            raise ValueError('Current cache cleanup lacks its closed original owner')
    elif observed['state'] != 'exited':
        return {'status': 'active' if observed['state'] == 'match' else 'unverified_retained',
                'reason': observed['reason']}
    try:return _remove_cached_files(container,owner,raw,deadline)
    except OSError as error:
        if _current is not None and getattr(error,'winerror',None) in {32,33}:
            raise _CleanupSharing(error) from error
        raise


def _read_inventory(journal, deadline, maximum):
    from .attempt_workspace import canonical_path, MAX_BYTES

    if journal.stat().st_size > MAX_LOG:
        raise ValueError('Oversized cache inventory')
    files, stages, directories, explicit_directories = {}, {}, {'data'}, set()
    size, digest = 0, hashlib.sha256()
    with journal.open('rb') as stream:
        while True:
            _check(deadline)
            line = stream.readline(MAX_LOG + 1 - size)
            if not line:
                break
            size += len(line)
            if size > MAX_LOG or not line.endswith(b'\n'):
                raise ValueError('Oversized or incomplete cache inventory')
            digest.update(line)
            entry = json.loads(line)
            path = entry['path']
            if canonical_path(path) != path:
                raise ValueError('Noncanonical cache inventory path')
            if entry.get('stage') is True:
                if (set(entry) != {'path', 'stage', 'size', 'file_id'} or not STREAM_STAGE.fullmatch(path)
                        or path in stages or type(entry['size']) is not int or not 0 <= entry['size'] <= MAX_BYTES
                        or not re.fullmatch('[0-9a-f]{64}', entry['file_id'])):
                    raise ValueError('Invalid cache stage identity')
                stages[path] = entry
            elif entry.get('directory') is True:
                if set(entry) != {'path', 'directory'} or path in explicit_directories:
                    raise ValueError('Invalid cache directory record')
                explicit_directories.add(path)
                directories.add('data/' + path)
            else:
                if ('data/' + path in files or type(entry['size']) is not int or entry['size'] < 0
                        or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
                    raise ValueError('Invalid cache file record')
                files['data/' + path] = {'size': entry['size'], 'sha256': entry['sha256']}
            if entry.get('stage') is not True:
                parts = path.split('/')
                directories.update('data/' + '/'.join(parts[:i]) for i in range(1, len(parts)))
            if maximum is not None and len(files) + len(stages) + len(directories) > maximum:
                raise ValueError('Private cache inventory exceeds its entry limit')
    if set(files) & directories:
        raise ValueError('Cache file and directory collision')
    return files, stages, directories, {'size': size, 'sha256': digest.hexdigest()}


def _verify_stage(fd, stage, deadline):
    _check(deadline)
    info = os.fstat(fd)
    if info.st_size > stage['size'] or _stage_identity(fd, info) != stage['file_id']:
        raise ValueError('Private cache stage identity or admitted size changed')


def _remove_cached_files(container, owner, raw, deadline):
    import stat
    from .attempt_workspace import ordinary
    from .file_transactions import _path

    journal = _path(container, 'files.jsonl')
    maximum = get_settings().storage_measurement_max_entries if deadline is not None else None
    files, stages, directories, journal_digest = _read_inventory(journal, deadline, maximum)
    found, found_stages = [], []
    for directory in ['', *sorted(directories)]:
        _check(deadline)
        parent = _path(container, directory) if directory else container
        try:
            entries = os.scandir(parent)
        except FileNotFoundError:
            continue  # A previously verified cleanup can have removed part of the tree.
        with entries:
            for entry in entries:
                _check(deadline)
                path = Path(entry.path)
                relative = path.relative_to(container).as_posix()
                if stat.S_ISDIR(ordinary(path).st_mode):
                    if relative not in directories:
                        raise ValueError('Unknown cache directory')
                elif relative in stages:
                    with _deletion_handle(path) as fd:
                        _verify_stage(fd, stages[relative], deadline)
                    found_stages.append(relative)
                elif relative not in {'owner.json', 'files.jsonl'}:
                    expected = files.get(relative)
                    if expected is None or _fingerprint_path(path, expected['size'], deadline) != expected:
                        raise ValueError('Unknown or changed cache file')
                    found.append(relative)
    # Preflight the complete inventory before removing anything. Recheck every
    # file immediately before unlinking; no recursive deletion during recovery.
    if _read_owner(owner) != raw or _fingerprint_path(journal, MAX_LOG, deadline) != journal_digest:
        raise ValueError('Cache ownership changed during inspection')
    for relative in found_stages:
        path = _path(container, relative)
        try:
            with _deletion_handle(path) as fd:
                _verify_stage(fd, stages[relative], deadline)
                path.unlink()
        except FileNotFoundError:
            pass
    for relative in found:
        path = _path(container, relative)
        try:
            expected = files[relative]
            if _fingerprint_path(path, expected['size'], deadline) != expected:
                raise ValueError('Cache file changed during cleanup')
            _check(deadline)
            path.unlink()
        except FileNotFoundError:
            pass  # Another janitor may have removed the same proven file.
    for relative in sorted(directories, key=lambda value: value.count('/'), reverse=True):
        _check(deadline)
        try:
            _path(container, relative).rmdir()
        except FileNotFoundError:
            pass
    # Open DELETE-capable handles before removing either metadata file. A lock
    # on the container, journal or owner therefore leaves both receipts intact.
    with _deletion_handle(container):
        with _deletion_handle(journal) as journal_fd,_deletion_handle(owner) as owner_fd:
            if (_fingerprint(lambda size: os.read(journal_fd, size), MAX_LOG, deadline) != journal_digest
                    or os.read(owner_fd, MAX_OWNER + 1) != raw):
                raise ValueError('Cache ownership changed before metadata deletion')
            _check(deadline)
            journal.unlink();owner.unlink()
        container.rmdir()
    return {'status': 'removed'}


def _review_candidate(kind, container, deadline):
    row = {'kind': kind, 'name': container.name}
    if not re.fullmatch('owned-[0-9a-f]{32}', container.name):
        return {**row, 'status': 'untracked_retained'}
    try:
        row.update(_recover(container.parent, container, deadline=deadline))
    except FileNotFoundError:
        row['status'] = 'recovery_required' if container.exists() else 'removed'
    except TimeoutError:
        row.update(status='deferred', reason='deadline')
    except (OSError, ValueError, KeyError, TypeError) as error:
        row.update(status='recovery_required', reason=type(error).__name__)
    return row


def _review_staging_preparation(path, deadline):
    from . import staging_preparation

    row = {'kind': staging_preparation.DIRECTORY, 'name': path.name}
    try:
        row['status'] = staging_preparation.recover(path, deadline)
    except FileNotFoundError:
        row['status'] = 'recovery_required' if os.path.lexists(path) else 'removed'
    except TimeoutError:
        row.update(status='deferred', reason='deadline')
    except (OSError, ValueError, KeyError, TypeError) as error:
        row.update(status='recovery_required', reason=type(error).__name__)
    return row


class RecoverySweep:
    """One paged directory sweep. Continuations share no database transaction."""
    def __init__(self):
        from . import staging_preparation

        self.kinds = deque(sorted(KINDS | {staging_preparation.DIRECTORY}))
        self.streams = {}

    @property
    def complete(self):
        return not self.kinds

    def _close_kind(self, kind):
        stream = self.streams.pop(kind, None)
        if stream is not None:
            stream.close()

    def close(self):
        for kind in tuple(self.streams):
            self._close_kind(kind)
        self.kinds.clear()

    def page(self):
        from .request_deadline import Deadline
        from . import staging_preparation

        deadline = Deadline(time.monotonic() + get_settings().storage_measurement_timeout_seconds)
        results = []
        while self.kinds and len(results) < RECOVERY_PAGE_SIZE and not deadline.expired:
            kind = self.kinds.popleft()
            try:
                if kind not in self.streams:
                    parent = staging_preparation.parent() if kind == staging_preparation.DIRECTORY else parent_for(kind)
                    self.streams[kind] = os.scandir(parent)
                entry = next(self.streams[kind], None)
                if entry is None:
                    self._close_kind(kind)
                    continue
            except FileNotFoundError:
                self._close_kind(kind)
                continue
            except (OSError, ValueError) as error:
                self._close_kind(kind)
                results.append({'kind': kind, 'status': 'recovery_required', 'reason': type(error).__name__})
                continue
            self.kinds.append(kind)
            if kind == staging_preparation.DIRECTORY:
                results.append(_review_staging_preparation(Path(entry.path), deadline))
            else:
                results.append(_review_candidate(kind, Path(entry.path), deadline))
        return results


def recover():
    """Collect evidence for one explicit sweep, using the same paged recovery."""
    sweep = RecoverySweep()
    results = []
    try:
        while not sweep.complete:
            results.extend(sweep.page())
        return results
    finally:
        sweep.close()
