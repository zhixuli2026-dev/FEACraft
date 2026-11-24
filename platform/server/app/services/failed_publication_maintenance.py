"""Offline retirement of a failed publication's verified duplicate objects."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select

from .. import storage
from ..models import (ArtifactPublication, ArtifactPublicationHead, Case, Job, JobAttempt,
                      PublicationStorageReservation, Workspace)
from . import attempt_objects, job_drain_recovery, process_identity_native, publication_objects
from .artifact_publication import PublicationIdentity
from .s3_version_maintenance import _delete_probe, digest
from .upload_storage import binding


async def load_target(db, case_id, publication_id):
    case = await db.get(Case, case_id)
    publication = await db.get(ArtifactPublication, publication_id)
    if (case is None or publication is None or publication.case_id != case_id or publication.status != 'failed'
            or publication.committed_at is not None or publication.manifest_sha256 != '0' * 64):
        raise ValueError('Select a failed publication that never became visible')
    head = await db.scalar(select(ArtifactPublicationHead.case_id).where(
        ArtifactPublicationHead.active_publication_id == publication_id))
    if head is not None:
        raise ValueError('The publication is still referenced by a head')
    attempt = await db.get(JobAttempt, publication.attempt_id)
    job = await db.get(Job, attempt.job_id) if attempt is not None else None
    if job is None or job.status not in {'failed', 'cancelled'}:
        raise ValueError('The publication job is not terminal')
    job_drain_recovery.eligible(case, job, attempt)
    await job_drain_recovery.history(db, case, job, attempt)
    reservation = await db.get(PublicationStorageReservation, publication_id)
    workspace = await db.get(Workspace, case.workspace_id)
    if (workspace is None or reservation is None or reservation.case_id != case_id
            or reservation.user_id != workspace.user_id or reservation.binding_json != binding(case)
            or reservation.state != 'materialized' or reservation.remaining_bytes != 0):
        raise ValueError('The duplicate has no settled copy reservation')
    if process_identity_native.probe(json.loads(reservation.writer_json)) != {'state':'exited', 'reason':'process_exited'}:
        raise ValueError('The original copy process has not been verified stopped on this node')
    metadata = replace(PublicationIdentity.capture(publication), manifest_sha256=reservation.manifest_sha256)
    source = json.loads(attempt.input_manifest_json)['workspace']
    output = json.loads(attempt.output_manifest_json)
    return case.s3_prefix, metadata, source, output


def review(prefix, row, source, output, *, retention_days, approved=None):
    if not storage._use_s3() or type(retention_days) is not int or not 0 <= retention_days <= 3650:
        raise ValueError('Failed copy maintenance requires S3 and a valid retention period')
    archive = publication_objects.identity(row)
    intent = publication_objects.read_intent(prefix, row)
    if intent['manifest_sha256'] != row.manifest_sha256:
        raise ValueError('The copy intent does not match its database reservation')
    manifest = intent['manifest']
    ready = json.loads(attempt_objects.read(prefix, publication_objects.journal_path(row, 'ready.json')))
    from .publication_journal import SCHEMA
    if ready != {'schema':SCHEMA, 'manifest_sha256':row.manifest_sha256, 'state':'bundle_ready'}:
        raise ValueError('The failed copy has no matching ready receipt')
    try:
        attempt_objects.read(prefix, publication_objects.journal_path(row, 'aliases-applied.json'))
    except FileNotFoundError:
        pass
    else:
        raise ValueError('The copy has an alias application receipt')
    if source['attempt_id'] != row.attempt_id:
        raise ValueError('The retained archive belongs to another attempt')
    original = attempt_objects.output_inventory(prefix, source, output)
    if any(original.get(name) != record for name,record in manifest['files'].items()):
        raise ValueError('The original output does not cover all duplicate objects')
    attempt_objects.verify_members(prefix, source['run_path'], original)

    client = storage.s3()
    target = attempt_objects.arguments(prefix, archive)
    raw = client.raw
    def listed():
        # Include unfamiliar directory markers, which must retain the copy.
        return sorted(({'key': item['Key'], 'etag': item.get('ETag'), 'size': item['Size'],
                        'modified': item['LastModified'].isoformat()}
                       for item in client._raw_pages(target['Bucket'], target['Key'] + '/')),
                      key=lambda item: item['key'])

    observed = listed()
    expected = {target['Key'] + '/manifest.json': attempt_objects.digest(publication_objects.encoded(manifest))}
    expected.update({target['Key']+'/files/'+name: {field:record[field] for field in ('size', 'sha256')}
                     for name,record in manifest['files'].items()})
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    recent = False
    for item in observed:
        record = expected.get(item['key'])
        if record is None or not item['etag'] or record['size'] != item['size']:
            raise ValueError('Unexpected object in the failed copy; retain it for inspection')
        path = archive + item['key'][len(target['Key']):]
        if attempt_objects.selected_digest(prefix, path, etag=item['etag'].strip('"'), size=item['size']) != record:
            raise ValueError('Failed copy object differs from its recorded bytes')
        recent |= datetime.fromisoformat(item['modified']) > cutoff
    if listed() != observed:
        raise ValueError('Failed copy changed during review')
    state = 'retained' if recent else 'candidate' if observed else 'absent'
    result = {'state':state, 'publication_id':row.id, 'retention_days':retention_days,
              'candidate_bytes':sum(item['size'] for item in observed) if state == 'candidate' else 0}
    if state == 'candidate':
        result['review_sha256'] = digest([manifest['storage'], vars(row), output['object_store'], retention_days, observed])
    if approved is None:
        return result
    if state != 'candidate' or approved != result['review_sha256']:
        raise ValueError('Failed copy differs from the approved review')
    _delete_probe(client, target['Bucket'], prefix.rstrip('/'))
    if listed() != observed:
        raise ValueError('Failed copy changed before retirement')
    for item in observed:
        raw.delete_object(Bucket=target['Bucket'], Key=item['key'], IfMatch=item['etag'])
    if listed():
        raise ValueError('Failed copy has remaining objects; generate a new review')
    return {**result, 'state':'evicted', 'evicted_bytes': result['candidate_bytes']}
