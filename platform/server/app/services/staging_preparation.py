"""Ownership records for local S3 snapshots still being prepared."""
import json
import os
import re
import stat
import time

from .. import storage
from . import process_identity_native as identity, request_deadline, staging_cleanup
from .attempt_workspace import ordinary

DIRECTORY = '.staging-preparations'
SCHEMA = 'vibeflow.staging-preparation.v1'
MAX_RECEIPT_BYTES = 4096
# A failed preparation may atomically replace its receipt before releasing the
# existing node claim. Cover both the original and the replacement file.
RECEIPT_PEAK_BYTES = 2 * MAX_RECEIPT_BYTES
_NAME = re.compile(r'job-[0-9]+-[0-9a-f]{32}')


def parent():
    from .file_transactions import _path
    return _path(storage._fs_root(), DIRECTORY)


def receipt(root):
    return parent() / (root.name + '.json')


def stage_root(stage):
    prefix, _, name = stage.partition('/')
    if prefix != '.staging' or not _NAME.fullmatch(name):
        raise ValueError('Invalid staging preparation name')
    staging = storage._fs_root().resolve() / prefix
    if os.path.lexists(staging) and not stat.S_ISDIR(ordinary(staging).st_mode):
        raise ValueError('Invalid staging preparation parent')
    return staging / name


def record(root):
    info = ordinary(root)
    if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
        raise ValueError('Invalid staging preparation directory')
    return {'schema': SCHEMA, 'name': root.name, 'directory': [info.st_dev, info.st_ino],
            'owner': identity.observe(os.getpid()), 'discarded': False}


def write(path, value):
    encoded = json.dumps(value, separators=(',', ':')).encode('utf8')
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ValueError('Oversized staging preparation receipt')
    storage._fs_put(DIRECTORY, path.name, encoded)


def registered(stage):
    # Both an active download and a failed preparation are private to cleanup.
    name = stage.rsplit('/', 1)[-1]
    return os.path.lexists(parent() / (name + '.json'))


def remove(root, path, value, check):
    check()
    try:
        info = ordinary(root)
    except FileNotFoundError:
        pass
    else:
        if [info.st_dev, info.st_ino] != value['directory']:
            raise ValueError('Staging preparation directory was replaced')
        staging_cleanup.remove_directory(root, check, expected=info)
    check()
    root.with_suffix('.manifest.json').unlink(missing_ok=True)
    check()
    path.unlink(missing_ok=True)


def discard(root, path, value, error):
    budget = request_deadline.current()
    if budget is None:
        budget = request_deadline.Deadline(time.monotonic() + request_deadline.STAGING_CLEANUP_SECONDS)
    try:
        remove(root, path, value, budget.check)
    except Exception as cleanup_error:
        error.add_note('Staging preparation cleanup: ' + type(cleanup_error).__name__)
        # No consumer has received this snapshot. A later maintenance pass may
        # finish the deletion even while the original API process is running.
        try:
            write(path, {**value, 'discarded': True})
        except Exception as receipt_error:
            error.add_note('Staging preparation receipt: ' + type(receipt_error).__name__)


def recover(path, deadline):
    deadline.check()
    if path.suffix != '.json' or not _NAME.fullmatch(path.stem):
        return 'untracked_retained'
    info = ordinary(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECEIPT_BYTES:
        raise ValueError('Invalid staging preparation receipt')
    with path.open('rb') as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    deadline.check()
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError('Oversized staging preparation receipt')
    value = json.loads(raw)
    if (set(value) != {'schema', 'name', 'directory', 'owner', 'discarded'}
            or value['schema'] != SCHEMA or value['name'] != path.stem
            or type(value['discarded']) is not bool
            or not isinstance(value['directory'], list) or len(value['directory']) != 2
            or any(type(part) is not int or part < 0 for part in value['directory'])
            or not value['directory'][1]):
        raise ValueError('Invalid staging preparation binding')
    owner = identity.validate(value['owner'])
    if owner['host_id'] != identity.host_id():
        return 'recovery_required'
    if not value['discarded']:
        state = identity.probe(owner)['state']
        if state != 'exited':
            return 'active' if state == 'match' else 'recovery_required'
    remove(stage_root('.staging/' + path.stem), path, value, deadline.check)
    return 'removed'
