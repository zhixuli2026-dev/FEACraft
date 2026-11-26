"""Read versioned FloTHERM helper builds from their immutable publications."""
from contextlib import contextmanager
from datetime import timezone
from pathlib import PurePosixPath

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from ..models import ArtifactPublication, ArtifactPublicationHead, Job, JobAttempt
from .artifact_publication import PublicationIdentity
from .file_read import ArchivedPublication
from .flotherm_convert_artifacts import relative


def _query(case_id):
    return (select(ArtifactPublication, Job.id, ArtifactPublicationHead.active_publication_id)
            .join(JobAttempt, JobAttempt.id == ArtifactPublication.attempt_id)
            .join(Job, Job.id == JobAttempt.job_id)
            .outerjoin(ArtifactPublicationHead, and_(
                ArtifactPublicationHead.case_id == ArtifactPublication.case_id,
                ArtifactPublicationHead.kind == ArtifactPublication.kind))
            .where(ArtifactPublication.case_id == case_id, Job.case_id == case_id,
                   ArtifactPublication.kind.startswith('flotherm-library:'),
                   ArtifactPublication.status.in_(('committed', 'superseded')), Job.status == 'succeeded'))


def _summary(row, job_id, active):
    published = row.committed_at or row.created_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return {'id': row.id, 'generation': row.generation, 'job_id': job_id,
            'published_at': published.isoformat(), 'current': active == row.id}


async def _selection(db, case_id, identifier):
    result = (await db.execute(_query(case_id).where(ArtifactPublication.id == identifier))).one_or_none()
    if result is None:
        raise HTTPException(404, '辅助库发布版本不存在')
    return result


async def list_versions(db, case_id, before, limit):
    statement = _query(case_id)
    if before:
        anchor, _, _ = await _selection(db, case_id, before)
        statement = statement.where(or_(
            ArtifactPublication.created_at < anchor.created_at,
            and_(ArtifactPublication.created_at == anchor.created_at, ArtifactPublication.id < before)))
    rows = (await db.execute(statement.order_by(
        ArtifactPublication.created_at.desc(), ArtifactPublication.id.desc()).limit(limit + 1))).all()
    return {'items': [_summary(*record) for record in rows[:limit]],
            'next': rows[limit - 1][0].id if len(rows) > limit else None}


async def version(db, case_id, identifier):
    row, job, active = await _selection(db, case_id, identifier)
    return PublicationIdentity.capture(row), _summary(row, job, active)


def _published_files(manifest):
    descriptor = manifest['descriptor']
    destination = relative(descriptor['destination_dir'])
    receipt = descriptor['receipt']
    native = receipt['native']
    libraries = native['libraries']
    if (receipt['schema'] != 'vibeflow.flotherm-library-publication.v1'
            or receipt['case_id'] != manifest['case_id']
            or receipt['attempt_id'] != manifest['attempt_id']
            or native['destination_dir'] != destination or not libraries):
        raise ValueError('Invalid FloTHERM library publication')
    selected = {}
    for path, entry in libraries.items():
        record = manifest['files'][relative(path)]
        if PurePosixPath(path).suffix != '.so' or entry != {key:record[key] for key in ('size', 'sha256')}:
            raise ValueError('Published FloTHERM library differs from its load receipt')
        selected[path] = 'library'
    for step, role in (('build', 'build_log'), ('load', 'load_log')):
        path = f'{destination}/flotherm-{step}.log'
        record = manifest['files'][path]
        if native['steps'][step]['log'] != {key:record[key] for key in ('size', 'sha256')}:
            raise ValueError('Published FloTHERM log differs from its build receipt')
        selected[path] = role
    return destination, native['openfoam'], selected


def details(prefix, row, summary):
    with ArchivedPublication(prefix, row) as archive:
        destination, openfoam, selected = _published_files(archive.manifest)
        if archive.manifest['descriptor']['receipt']['job_id'] != summary['job_id']:
            raise ValueError('Published FloTHERM library belongs to another job')
        files = [{'path': path, 'role': role, 'size': archive.manifest['files'][path]['size'],
                  'sha256': archive.manifest['files'][path]['sha256']}
                 for path, role in selected.items()]
        return {**summary, 'destination_dir': destination, 'openfoam': openfoam, 'files': files}


@contextmanager
def artifact(prefix, row, path):
    with ArchivedPublication(prefix, row) as archive:
        _, _, selected = _published_files(archive.manifest)
        if path not in selected:
            raise HTTPException(404, '所选辅助库版本不包含此文件')
        yield archive.member(path)
