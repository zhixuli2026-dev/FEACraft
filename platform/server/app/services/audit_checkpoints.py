"""Retain verified audit checkpoints in an independently configured S3 bucket."""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import select

from ..audit_models import OrganizationAuditHead, OrganizationAuditRecord
from ..db import SessionLocal
from .audit import query


def configured_client(endpoint):
    if endpoint:
        parts = urlsplit(endpoint)
        loopback = parts.hostname in {'127.0.0.1', '::1', 'localhost'}
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None or
                parts.password is not None or parts.query or parts.fragment or parts.path not in ('', '/') or
                (parts.scheme == 'http' and not loopback)):
            raise ValueError('Audit checkpoint endpoint requires HTTPS or local test transport')
    return boto3.client('s3', endpoint_url=endpoint or None,
        config=Config(connect_timeout=4, read_timeout=12, retries={'max_attempts': 1},
                      s3={'addressing_style': 'path'} if endpoint else None))


async def chain_head(org_id):
    async with SessionLocal() as db:
        head = await query(db, org_id, limit=0)
    if not head['chain_valid']:
        raise ValueError('Audit chain failed integrity verification')
    return head['head_sequence'], head['head_hash']


def document(deployment, org_id, sequence, digest):
    if (not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}', deployment) or type(org_id) is not int or org_id < 1 or
            type(sequence) is not int or sequence < 1 or not re.fullmatch('[a-f0-9]{64}', digest)):
        raise ValueError('Audit checkpoint identity is invalid')
    key = f'audit-checkpoints/{deployment}/org-{org_id}/{sequence:020d}.json'
    checkpoint = {'schema': 'vibeflow.audit.checkpoint.v1', 'deployment': deployment, 'org_id': org_id,
                  'sequence': sequence, 'chain_sha256': digest}
    body = (json.dumps(checkpoint, sort_keys=True, separators=(',', ':'))+'\n').encode('utf8')
    return key, checkpoint, body


def retained_version(client, args, result, body, version):
    with closing(result['Body']) as actual:
        if actual.read(len(body)+1) != body or not version or version == 'null' or result.get('VersionId') != version:
            raise ValueError('Stored checkpoint differs from the verified audit head')
    lock = client.get_object_retention(**args, VersionId=version)['Retention']
    actual_retention = lock['RetainUntilDate']
    if lock['Mode'] != 'COMPLIANCE' or actual_retention <= datetime.now(timezone.utc):
        raise ValueError('Checkpoint lacks the required version retention')
    return actual_retention


def store(client, bucket, deployment, org_id, sequence, digest, retention_days):
    """A key is defined by organization and sequence, so a replacement is detected."""
    if not 1 <= retention_days <= 3650:
        raise ValueError('Audit checkpoint retention length is invalid')
    key, checkpoint, body = document(deployment, org_id, sequence, digest)
    args = {'Bucket': bucket, 'Key': key}
    now = datetime.now(timezone.utc)
    retain_until = (now + timedelta(days=retention_days)).replace(microsecond=0)
    try:
        result = client.put_object(**args, Body=body, IfNoneMatch='*', ContentType='application/json',
            ChecksumSHA256=base64.b64encode(hashlib.sha256(body).digest()).decode('ascii'),
            ObjectLockMode='COMPLIANCE', ObjectLockRetainUntilDate=retain_until)
        version = result.get('VersionId')
        if not version or version == 'null':
            raise ValueError('Checkpoint store did not return a version ID')
        result = client.get_object(**args, VersionId=version)
        created = True
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] != 412:
            raise
        result = client.get_object(**args)
        version = result.get('VersionId')
        created = False
    actual_retention = retained_version(client, args, result, body, version)
    if created and actual_retention < retain_until:
        raise ValueError('Checkpoint lacks the required version retention')
    return {**checkpoint, 'key': key, 'version_id': version, 'retain_until': actual_retention.isoformat(),
            'created': created}


async def verify(client, bucket, deployment, receipt, session_factory=SessionLocal):
    org_id, sequence, digest = (receipt[field] for field in ('org_id', 'sequence', 'chain_sha256'))
    key, checkpoint, body = document(deployment, org_id, sequence, digest)
    version = receipt['version_id']
    if not isinstance(version, str) or not 1 <= len(version) <= 1024 or version == 'null':
        raise ValueError('Invalid audit checkpoint version')
    args = {'Bucket': bucket, 'Key': key}

    def read():
        result = client.get_object(**args, VersionId=version)
        return retained_version(client, args, result, body, version)

    lock = await asyncio.to_thread(read)
    async with session_factory() as db:
        current = await query(db, org_id, limit=0)
        prefix = await db.get(OrganizationAuditRecord, (org_id, sequence))
        if (not current['chain_valid'] or current['head_sequence'] < sequence or prefix is None or
                json.loads(prefix.payload).get('event_hash') != digest):
            raise ValueError('Audit database chain differs from the retained checkpoint')
    return {**checkpoint, 'key': key, 'version_id': version, 'retain_until': lock.isoformat(), 'verified': True}


async def run(args):
    client = configured_client(args.endpoint)
    if args.verify_receipt is not None:
        try:
            with open(args.verify_receipt, 'rb') as source:
                payload = source.read(8193)
            if len(payload) > 8192:
                raise ValueError('Audit checkpoint receipt exceeds its limit')
            outcome = await verify(client, args.bucket, args.deployment, json.loads(payload))
            print(json.dumps(outcome), flush=True)
            return 0
        finally:
            client.close()
    async with SessionLocal() as db:
        orgs = select(OrganizationAuditHead.org_id).order_by(OrganizationAuditHead.org_id)
        if args.org is not None:
            orgs = orgs.where(OrganizationAuditHead.org_id == args.org)
        selected = (await db.scalars(orgs)).all()
    if args.org is not None and not selected:
        raise ValueError('Organization audit head does not exist')
    failed = False
    try:
        for org_id in selected:
            try:
                sequence, digest = await chain_head(org_id)
                if sequence:
                    receipt = await asyncio.to_thread(store, client, args.bucket, args.deployment,
                                                      org_id, sequence, digest, args.retention_days)
                    print(json.dumps(receipt), flush=True)
            except (ClientError, OSError, ValueError) as error:
                failed = True
                print(json.dumps({'org_id': org_id, 'checkpoint_failed': type(error).__name__}), flush=True)
    finally:
        client.close()
    return int(failed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bucket', default=os.getenv('VIBEFLOW_AUDIT_CHECKPOINT_BUCKET'),
                        required=not os.getenv('VIBEFLOW_AUDIT_CHECKPOINT_BUCKET'))
    parser.add_argument('--deployment', default=os.getenv('VIBEFLOW_AUDIT_CHECKPOINT_DEPLOYMENT'),
                        required=not os.getenv('VIBEFLOW_AUDIT_CHECKPOINT_DEPLOYMENT'))
    parser.add_argument('--endpoint', default=os.getenv('VIBEFLOW_AUDIT_CHECKPOINT_ENDPOINT'))
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--retention-days', type=int)
    operation.add_argument('--verify-receipt')
    parser.add_argument('--org', type=int)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
