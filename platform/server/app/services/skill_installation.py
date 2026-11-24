"""Recover a skill publication under the same lock used by readers and writers."""
from contextlib import contextmanager
import json
import logging
import os
import re
import stat
import time

from . import skill_store as store
from .case_write import CaseWriteConflict
from .file_transactions import _sync, _write_new
from .fs_directory import move_new

JOURNAL = '.skill-install.json'
MAX_JOURNAL_BYTES = 4096
_SCHEMA = 'vibeflow.skill-install.v1'
_STAGE = re.compile(r'\.install-[a-z0-9_]{8,32}')
_BACKUP = re.compile(r'\.([a-z0-9][a-z0-9-]{0,63})\.bak-[0-9]{1,24}')
log = logging.getLogger(__name__)


def _conflict():
    return CaseWriteConflict('skill_recovery_required', '技能版本尚待恢复，请保留现有文件')


def _directory_id(path):
    try:
        info = store._ordinary(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
        raise _conflict()
    return [info.st_dev, info.st_ino]


def _file(path):
    if not stat.S_ISREG(store._ordinary(path).st_mode):
        raise _conflict()


@contextmanager
def _locked(root):
    path = root / '.skill-publish.lock'
    if os.path.lexists(path):
        _file(path)
    with path.open('a+b') as stream:
        _file(path)
        if os.name == 'nt':
            import msvcrt
            # The CRT supports locking past EOF. The zero-length lock file
            # contributes no bytes to an account's skill-storage measurement.
            lock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        end = time.monotonic() + 3
        while True:
            store._check_deadline()
            stream.seek(0)
            try:
                lock()
                break
            except (BlockingIOError, PermissionError) as error:
                if time.monotonic() >= end:
                    raise CaseWriteConflict('skill_storage_busy', '技能目录正在更新，请稍后重试') from error
                time.sleep(.025)
        yield  # Closing the handle releases the OS lock, including on process exit.


@contextmanager
def scope(root, *, name=None, writing=False, create=False):
    if not os.path.lexists(root) and not create:
        yield
        return
    base = root.parents[1]
    for path in (base, root.parent, root):
        if create:
            path.mkdir(exist_ok=True, parents=path == base)
        if _directory_id(path) is None:
            raise _conflict()
    with _locked(root):
        cleared = _recover(root)
        if writing and not cleared:
            raise _conflict()
        _recover_legacy(root, name)
        yield


def _read(journal):
    _file(journal)
    with journal.open('rb') as stream:
        raw = stream.read(MAX_JOURNAL_BYTES + 1)
    if len(raw) > MAX_JOURNAL_BYTES:
        raise _conflict()
    try:
        value = json.loads(raw)
        if (set(value) != {'schema', 'name', 'stage', 'stage_id', 'old', 'new'}
                or value['schema'] != _SCHEMA
                or not store.NAME_RE.fullmatch(value['name'])
                or not _STAGE.fullmatch(value['stage'])):
            raise _conflict()
        for key in ('stage_id', 'old', 'new'):
            parts = value[key]
            if key == 'old' and parts is None:
                continue
            if (not isinstance(parts, list) or len(parts) != 2
                    or any(type(part) is not int or part < 0 for part in parts) or not parts[1]):
                raise _conflict()
    except (TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise _conflict() from error
    return value


def _recover(root):
    journal = root / JOURNAL
    if not os.path.lexists(journal):
        return True
    value = _read(journal)
    stage, target = root / value['stage'], root / value['name']
    previous = stage / 'previous'
    control = _directory_id(stage)
    if control is not None and control != value['stage_id']:
        raise _conflict()
    current, backup = _directory_id(target), _directory_id(previous)
    old, new = value['old'], value['new']
    if backup is not None and backup != old:
        raise _conflict()
    if current is None and old is not None:
        if backup != old:
            raise _conflict()
        move_new(previous, target, root=root)
        _sync(root)
    elif current not in (old, new) or (backup is not None and current == old):
        raise _conflict()
    # The published directory is now settled even if a reader is still
    # holding an old file handle and delaying removal of the hidden stage.
    try:
        if control is not None:
            store._remove_owned_directory(stage, root)
        journal.unlink()
        _sync(root)
    except OSError as error:
        log.warning('Skill install cleanup deferred: %s', type(error).__name__)
        return False
    return True


def _recover_legacy(root, selected):
    if selected is not None and (
        not store.NAME_RE.fullmatch(selected) or os.path.lexists(root / selected)
    ):
        return
    pattern = f'.{selected}.bak-*' if selected is not None else '.*.bak-*'
    candidates = {}
    for path in root.glob(pattern):
        match = _BACKUP.fullmatch(path.name)
        if match and not os.path.lexists(root / match[1]):
            candidates.setdefault(match[1], []).append(path)
    for name, paths in candidates.items():
        try:
            if len(paths) != 1 or _directory_id(paths[0]) is None:
                raise _conflict()
            _file(paths[0] / 'SKILL.md')
            move_new(paths[0], root / name, root=root)
            _sync(root)
        except (FileNotFoundError, ValueError, OSError) as error:
            if selected is not None:
                raise _conflict() from error
            log.warning('Existing skill backup retained: %s', type(error).__name__)


def publish(root, stage, prepared, name, overwrite):
    """Publish the fully extracted skill while holding its scope lock."""
    target = root / name
    old = _directory_id(target)
    if old is not None and not overwrite:
        raise FileExistsError(name)
    value = {'schema': _SCHEMA, 'name': name, 'stage': stage.name,
             'stage_id': _directory_id(stage), 'old': old, 'new': _directory_id(prepared)}
    intent = stage / 'intent.json'
    _write_new(intent, json.dumps(value, separators=(',', ':')).encode('utf8'))
    os.replace(intent, root / JOURNAL)
    _sync(root)
    try:
        if old is not None:
            os.replace(target, stage / 'previous')
        os.replace(prepared, target)
        _sync(root)
    except BaseException as error:
        try:
            _recover(root)
        except Exception as recovery_error:
            error.add_note('Skill install recovery: ' + type(recovery_error).__name__)
        raise
    _recover(root)
    return target
