"""Prepare published-alias inventories outside SQL under a storage reservation."""
from dataclasses import dataclass

from sqlalchemy import select
from ..models import ArtifactPublication, ArtifactPublicationHead, JobAttempt
from . import artifact_publication as publication, storage_operations
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
import asyncio


@dataclass(frozen=True)
class AttemptSnapshot:
    job_id: int
    publication_json: str
    output_manifest_json: str


@dataclass(frozen=True)
class HeadSnapshot:
    kind: str
    version: int
    next_generation: int
    active_publication_id: str | None
    publication: publication.PublicationIdentity | None
    status: str | None
    attempt: AttemptSnapshot | None


async def capture(db, case_id):
    heads = await db.scalars(select(ArtifactPublicationHead)
        .where(ArtifactPublicationHead.case_id == case_id)
        .order_by(ArtifactPublicationHead.kind).execution_options(populate_existing=True))
    result = []
    for head in heads:
        row = await db.get(ArtifactPublication, head.active_publication_id,
                           populate_existing=True) if head.active_publication_id else None
        attempt = await db.get(JobAttempt, row.attempt_id, populate_existing=True) if row else None
        result.append(HeadSnapshot(head.kind, head.version, head.next_generation,
            head.active_publication_id, publication.PublicationIdentity.capture(row) if row else None,
            row.status if row else None, AttemptSnapshot(attempt.job_id, attempt.publication_json,
                attempt.output_manifest_json) if attempt else None))
    return tuple(result)


def read_paths(prefix, heads):
    result = []
    for head in heads:
        if head.publication is None:
            continue
        try:
            manifest = publication.manifest_for_alias_paths(prefix, head.publication)
            paths = set(manifest['files']) | set(manifest['descriptor'].get('generated_paths', []))
        except (OSError, ValueError, KeyError):
            paths = publication.fallback_managed_paths(head.publication, head.attempt)
        result.append((head.kind, frozenset(paths)))
    return tuple(result)


@dataclass(frozen=True)
class PreparedAliases:
    lease: object
    heads: tuple
    paths: tuple

    async def validate_locked(self, db, case):
        storage_operations.require_context(self.lease)
        await self.lease.require_locked(db)
        if ((case.id, case.s3_prefix) != (self.lease.case.id, self.lease.case.s3_prefix)
                or await capture(db, case.id) != self.heads):
            raise CaseWriteConflict('input_stale', '产物发布记录已变化，请重新同步保留的副本')

    def paths_for(self, kind):
        return dict(self.paths)[kind]


async def prepare(lease):
    storage_operations.require_context(lease)
    async with lease.control() as db:
        heads = await capture(db, lease.case.id)
    paths = await finish_before_cancel(asyncio.to_thread(read_paths, lease.case.s3_prefix, heads))
    await lease.checkpoint()
    return PreparedAliases(lease, heads, paths)
