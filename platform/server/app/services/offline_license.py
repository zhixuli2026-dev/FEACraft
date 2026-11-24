"""VibeFlow offline grants. Runtime verification requires only the issuer's public key."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
import os
import re
import stat
import time
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .process_identity_native import host_id


GRANT = 'vibeflow.offline-grant.v1'
REVOCATIONS = 'vibeflow.offline-revocations.v1'
DOCUMENT_SIZE = 128 * 1024
MAX_EPOCH = 253402300799


class Grant(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    serial: UUID
    deployment: UUID
    org_id: int = Field(strict=True, gt=0, le=2**31-1)
    plan: Literal['pro', 'team']
    device: str = Field(pattern=r'^[0-9a-f]{64}$')
    issued_at: int = Field(strict=True, gt=0, le=MAX_EPOCH)
    not_before: int = Field(strict=True, gt=0, le=MAX_EPOCH)
    expires_at: int = Field(strict=True, gt=0, le=MAX_EPOCH)
    revocation_sequence: int = Field(strict=True, ge=1, le=2**31-1)

    @model_validator(mode='after')
    def period(self):
        if not self.issued_at <= self.not_before < self.expires_at:
            raise ValueError('Invalid license period')
        return self


class Revocations(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    deployment: UUID
    sequence: int = Field(strict=True, ge=1, le=2**31-1)
    issued_at: int = Field(strict=True, gt=0, le=MAX_EPOCH)
    serials: list[UUID] = Field(default_factory=list, max_length=2048)

    @model_validator(mode='after')
    def unique_serials(self):
        if len(set(self.serials)) != len(self.serials):
            raise ValueError('Repeated revoked license serial')
        return self


def device():
    if os.name != 'nt':
        # The process identity helper can use a boot ID. A license needs
        # the installed system ID so a standard restart retains activation.
        machine_id = read_file(Path('/etc/machine-id'), 256).decode('ascii').strip()
        if not re.fullmatch(r'[0-9a-f]{32}', machine_id) or machine_id == '0' * 32:
            raise ValueError('Offline grants require a persistent system identity')
    return hmac.new(b'VibeFlow offline grant device v1', bytes.fromhex(host_id()), hashlib.sha256).hexdigest()


def read_file(path, maximum=DOCUMENT_SIZE):
    with path.open('rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise ValueError('License file size is invalid')
        data = handle.read(maximum+1)
    if not data or len(data) > maximum:
        raise ValueError('License file size changed')
    return data


def unique_object(items):
    value = dict(items)
    if len(value) != len(items):
        raise ValueError('License contains a duplicate field')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def public_key(data):
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError('Expected an Ed25519 issuer public key')
    return key


def sign(kind, payload, key: Ed25519PrivateKey):
    body = {'type':kind, 'payload':payload.model_dump(mode='json')}
    return {**body, 'signature':base64.b64encode(key.sign(canonical(body))).decode('ascii')}


def verify(kind, envelope, key, schema):
    if (not isinstance(envelope, dict) or set(envelope) != {'type','payload','signature'}
            or envelope['type'] != kind or not isinstance(envelope['payload'], dict)
            or not isinstance(envelope['signature'], str) or len(envelope['signature']) != 88):
        raise ValueError('License envelope has an invalid shape')
    signature = base64.b64decode(envelope['signature'], validate=True)
    key.verify(signature, canonical({'type':kind, 'payload':envelope['payload']}))
    return schema.model_validate(envelope['payload'])


@lru_cache(maxsize=16)
def validated_bundle(raw, pem):
    key = public_key(pem)
    bundle = json.loads(raw.decode('utf8'), object_pairs_hook=unique_object)
    if not isinstance(bundle, dict) or set(bundle) != {'grant','revocations'}:
        raise ValueError('Expected a signed grant and revocation list')
    grant = verify(GRANT, bundle['grant'], key, Grant)
    revocations = verify(REVOCATIONS, bundle['revocations'], key, Revocations)
    if (revocations.deployment != grant.deployment or revocations.sequence < grant.revocation_sequence):
        raise ValueError('License revocations differ from the issuing deployment')
    return grant, revocations


@dataclass(frozen=True)
class Status:
    state: str
    grant: Grant | None = None

    def public(self):
        if self.grant is None:
            return {'status':self.state}
        expiry = datetime(1970,1,1,tzinfo=timezone.utc) + timedelta(seconds=self.grant.expires_at)
        return {'status':self.state,'plan_code':self.grant.plan,'expires_at':expiry.isoformat()}


def inspect(settings, org_id):
    try:
        grant, revocations = validated_bundle(read_file(Path(settings.offline_license_bundle_path)),
            read_file(Path(settings.offline_license_public_key_path),4096))
    except (ValueError, OSError, InvalidSignature, RecursionError):
        return Status('invalid')
    if grant.deployment != settings.offline_license_deployment or grant.org_id != org_id:
        return Status('unassigned')
    now = int(time.time())
    if revocations.issued_at > now or revocations.sequence < settings.offline_license_min_revocation_sequence:
        return Status('stale_revocations', grant)
    if grant.serial in revocations.serials:
        return Status('revoked', grant)
    if now < grant.not_before:
        return Status('not_yet_valid', grant)
    if now >= grant.expires_at:
        return Status('expired', grant)
    try:
        matches = hmac.compare_digest(grant.device, device())
    except (OSError, ValueError):
        return Status('device_unavailable', grant)
    return Status('active' if matches else 'other_device', grant)


async def for_org(settings, org_id):
    if not settings.offline_license_bundle_path:
        return None
    return await asyncio.to_thread(inspect, settings, org_id)
