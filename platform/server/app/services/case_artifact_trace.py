"""Present case-owned publication provenance from sealed inputs and commit records."""
import hashlib
import json
import re

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from .. import storage
from ..models import ArtifactPublication, ArtifactPublicationHead, DurableEvent, Job, JobAttempt
from . import artifact_publication, attempt_workspace
from .case_read_capacity import file_read_deadline
from .case_write import bounded_case_read
from .casedict import CASE_DICT_PATH
from .durable_events import json_text


def sealed_source(attempt, case_id):
    document = json.loads(attempt.input_manifest_json)
    manifest = document.get('workspace')
    if manifest is None:
        return None
    attempt_workspace.validate_identity(manifest)
    source = manifest['source']
    if (manifest['attempt_id'], source['case_id'], source['job_id']) != (attempt.id, case_id, attempt.job_id):
        raise ValueError('Sealed publication source belongs to another attempt')
    dictionary = manifest['files'].get(CASE_DICT_PATH)
    revision = source.get('dict_revision')
    case_dict = {'revision': revision} if type(revision) is int and revision >= 0 else {}
    if dictionary is not None:
        digest, size = dictionary['sha256'], dictionary['size']
        if not re.fullmatch('[0-9a-f]{64}', digest) or type(size) is not int or size < 0:
            raise ValueError('Invalid sealed case dictionary record')
        case_dict.update(path=CASE_DICT_PATH, sha256=digest, size=size)
    references = []
    for reference in manifest.get('publications', []):
        references.append({key: reference[key] for key in (
            'publication_id', 'attempt_id', 'kind', 'generation', 'manifest_sha256')})
    return {
        'workspace_sha256': hashlib.sha256(json_text(manifest).encode('utf-8')).hexdigest(),
        'case_dict': case_dict or None,
        'input_publications': references,
    }


def _trace_query(case_id, *columns):
    publication = ArtifactPublication
    head = ArtifactPublicationHead
    return (select(*columns, (head.active_publication_id == publication.id).label('active'))
            .select_from(publication).join(JobAttempt, JobAttempt.id == publication.attempt_id)
            .join(Job, Job.id == JobAttempt.job_id)
            .outerjoin(head, and_(head.case_id == publication.case_id, head.kind == publication.kind))
            .where(publication.case_id == case_id, Job.case_id == case_id, Job.status == 'succeeded',
                   JobAttempt.phase == 'succeeded', publication.status.in_(('committed', 'superseded'))))


def _summary(row, job_id, active):
    return {'id': row.id, 'attempt_id': row.attempt_id, 'job_id': job_id, 'kind': row.kind,
            'generation': row.generation, 'manifest_sha256': row.manifest_sha256, 'active': bool(active),
            'status': row.status}


async def page(db, case_id, before_id, limit):
    publication = ArtifactPublication
    query = _trace_query(case_id, publication, Job.id)
    if before_id is not None:
        cursor = await db.scalar(query.where(publication.id == before_id))
        if cursor is None:
            raise HTTPException(404, '产物追溯游标不存在')
        query = query.where(or_(publication.created_at < cursor.created_at,
                                and_(publication.created_at == cursor.created_at, publication.id < cursor.id)))
    rows = (await db.execute(query.order_by(publication.created_at.desc(), publication.id.desc()).limit(limit + 1))).all()
    items = [_summary(row, job_id, active) for row, job_id, active in rows[:limit]]
    return {'items': items, 'next_before_id': items[-1]['id'] if len(rows) > limit else None}


async def native_file_identity(db, case_id, identifier):
    row = await db.scalar(_trace_query(case_id, ArtifactPublication).where(ArtifactPublication.id == identifier))
    if row is None or (row.kind != 'mesh' and not re.fullmatch(r'(calculix|su2|elmer|moose|code-aster):[0-9a-f]{40}', row.kind)):
        raise HTTPException(404, '原生工程产物发布不存在')
    return artifact_publication.PublicationIdentity.capture(row)


def _archive(prefix, identity):
    from .publication_objects import bound_metadata, validate_records
    if storage._use_s3():
        return bound_metadata(prefix, identity)
    _, manifest = artifact_publication.read_manifest(prefix, identity)
    validate_records(manifest['files'])
    return manifest


def _proof(row, event, source):
    if event is None:
        return None
    receipt = json.loads(event.payload_json)
    kind = 'artifact.published' if row.status == 'committed' else 'artifact.superseded'
    if (event.type != kind or receipt['publication_id'] != row.id or receipt['kind'] != row.kind
            or receipt['generation'] != row.generation
            or receipt.get('manifest_sha256', row.manifest_sha256) != row.manifest_sha256):
        raise ValueError('Publication trace differs from its commit event')
    if 'provenance' in receipt and receipt['provenance'] != source:
        raise ValueError('Sealed source differs from the publication commit event')
    return {'event_id': event.id, 'sequence': event.seq, 'source_recorded': 'provenance' in receipt}


async def detail(db, case, identifier):
    selected = (await db.execute(_trace_query(case.id, ArtifactPublication, JobAttempt, Job.id)
                                .where(ArtifactPublication.id == identifier))).one_or_none()
    if selected is None:
        raise HTTPException(404, '产物追溯记录不存在')
    row, attempt, job_id, active = selected
    events = (await db.scalars(select(DurableEvent).where(DurableEvent.case_id == case.id,
        DurableEvent.attempt_id == attempt.id, DurableEvent.type.in_(('artifact.published', 'artifact.superseded')))
        .limit(2))).all()
    try:
        if len(events) > 1:
            raise ValueError('Duplicate publication commit event')
        source = sealed_source(attempt, case.id)
        proof = _proof(row, events[0] if events else None, source)
        identity = artifact_publication.PublicationIdentity.capture(row)
        publication = _summary(row, job_id, active)
        prefix = case.s3_prefix
        # Release the SQL snapshot before inspecting the immutable archive.
        await db.rollback()
        code, label = 'case_artifact_trace', '产物追溯读取'
        async with file_read_deadline(code, label):
            archive = await bounded_case_read(_archive, prefix, identity, code=code, label=label)
        files = archive['files']
        contact = None
        mechanical_loads = None
        elasticity = None
        pressure_transfer = None
        numerical = None
        mesh_adaptivity = None
        native_configuration = {}
        if publication['kind'].startswith('calculix:'):
            descriptor = archive['descriptor']
            contact = descriptor['source'].get('contact')
            if contact and descriptor['receipt']['native_contact_sha256'] != contact['native_input_sha256']:
                raise ValueError('CalculiX contact declaration differs from its native receipt')
            mechanical_loads = descriptor['source'].get('mechanical_loads')
            if mechanical_loads and descriptor['receipt']['native_loads_sha256'] != mechanical_loads['native_input_sha256']:
                raise ValueError('CalculiX load declaration differs from its native receipt')
            pressure_transfer = descriptor['source'].get('pressure_transfer')
            if pressure_transfer is not None and (not mechanical_loads or not any(
                    item['publication_id']==pressure_transfer['publication_id'] and
                    item['manifest_sha256']==pressure_transfer['manifest_sha256']
                    for item in source['input_publications'])):
                raise ValueError('CalculiX pressure transfer has no sealed OpenFOAM source')
            elasticity = descriptor['source'].get('elasticity')
            if elasticity and descriptor['receipt']['native_elasticity_sha256'] != elasticity['native_input_sha256']:
                raise ValueError('CalculiX elasticity declaration differs from its native receipt')
            element_families = descriptor['source'].get('element_families')
            if element_families is not None:
                if descriptor['receipt']['element_families'] != element_families:
                    raise ValueError('CalculiX element types differ from their native receipt')
                native_configuration['element_families'] = element_families
        if publication['kind'].startswith('elmer:'):
            descriptor = archive['descriptor']
            for key in ('coupling','electrodes','electrostatic'):
                value = descriptor['source'].get(key)
                if value is None:
                    continue
                if descriptor['receipt'][key] != value:
                    raise ValueError('Elmer declaration differs from its native receipt: '+key)
                native_configuration[key] = value
        if publication['kind'].startswith(('calculix:','elmer:','su2:','moose:','code-aster:')):
            descriptor = archive['descriptor']
            if 'numerical' in descriptor['source']:
                numerical = descriptor['receipt']['numerical']
                if descriptor['source']['numerical'] != {key:value for key,value in numerical.items() if key!='observed'}:
                    raise ValueError('Declared numerical controls differ from their native receipt')
        if publication['kind'].startswith('moose:') and 'mesh_adaptivity' in descriptor['source']:
            mesh_adaptivity = descriptor['receipt']['mesh_adaptivity']
            if descriptor['source']['mesh_adaptivity'] != {key:value for key,value in mesh_adaptivity.items() if key!='observed'}:
                raise ValueError('MOOSE mesh refinement differs from its native receipt')
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(409, {'code': 'evidence_invalid', 'message': '产物追溯记录或清单无法核验'}) from error
    revision = (source or {}).get('case_dict') or {}
    query = None
    if 'sha256' in revision and 'revision' in revision:
        query = f"/cases/{identity.case_id}/provenance?revision={revision['revision']}&sha256={revision['sha256']}&limit=1"
    result = {'publication': publication, 'source': source, 'proof': proof, 'files': files,
              'case_revision_query': query}
    if contact is not None:
        result['contact'] = contact
    if mechanical_loads is not None:
        result['mechanical_loads'] = mechanical_loads
    if elasticity is not None:
        result['elasticity'] = elasticity
    if pressure_transfer is not None:
        result['pressure_transfer'] = pressure_transfer
    if numerical is not None:
        result['numerical'] = numerical
    if mesh_adaptivity is not None:
        result['mesh_adaptivity'] = mesh_adaptivity
    result.update(native_configuration)
    return result
