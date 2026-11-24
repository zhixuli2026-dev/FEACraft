"""Offline eviction of a verified, reconstructible S3 publication cache."""

import hashlib
import os
from pathlib import PurePosixPath
import stat
import time

from sqlalchemy import select

from .. import storage
from ..models import ArtifactPublication, Case, Job, JobAttempt
from . import artifact_publication, publication_objects, attempt_workspace
from .durable_events import json_text
from .fs_directory import move_new


async def load_target(db, case_id, publication_id):
    from .artifact_publication import PublicationIdentity

    row = await db.get(ArtifactPublication, publication_id)
    case = await db.get(Case, case_id)
    if case is None or row is None or row.case_id != case_id or row.status not in {'committed', 'superseded'}:
        raise ValueError('Select a published artifact of the requested case')
    job = await db.scalar(select(Job).join(JobAttempt, Job.id == JobAttempt.job_id)
        .where(JobAttempt.id == row.attempt_id))
    if job is None or job.case_id != case_id or job.status != 'succeeded':
        raise ValueError('Publication has no successful owned job')
    return case.s3_prefix, PublicationIdentity.capture(row)


def _signature(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _file(path, expected):
    before = attempt_workspace.ordinary(path)
    if not stat.S_ISREG(before.st_mode) or before.st_size != expected['size']:
        raise ValueError('Unexpected file in the publication cache')
    if (attempt_workspace.digest_file(path) != expected
            or _signature(attempt_workspace.ordinary(path)) != _signature(before)):
        raise ValueError('Publication cache file changed or differs from its archive')
    return _signature(before)


def _inventory(folder, row, manifest, *, partial):
    expected = {'manifest.json': {'size': len(publication_objects.encoded(manifest)),
                                 'sha256': row.manifest_sha256}}
    expected.update({'files/' + name: {key: value[key] for key in ('size', 'sha256')}
                     for name, value in manifest['files'].items()})
    directories = {'.', 'files'}
    for name in expected:
        directories.update(parent.as_posix() for parent in PurePosixPath(name).parents)
    found, found_dirs = {}, {}
    pending = [folder]
    while pending:
        directory = pending.pop()
        info = attempt_workspace.ordinary(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('Publication cache directory was replaced')
        found_dirs[directory.relative_to(folder).as_posix()] = _signature(info)
        for path in directory.iterdir():
            name = path.relative_to(folder).as_posix()
            if name in directories:
                pending.append(path)
            elif name in expected:
                found[name] = _file(path, expected[name])
            else:
                raise ValueError('Unknown publication cache member; retain the entire directory')
    if not partial and (found.keys() != expected.keys() or found_dirs.keys() != directories):
        raise ValueError('Incomplete publication cache; retain it for inspection')
    return found, found_dirs


def review(prefix, row, *, retention_days=30, approved=None):
    if not storage._use_s3() or type(retention_days) is not int or not 0 <= retention_days <= 3650:
        raise ValueError('Publication cache maintenance requires S3 and a valid retention period')
    relative = publication_objects.identity(row)
    root = storage._fs_path(prefix, '.')
    target = root / relative
    parked = target.with_name('.evicting-' + row.id)
    if not os.path.lexists(target) and not os.path.lexists(parked):
        if approved is not None:
            raise ValueError('The reviewed publication cache is no longer present')
        return {'state': 'absent', 'candidate_bytes': 0, 'publication_id': row.id}
    attempt_workspace.checked_path(root, artifact_publication.PREFIX)
    # Drain this node before applying. Other nodes with independent cache roots
    # can continue using the authoritative SQL/S3 publication.
    partial = os.path.lexists(parked)
    folder = parked if partial else target
    manifest = publication_objects.verified_manifest(prefix, row)
    files, directories = _inventory(folder, row, manifest, partial=partial)
    if not partial and files['manifest.json'][4] > time.time_ns() - retention_days * 86_400 * 10**9:
        if approved is not None:
            raise ValueError('The cache has been refreshed since review')
        return {'state': 'retained', 'candidate_bytes': 0, 'publication_id': row.id}
    binding = [str(folder), vars(row), manifest['storage'], retention_days, files, directories]
    digest = hashlib.sha256(json_text(binding).encode('utf-8')).hexdigest()
    size = sum(signature[3] for signature in files.values())
    result = {'state': 'candidate', 'candidate_bytes': size, 'publication_id': row.id,
              'review_sha256': digest, 'retention_days': retention_days, 'interrupted': partial}
    if approved is None:
        return result
    if approved != digest:
        raise ValueError('The local cache differs from the approved review')
    if not partial:
        move_new(target, parked, root=root)
    for name, signature in sorted(files.items()):
        path = attempt_workspace.checked_path(parked, name)
        if _signature(attempt_workspace.ordinary(path)) != signature:
            raise ValueError('Publication cache changed during eviction')
        path.unlink()
    for name in sorted(directories, key=lambda item: (len(PurePosixPath(item).parts), item), reverse=True):
        directory = parked / name
        before = directories[name]
        after = _signature(attempt_workspace.ordinary(directory))
        if before[:3] != after[:3]:
            raise ValueError('Publication cache directory changed during eviction')
        directory.rmdir()
    return {**result, 'state': 'evicted', 'evicted_bytes': size}
