"""Bounded removal of a stopped writer's legacy staging directory."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import time

from .. import storage
from . import request_deadline
from .fs_directory import retry_sharing_violation


def intent(stage, synchronized):
    return {'schema': 'vibeflow.terminal-cleanup.v1', 'stage_prefix': stage,
            'intent': 'cleanup_only', 'source_synchronized': synchronized}


def _attributes(info):
    return getattr(info, 'st_file_attributes', 0)


def _is_directory(info):
    return stat.S_ISDIR(info.st_mode) and not _attributes(info) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def _directory(path):
    info = path.lstat()
    if not _is_directory(info):
        raise ValueError('Staging cleanup requires an ordinary directory')
    return info


def _same_directory(original, current):
    if not _is_directory(current) or not os.path.samestat(original, current):
        raise OSError('Staging directory changed during cleanup')


@contextmanager
def _open_directory(path, parent, expected):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _same_directory(expected, os.fstat(fd))
        yield fd
    finally:
        os.close(fd)


def _empty_fd(fd, check):
    check()
    with os.scandir(fd) as children:
        for child in children:
            check()
            info = child.stat(follow_symlinks=False)
            check()
            if _is_directory(info):
                with _open_directory(child.name, fd, info) as subdir:
                    _empty_fd(subdir, check)
                current = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                _same_directory(info, current)
                check()
                os.rmdir(child.name, dir_fd=fd)
            else:
                os.unlink(child.name, dir_fd=fd)
            check()


def _empty_windows(path, info, check):
    check()
    _same_directory(info, _directory(path))
    check()
    with os.scandir(path) as children:
        for child in children:
            check()
            _same_directory(info, _directory(path))
            selected = Path(child.path)
            entry = selected.lstat()
            check()
            if _is_directory(entry):
                _empty_windows(selected, entry, check)
                _same_directory(info, _directory(path))
                _same_directory(entry, _directory(selected))
                check()
                selected.rmdir()
            elif (_attributes(entry) & stat.FILE_ATTRIBUTE_DIRECTORY):
                # Windows directory junctions are removed as links.
                selected.rmdir()
            else:
                selected.unlink()
            check()


def remove_directory(path, check, *, expected=None):
    check()
    try:
        info = _directory(path)
    except FileNotFoundError:
        return
    if expected is not None:
        _same_directory(expected, info)
    check()
    if os.name == 'nt':
        _empty_windows(path, info, check)
    else:
        with _open_directory(path, None, info) as fd:
            _empty_fd(fd, check)
    _same_directory(info, _directory(path))
    check()
    path.rmdir()
    check()


def clean(stage, *, synchronized, retry_seconds=None):
    if not re.fullmatch(r'\.staging/job-[0-9]+-[0-9a-f]{32}', stage):
        raise ValueError('Invalid staging cleanup identity')
    inherited = request_deadline.current()
    end = time.monotonic() + request_deadline.STAGING_CLEANUP_SECONDS
    budget = request_deadline.Deadline(min(end, inherited.deadline) if inherited else end)
    check = budget.check
    check()
    parent = storage._fs_root().resolve() / '.staging'
    try:
        original = _directory(parent)
    except FileNotFoundError:
        return
    root = parent / stage.split('/')[1]
    manifest = root.with_suffix('.manifest.json')
    marker = root.with_suffix('.terminal-cleanup.json')
    expected = intent(stage, synchronized)

    def remove():
        check()
        _same_directory(original, _directory(parent))
        if os.path.lexists(marker):
            info = marker.lstat()
            if (_attributes(info) & stat.FILE_ATTRIBUTE_REPARSE_POINT or
                    not stat.S_ISREG(info.st_mode) or info.st_size > 4096):
                raise ValueError('Invalid staging cleanup marker')
            with marker.open('rb') as stream:
                existing = stream.read(4097)
            if len(existing) > 4096 or json.loads(existing) != expected:
                raise ValueError('Staging cleanup marker belongs to another operation')
        elif os.path.lexists(root) or os.path.lexists(manifest):
            check()
            storage._fs_put('.staging', marker.name, json.dumps(expected).encode('utf8'))
        else:
            return
        remove_directory(root, check)
        _same_directory(original, _directory(parent))
        check()
        manifest.unlink(missing_ok=True)
        check()
        marker.unlink(missing_ok=True)
        check()

    if retry_seconds is None:
        retry_sharing_violation(remove)
        return
    retry_until = min(time.monotonic() + retry_seconds, budget.deadline)
    while True:
        try:
            remove()
            return
        except TimeoutError:
            raise
        except OSError:
            check()
            remaining = retry_until - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(.05, remaining))
