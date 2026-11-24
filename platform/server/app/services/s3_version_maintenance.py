"""Explicit offline retention for immutable live versions, with generation fencing.

All application readers/writers must be stopped. Never run as background GC:
presigned downloads and pinned reader snapshots do not carry durable GC leases.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from botocore.exceptions import ClientError

from .s3_live_store import COMPACT_SCHEMA, DIRECTORY, HEAD, PENDING, split, visible

PLAN_SCHEMA = 'vibeflow.live-version-maintenance.v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def plan(client, bucket, prefix, *, retention_days=30):
    if split(prefix+'/') != (prefix, ''):
        raise ValueError('An explicit canonical case prefix is required')
    if type(retention_days) is not int or not 0 <= retention_days <= 36500:
        raise ValueError('Retention must be between 0 and 36500 days')
    value, etag = client._load(bucket, prefix)
    if value is None:
        raise ValueError('Legacy namespace has no live manifest; migrate through normal application writes first')
    if visible(value['files'].get(PENDING)):
        raise ValueError('Pending file transaction must be recovered before version maintenance')
    selected = {entry['key'] for entry in value['files'].values() if visible(entry)}
    # Verify every current reference before proposing removal of any old copy.
    for entry in value['files'].values():
        if not visible(entry): continue
        info = client.raw.head_object(Bucket=bucket, Key=entry['key'])
        if info.get('ETag') != entry['physical_etag'] or info['ContentLength'] != entry['size']:
            raise ValueError('Current immutable source is missing or changed')
    now = datetime.now(timezone.utc)
    cutoff = now-timedelta(days=retention_days)
    base = prefix+'/'+DIRECTORY+'/objects/'
    pattern = re.compile(re.escape(base)+r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/[^/]+$')
    candidates, retained = [], {'current': 0, 'recent': 0, 'unknown': 0}
    for row in client._raw_pages(bucket, base):
        if row['Key'] in selected:
            retained['current'] += 1
        elif (not pattern.fullmatch(row['Key']) or not row.get('ETag')
              or not isinstance(row.get('LastModified'), datetime) or type(row.get('Size')) is not int):
            retained['unknown'] += 1
        elif row['LastModified'] > cutoff:
            retained['recent'] += 1
        else:
            candidates.append({'key':row['Key'], 'etag':row['ETag'], 'bytes':row['Size'],
                               'modified':row['LastModified'].isoformat()})
    latest, current = client._load(bucket, prefix)
    if current != etag or latest != value:
        raise ValueError('Manifest changed during maintenance preview')
    result = {'schema':PLAN_SCHEMA, 'endpoint':client.meta.endpoint_url, 'bucket':bucket,
              'prefix':prefix, 'head_etag':etag, 'head_sha256':digest(value),
              'created_at':now.isoformat(), 'retention_days':retention_days,
              'cutoff':cutoff.isoformat(), 'candidates':sorted(candidates,key=lambda x:x['key']),
              'candidate_bytes':sum(x['bytes'] for x in candidates), 'retained':retained,
              'tombstones':sum(not visible(e) for e in value['files'].values()),
              'requires_offline':True, 'new_schema':COMPACT_SCHEMA}
    return {**result, 'plan_sha256':digest(result)}


def _delete_probe(client, bucket, prefix):
    client._require_conditions(bucket, prefix)
    key = prefix+'/'+DIRECTORY+'/probes/'+uuid.uuid4().hex
    version = client.raw.put_object(Bucket=bucket, Key=key, Body=b'conditional-delete-probe', IfNoneMatch='*')['ETag']
    try:
        try: client.raw.delete_object(Bucket=bucket, Key=key, IfMatch='"invalid-version"')
        except ClientError as error:
            if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode') != 412: raise
        else: raise ValueError('Object backend does not enforce conditional deletion')
        observed = client.raw.head_object(Bucket=bucket, Key=key)
        if observed.get('ETag') != version: raise ValueError('Conditional delete probe changed')
    finally:
        client.raw.delete_object(Bucket=bucket, Key=key, IfMatch=version)


def apply(client, approved, *, confirm_offline=False):
    if confirm_offline is not True:
        raise ValueError('Stop all readers/writers and confirm offline maintenance')
    expected = dict(approved); signature = expected.pop('plan_sha256', None)
    if signature != digest(expected) or expected.get('schema') != PLAN_SCHEMA:
        raise ValueError('Invalid maintenance plan digest/schema')
    bucket, prefix = expected['bucket'], expected['prefix']
    # Rebuild the candidate set instead of trusting editable file paths. New
    # objects beyond the old cutoff are never swept by this approval.
    fresh = plan(client, bucket, prefix, retention_days=expected['retention_days'])
    for key in ('endpoint','head_etag','head_sha256'):
        if fresh[key] != expected[key]: raise ValueError('Maintenance target or manifest changed; preview again')
    allowed = {r['key']:r for r in fresh['candidates']}
    if len({r['key'] for r in expected['candidates']}) != len(expected['candidates']):
        raise ValueError('Duplicate maintenance candidates')
    for row in expected['candidates']:
        if allowed.get(row['key']) != row or datetime.fromisoformat(row['modified']) > datetime.fromisoformat(expected['cutoff']):
            raise ValueError('Candidate changed or is outside the approved retention period')
    _delete_probe(client, bucket, prefix)
    value, etag = client._load(bucket, prefix)
    if etag != expected['head_etag'] or digest(value) != expected['head_sha256']:
        raise ValueError('Manifest changed before maintenance fence')
    compact = dict(value, schema=COMPACT_SCHEMA, generation=str(uuid.uuid4()),
                   files={p:e for p,e in value['files'].items() if visible(e)})
    _, fenced_etag = client._save(bucket, prefix, compact, etag)
    deleted, errors = [], []
    for row in expected['candidates']:
        _, current = client._load(bucket, prefix)
        if current != fenced_etag:
            raise ValueError('Writer observed during offline maintenance; stop and inspect before retrying')
        try:
            client.raw.delete_object(Bucket=bucket, Key=row['key'], IfMatch=row['etag'])
            deleted.append(row)
        except ClientError as error:
            errors.append({'key':row['key'], 'code':error.response.get('Error',{}).get('Code','unknown')})
    return {'schema':PLAN_SCHEMA, 'plan_sha256':signature, 'generation':compact['generation'],
            'head_etag':fenced_etag, 'compacted_tombstones':expected['tombstones'],
            'deleted_objects':len(deleted), 'deleted_bytes':sum(r['bytes'] for r in deleted),
            'errors':errors, 'requires_new_preview_for_retry':True}
