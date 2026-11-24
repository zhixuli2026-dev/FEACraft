"""Read-only current artifact graph and recomputation advice.

Execution history stays immutable. This observation never authorizes execution:
admission and finalization must still validate their reserved inputs. No DB
transaction spans filesystem/native/S3 validation. Coordinated edits detected
between the two DB observations make the whole response a conflict.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from .. import storage
from ..models import ArtifactPublication, ArtifactPublicationHead, CaseEventCounter, Job, JobAttempt
from . import artifact_publication as publication, attempt_workspace as workspace
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .publication_inputs import normalize, resolve
from .publication_read_set import PublicationReadSet
from .workflow_state import ExecutionConflict

logger = logging.getLogger(__name__)
CORE = ('geometry', 'mesh', 'solve', 'postprocess', 'report')
MAX_NODES = 128


async def checkpoint(db, case):
    heads = list(await db.scalars(select(ArtifactPublicationHead)
        .where(ArtifactPublicationHead.case_id == case.id).order_by(ArtifactPublicationHead.kind).limit(MAX_NODES + 1)))
    if len(heads) > MAX_NODES:
        raise CaseWriteConflict('dependency_limit', 'Too many publication channels for one dependency inspection')
    counter = await db.get(CaseEventCounter, case.id, populate_existing=True)
    return {'case_id': case.id, 'workspace_id': case.workspace_id, 'prefix': case.s3_prefix,
        'revision': case.dict_revision, 'event_sequence': counter.next_seq if counter else None,
        'heads': [{'kind': h.kind, 'publication_id': h.active_publication_id,
                   'version': h.version, 'next_generation': h.next_generation} for h in heads]}


def inspect_bundle(prefix, row):
    if storage._use_s3():
        from .publication_objects import verified_manifest
        return verified_manifest(prefix, row)
    folder, manifest = publication.read_manifest(prefix, row)
    # A typed command may legitimately publish zero files. Unlike a mounted
    # input selection this is a valid empty publication, not a missing input.
    for path, record in manifest['files'].items():
        if path != workspace.canonical_path(path):
            raise ValueError('Invalid publication artifact path')
        actual = workspace.digest_file(workspace.checked_path(folder / 'files', path))
        if any(actual[key] != record[key] for key in ('size', 'sha256')):
            raise ValueError('Publication artifact integrity mismatch')
    return manifest


class Inspector:
    def __init__(self, case, read_set, run_dir):
        self.case, self.db, self.run_dir = case, read_set, run_dir
        self.nodes = {}
        self.manifests = {}

    async def visit(self, identifier, ancestors=()):
        if identifier in ancestors or len(ancestors) >= 16:
            return {'status': 'unverifiable', 'reason': 'dependency_cycle_or_depth'}
        if identifier in self.nodes:
            return self.nodes[identifier]
        if len(self.nodes) >= MAX_NODES:
            raise CaseWriteConflict('dependency_limit', 'Dependency graph exceeds the inspection limit')
        row = await self.db.get(ArtifactPublication, identifier)
        if row is None or row.case_id != self.case.id:
            return {'status': 'unverifiable', 'reason': 'publication_unavailable'}
        node = {'publication_id': row.id, 'kind': row.kind, 'generation': row.generation,
                'status': 'unverifiable', 'reasons': [], 'dependencies': []}
        self.nodes[identifier] = node
        job = await self.db.scalar(select(Job).join(JobAttempt, Job.id == JobAttempt.job_id)
            .where(JobAttempt.id == row.attempt_id))
        if row.status not in {'committed', 'superseded'} or job is None or job.case_id != self.case.id or job.status != 'succeeded':
            node['reasons'].append({'code': 'publication_not_verified'})
            return node
        try:
            manifest = await finish_before_cancel(asyncio.to_thread(inspect_bundle, self.case.s3_prefix, row))
            descriptor = manifest['descriptor']
            from .step_handlers import publication_handler, kind_for
            handler = publication_handler(descriptor)
            if kind_for(handler, descriptor) != row.kind:
                raise ValueError('Publication channel differs from its handler')
            dependencies = normalize(descriptor['input_publications']) if descriptor.get('input_publications') else []
            self.manifests[identifier] = manifest
        except (OSError, ValueError, KeyError, TypeError, ExecutionConflict):
            logger.warning('Cannot inspect dependency bundle (case=%s publication=%s)', self.case.id, row.id, exc_info=True)
            node['reasons'].append({'code': 'bundle_unverifiable'})
            return node
        node['handler'] = handler.key
        node['mode'] = descriptor.get('mode')
        node['run_dir'] = descriptor.get('run_dir')
        node['subdomains'] = descriptor.get('subdomains')
        node['name'] = descriptor.get('name')
        node['status'] = 'fresh'
        head = await self.db.get(ArtifactPublicationHead, (self.case.id, row.kind))
        if head is None or head.active_publication_id != row.id or row.status != 'committed':
            node['status'] = 'stale'
            node['reasons'].append({'code': 'not_current'})
        if self.run_dir is not None and descriptor.get('run_dir') not in (None, self.run_dir):
            node['status'] = 'stale'
            node['reasons'].append({'code': 'run_directory_changed'})
        for selection in dependencies:
            edge = {'publication_id': selection['publication_id'], 'require_current': selection['require_current']}
            if selection['require_current']:
                upstream = await self.visit(selection['publication_id'], (*ancestors, identifier))
                edge['status'] = upstream['status']
                # Validate the exact selected paths as well as the dependency's
                # source; a malformed selection is not a usable graph edge.
                if edge['status'] == 'fresh':
                    files = self.manifests[selection['publication_id']]['files']
                    selected = selection['paths'] if selection['paths'] is not None else list(files)
                    if not selected or any(p not in files or p.split('/')[0].casefold() == '.vibeflow' for p in selected):
                        edge['status'] = 'unverifiable'
            else:
                try:
                    await resolve(self.db, self.case, [selection], materialize=False)
                    edge['status'] = 'historical_available'
                except (OSError, ValueError, KeyError, TypeError, ExecutionConflict):
                    edge['status'] = 'unverifiable'
            node['dependencies'].append(edge)
            if edge['status'] not in {'fresh', 'historical_available'}:
                node['reasons'].append({'code': 'dependency_' + edge['status'],
                                        'publication_id': selection['publication_id']})
                if edge['status'] == 'unverifiable':
                    node['status'] = 'unverifiable'
                elif node['status'] != 'unverifiable':
                    node['status'] = 'stale'
        if any(edge['status'] not in {'fresh', 'historical_available'} for edge in node['dependencies']):
            # Source validators can themselves require fresh upstream evidence
            # (e.g. report readiness). A known stale dependency already proves
            # staleness; don't mislabel that expected gate refusal as unknown.
            node['source_check'] = 'skipped_invalid_dependency'
        else:
            try:
                validator = getattr(handler, 'validate_publication', None)
                if validator:
                    await validator(self.db, self.case, descriptor)
                observed = await finish_before_cancel(asyncio.to_thread(handler.source, self.case.s3_prefix, descriptor))
                node['source_check'] = 'checked'
                if observed != descriptor['source']:
                    node['status'] = 'stale'
                    # Only schema field names, never original or current values.
                    fields = sorted(k for k in set(observed) | set(descriptor['source'])
                                    if observed.get(k) != descriptor['source'].get(k))
                    node['reasons'].append({'code': 'source_changed', 'fields': fields[:100], 'field_count': len(fields)})
            except (OSError, ValueError, KeyError, TypeError, ExecutionConflict):
                logger.warning('Cannot inspect dependency source (case=%s publication=%s)', self.case.id, row.id, exc_info=True)
                node['status'] = 'unverifiable'
                node['reasons'].append({'code': 'source_unverifiable'})
        return node


async def inspect(case, snapshot, *, run_dir=None):
    reads = PublicationReadSet()
    inspector = Inspector(case, reads, run_dir)
    heads = {item['kind']: item for item in snapshot['heads']}
    channels = []
    for kind in (*CORE, *sorted(set(heads) - set(CORE))):
        head = heads.get(kind)
        identifier = head['publication_id'] if head else None
        if identifier:
            node = await inspector.visit(identifier)
            channels.append({'kind': kind, 'publication_id': identifier,
                             'status': node['status'] if node.get('kind') == kind else 'unverifiable'})
        else:
            channels.append({'kind': kind, 'publication_id': None, 'status': 'missing'})
    reads.seal()
    # Current heads define what needs regeneration. A superseded historical
    # ancestor may stale a consumer, but must not schedule replacing a fresh head.
    required = {item['kind']: set() for item in channels}
    for previous, following in zip(CORE, CORE[1:]):
        required[following].add(previous)
    for item in channels:
        node = inspector.nodes.get(item['publication_id'], {})
        for edge in node.get('dependencies', []):
            upstream = inspector.nodes.get(edge['publication_id'], {})
            kind = upstream.get('kind')
            if edge['require_current'] and kind in required and kind != item['kind']:
                required[item['kind']].add(kind)
    ordered, remaining = [], list(channels)
    while remaining:
        done = {item['kind'] for item in ordered}
        ready = next((item for item in remaining if required[item['kind']] <= done), None)
        if ready is None:
            # Historical versions can be acyclic while rewiring current channel
            # heads would form a cycle. Do not propose an executable order.
            for item in remaining:
                item['status'] = 'unverifiable'
                item['reason'] = 'recompute_order_cycle'
            ordered.extend(remaining)
            break
        remaining.remove(ready)
        ordered.append(ready)
    result = {'schema': 'vibeflow.dependencies.v1', 'case_id': case.id, 'run_dir': run_dir,
        'dict_revision': snapshot['revision'], 'event_sequence': snapshot['event_sequence'],
        'advisory': True, 'channels': channels, 'publications': list(inspector.nodes.values()),
        'recompute': [item['kind'] for item in ordered if item['status'] in {'stale', 'missing'}],
        'needs_inspection': [item['kind'] for item in ordered if item['status'] == 'unverifiable']}
    return result, reads
