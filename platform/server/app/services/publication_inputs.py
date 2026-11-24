"""Owned immutable publications selected as inputs, never their live aliases."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
import uuid

from sqlalchemy import select

from .. import storage
from ..models import ArtifactPublication, Job, JobAttempt
from . import artifact_publication as publication, attempt_workspace as workspace
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict

logger=logging.getLogger(__name__)

class PublicationInputStale(CaseWriteConflict):
    def __init__(self, message):
        super().__init__('input_stale', message)


@dataclass(frozen=True)
class PublicationSource:
    root: Path | None
    files: dict
    reference: dict
    manifest: dict | None = None


@dataclass(frozen=True)
class _DependencyShape:
    height: int
    identifiers: frozenset


def normalize(selections, *, _expanded_paths=False):
    if not isinstance(selections, list) or not 1 <= len(selections) <= 16:
        raise ValueError('input_publications must select 1-16 publications')
    result, identifiers = [], set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection)-{'publication_id', 'paths', 'require_current'}:
            raise ValueError('Invalid publication input selection')
        identifier = selection.get('publication_id')
        if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
            raise ValueError('publication_id must be a canonical UUID')
        if identifier in identifiers:
            raise ValueError('Select each publication only once')
        identifiers.add(identifier)
        current = selection.get('require_current', True)
        if not isinstance(current, bool):
            raise ValueError('require_current must be a boolean')
        paths = selection.get('paths')
        if paths is not None:
            # A public selection is bounded to 256 explicit paths. Selecting
            # an entire publication expands into its authenticated file list;
            # later validation must use the existing sealed-workspace limit.
            limit = workspace.MAX_FILES if _expanded_paths else 256
            if not isinstance(paths, list) or not 1 <= len(paths) <= limit:
                raise ValueError('Publication paths must select 1-'+str(limit)+' files')
            paths = sorted({workspace.canonical_path(path) for path in paths})
        result.append({'publication_id':identifier, 'paths':paths, 'require_current':current})
    return sorted(result, key=lambda item:item['publication_id'])


def _selected_members(manifest, paths):
    files = manifest['files']
    selected = sorted(files) if paths is None else paths
    if not selected or any(path not in files for path in selected):
        raise ValueError('Publication does not contain the selected files')
    if any(path != workspace.canonical_path(path) or path.split('/')[0].casefold() == '.vibeflow'
           for path in selected):
        raise ValueError('Publication input cannot mount execution metadata')
    return {path: files[path] for path in selected}


def inspect_files(prefix, row, paths):
    try:
        folder, manifest = publication.read_manifest(prefix, row)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PublicationInputStale('所引用的发布清单缺失或损坏') from error
    records = _selected_members(manifest, paths)
    root = folder / 'files'
    workspace.ordinary(root)
    from .local_source_reads import observe
    with observe(root,records):
        for path, expected in records.items():
            target = workspace.checked_path(root, path)
            actual = workspace.digest_file(target)
            if any(actual[key] != expected[key] for key in ('size','sha256')):
                raise PublicationInputStale('所引用的产物文件已损坏，请重新生成')
    return root, records, manifest


def _verify_files(prefix, row, paths):
    if not storage._use_s3():
        return inspect_files(prefix, row, paths)
    from .publication_objects import verified_manifest
    try:
        manifest = verified_manifest(prefix, row)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PublicationInputStale('所引用的发布清单或文件缺失或损坏') from error
    return None, _selected_members(manifest, paths), manifest


async def resolve(db, case, selections, *, materialize=True, _ancestors=(),
                  _validated_current=None, _expanded_paths=False):
    """Caller holds case control, or uses a reserved PublicationReadSet.

    The latter must replay all database observations in the final transaction
    before publishing. Use materialize=False when only records and references
    are needed. A detached read alone never grants commit authority.
    """
    # A dependency diamond must check each current publication's source once
    # per traversal. Keep this state local: a later resolve revalidates sources.
    # File integrity, ownership and the active head are still checked for every
    # selection, including different path subsets of an already visited node.
    if _validated_current is None:
        _validated_current = {}
    inspect = inspect_files if materialize else _verify_files
    result = []
    for selection in normalize(selections, _expanded_paths=_expanded_paths):
        if selection['publication_id'] in _ancestors or len(_ancestors)>=16:
            raise PublicationInputStale('发布依赖存在循环或超过最大深度')
        row = await db.get(ArtifactPublication, selection['publication_id'], populate_existing=True)
        if row is None or row.case_id != case.id:
            # Same response for unknown identifiers and foreign ownership.
            raise ValueError('Publication input is unavailable for this case')
        job = await db.scalar(select(Job).join(JobAttempt, Job.id == JobAttempt.job_id)
            .where(JobAttempt.id == row.attempt_id))
        if row.status not in {'committed','superseded'} or job is None or job.case_id != case.id or job.status != 'succeeded':
            raise PublicationInputStale('所引用的成果尚未通过发布验证')
        try:
            root, files, manifest = await finish_before_cancel(asyncio.to_thread(
                inspect, case.s3_prefix, row, selection['paths']))
        except (OSError, KeyError) as error:
            raise PublicationInputStale('所引用的发布清单或文件缺失') from error
        freshness_key = (row.id, row.manifest_sha256, selection['require_current'])
        shape = _validated_current.get(freshness_key)
        if shape is not None and (len(_ancestors) + shape.height > 16
                or shape.identifiers.intersection(_ancestors)):
            raise PublicationInputStale('发布依赖存在循环或超过最大深度')
        if selection['require_current']:
            _, active = await publication.current(db, case.id, row.kind)
            if active is None or active.id != row.id:
                raise PublicationInputStale('所引用的成果不是当前版本，请刷新后重新提交')
            if shape is None:
                from .step_handlers import publication_handler,kind_for
                handler = publication_handler(manifest['descriptor'])
                if row.kind != kind_for(handler,manifest['descriptor']):
                    raise ValueError('Publication kind has no matching freshness validator')
                try:
                    validate_publication=getattr(handler,'validate_publication',None)
                    if validate_publication is not None:
                        await validate_publication(db,case,manifest['descriptor'])
                    from .local_source_reads import read_source
                    observed = await finish_before_cancel(asyncio.to_thread(read_source,handler,
                        case.s3_prefix, manifest['descriptor']))
                except TimeoutError:
                    raise
                except (OSError, ValueError, KeyError) as error:
                    logger.warning('Current publication input inspection failed (case=%s publication=%s)',
                        case.id,row.id,exc_info=True)
                    raise PublicationInputStale('所引用成果的当前输入不可校验') from error
                if observed != manifest['descriptor']['source']:
                    raise PublicationInputStale('所引用的成果已过期，请重新生成')
                dependencies = manifest['descriptor'].get('input_publications')
                children = []
                if dependencies:
                    children = await resolve(db, case, dependencies, _ancestors=(*_ancestors, row.id),
                        _validated_current=_validated_current, materialize=False)
                shapes = [_validated_current[(item.reference['publication_id'],
                    item.reference['manifest_sha256'], item.reference['require_current'])] for item in children]
                _validated_current[freshness_key] = _DependencyShape(
                    1 + max((item.height for item in shapes), default=0),
                    frozenset({row.id}).union(*(item.identifiers for item in shapes)))
        else:
            # Historical references terminate traversal, but their identifiers
            # still participate in ancestor-cycle and maximum-depth checks.
            _validated_current[freshness_key] = _DependencyShape(1, frozenset({row.id}))
        result.append(PublicationSource(root, files, {
            'publication_id':row.id, 'kind':row.kind, 'generation':row.generation,
            'attempt_id':row.attempt_id, 'manifest_sha256':row.manifest_sha256,
            'paths':sorted(files), 'require_current':selection['require_current']}, manifest))
    return result


async def validate(db, case, manifest):
    references = manifest.get('publications') or []
    if not references:
        return
    try:
        resolved = await resolve(db, case, [{key:reference[key] for key in
            ('publication_id','paths','require_current')} for reference in references],
            materialize=False, _expanded_paths=True)
        if [item.reference for item in resolved] != references:
            raise PublicationInputStale('发布来源与封存记录不一致')
        for item in resolved:
            if any(manifest['files'].get(path) != record for path, record in item.files.items()):
                raise PublicationInputStale('封存文件与所引用的产物清单不一致')
    except (OSError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, PublicationInputStale):
            raise
        raise PublicationInputStale('发布依赖缺失、损坏或已失效') from error
