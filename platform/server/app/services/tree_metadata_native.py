"""Portable Linux mode metadata applied to an actual private execution copy.

Stdlib only: this file is also invoked by the WSL sandbox launcher. It never
changes permissions outside the supplied ordinary directory tree.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import sys

PATH = '.vibeflow/tree-state.json'
SCHEMA = 'vibeflow.case-tree-metadata.v1'
MAX_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 10000


def name(value):
    if (not isinstance(value, str) or not value or '\\' in value or
            PurePosixPath(value).is_absolute() or PurePosixPath(value).as_posix() != value or
            value.split('/')[0].casefold() == '.vibeflow' or len(value.split('/')) > 32):
        raise ValueError('Invalid tree metadata path')
    for part in value.split('/'):
        if (part in {'.', '..'} or part.endswith((' ', '.')) or PureWindowsPath(part).is_reserved()
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part) or len(part.encode('utf8')) > 255):
            raise ValueError('Invalid tree metadata component')
    return value


def validate(document):
    if document.get('schema') != SCHEMA or type(document.get('materialize_directories')) is not bool:
        raise ValueError('Unknown tree metadata')
    entries = document['entries']
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise ValueError('Tree metadata exceeds its entry bound')
    seen = set()
    for path, entry in entries.items():
        name(path)
        if path.casefold() in seen:
            raise ValueError('Tree metadata contains path aliases')
        seen.add(path.casefold())
        if entry['kind'] not in {'file', 'directory'} or type(entry['mode']) is not int or not 0 <= entry['mode'] <= 0o7777:
            raise ValueError('Invalid tree entry type or mode')
        if entry['kind'] == 'file':
            if type(entry['size']) is not int or entry['size'] < 0 or len(entry['sha256']) != 64:
                raise ValueError('Invalid tree file fingerprint')
            int(entry['sha256'], 16)
        for parent in PurePosixPath(path).parents:
            if str(parent) != '.' and entries.get(str(parent), {}).get('kind') != 'directory':
                raise ValueError('Tree metadata has a missing or non-directory parent')
    return document


def decode(data):
    if len(data) > MAX_BYTES:
        raise ValueError('Oversized tree metadata')
    return validate(json.loads(data))


def ordinary(path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or
            not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or
            stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
        raise ValueError('Tree metadata refuses links and special files')
    return info


def checked(root, relative):
    current = root
    ordinary(current)
    for part in relative.split('/'):
        current /= part
        try:
            ordinary(current)
        except FileNotFoundError:
            pass
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError('Tree metadata escaped its root')
    return current


def fingerprint(path):
    h, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk); size += len(chunk)
            if size > 128*1024*1024:
                raise ValueError('Tree file exceeds snapshot bound')
    return {'size': size, 'sha256': h.hexdigest()}


def apply(root):
    root = Path(root)
    source = checked(root, PATH)
    if not source.exists():
        return
    with source.open('rb') as stream:
        document = decode(stream.read(MAX_BYTES+1))
    entries = document['entries']
    if document['materialize_directories']:
        for path in sorted(entries, key=lambda p: (p.count('/'), p)):
            if entries[path]['kind'] == 'directory':
                checked(root, path).mkdir(exist_ok=True)
    # Parents remain traversable until all children are checked and restored.
    for path in sorted(entries, key=lambda p: (-p.count('/'), p)):
        entry = entries[path]
        target = checked(root, path)
        if not target.exists():
            continue
        info = ordinary(target)
        if entry['kind'] == 'directory':
            if not stat.S_ISDIR(info.st_mode):
                continue
        elif not stat.S_ISREG(info.st_mode) or fingerprint(target) != {k: entry[k] for k in ('size', 'sha256')}:
            continue
        os.chmod(target, entry['mode'], follow_symlinks=False)


if __name__ == '__main__':
    apply(sys.argv[1])
