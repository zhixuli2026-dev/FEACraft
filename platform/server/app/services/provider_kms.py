"""Envelope encryption for organization model credentials using OpenBao Transit."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx

from ..config import get_settings


PREFIX = 'vibeflow-kms-v1:'
MAX_ENVELOPE = 16384
MAX_SECRET = 4096
_NAME = re.compile(r'[a-z][a-z0-9-]{0,47}')
_WRAPPED = re.compile(r'vault:v[1-9][0-9]{0,8}:[A-Za-z0-9+/=]{16,2048}')


class KmsError(RuntimeError):
    pass


def _encode(value):
    return base64.b64encode(value).decode('ascii')


def _decode(value):
    return base64.b64decode(value, validate=True)


def _binding(org_id, profile_id, provider, base_url):
    if type(org_id) is not int or org_id <= 0 or type(profile_id) is not int or profile_id <= 0:
        raise KmsError('Invalid organization credential binding')
    endpoint_digest = hashlib.sha256(json.dumps([provider,base_url],separators=(',',':')).encode()).hexdigest()
    return f'{PREFIX}{org_id}:{profile_id}:{endpoint_digest}'.encode('ascii')


def key_name(org_id, settings=None):
    selected = settings or get_settings()
    if not _NAME.fullmatch(selected.provider_kms_key_prefix):
        raise KmsError('Organization KMS key prefix is not configured')
    return f'{selected.provider_kms_key_prefix}-org-{org_id}'


async def _transit(org_id, operation, body):
    settings = get_settings()
    try:
        uri = urlsplit(settings.provider_kms_address)
        if ((uri.scheme != 'https' and not (uri.scheme == 'http' and
             uri.hostname in {'localhost','127.0.0.1','::1'})) or not uri.hostname or uri.port == 0
            or uri.username is not None or uri.path not in {'','/'} or uri.query or uri.fragment
            or not settings.provider_kms_token_file
            or not _NAME.fullmatch(settings.provider_kms_mount)):
            raise KmsError('Organization KMS is not configured')
        token_path = Path(settings.provider_kms_token_file)
        with token_path.open('rb') as stream:
            token = stream.read(4097).strip().decode('ascii')
        if not token or len(token)>4096 or any(not 33<=ord(c)<=126 for c in token):
            raise KmsError('Organization KMS credential is invalid')
        tls = (ssl.create_default_context(cafile=settings.provider_kms_ca_file)
               if settings.provider_kms_ca_file else True)
        endpoint = f'{uri.scheme}://{uri.netloc}/v1/{settings.provider_kms_mount}/{operation}/{key_name(org_id,settings)}'
        async with httpx.AsyncClient(trust_env=False, verify=tls,
                                     timeout=httpx.Timeout(6.0,connect=2.0)) as client:
            async with client.stream('POST', endpoint, json=body, headers={
                'X-Vault-Token': token, 'X-Vault-Request':'true', 'Accept-Encoding':'identity',
            }) as response:
                if response.status_code != 200:
                    raise KmsError('Organization KMS rejected the operation')
                value=bytearray()
                async for part in response.aiter_bytes():
                    value.extend(part)
                    if len(value)>MAX_ENVELOPE:
                        raise KmsError('Organization KMS response is too large')
        data = json.loads(value)['data']
        if not isinstance(data, dict):
            raise KmsError('Invalid organization KMS response')
        return data
    except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
        raise KmsError('Organization KMS is unavailable') from None


def _wrapped(data):
    value = data.get('ciphertext')
    if not isinstance(value,str) or not _WRAPPED.fullmatch(value):
        raise KmsError('Invalid wrapped organization credential')
    return value


def _parse(value):
    if not value.startswith(PREFIX) or len(value)>MAX_ENVELOPE:
        raise KmsError('Invalid organization credential envelope')
    try:
        data = json.loads(value[len(PREFIX):])
        if not isinstance(data,dict) or set(data)!={'ciphertext','nonce','wrapped_key'}:
            raise ValueError
        _wrapped({'ciphertext':data['wrapped_key']})
        return data
    except (ValueError,TypeError):
        raise KmsError('Invalid organization credential envelope') from None


def _pack(data):
    return PREFIX+json.dumps(data,separators=(',',':'))


async def seal(org_id, profile_id, provider, base_url, secret):
    binding = _binding(org_id,profile_id,provider,base_url)
    plain = secret.encode('utf8')
    if not 1<=len(plain)<=MAX_SECRET:
        raise KmsError('Invalid organization credential length')
    dek,nonce = AESGCM.generate_key(bit_length=256),os.urandom(12)
    ciphertext = AESGCM(dek).encrypt(nonce,plain,binding)
    result = await _transit(org_id,'encrypt',{'plaintext':_encode(dek),'context':_encode(binding)})
    return _pack({'ciphertext':_encode(ciphertext),'nonce':_encode(nonce),'wrapped_key':_wrapped(result)})


async def unseal(org_id, profile_id, provider, base_url, value):
    binding = _binding(org_id,profile_id,provider,base_url)
    doc = _parse(value)
    result = await _transit(org_id,'decrypt',{'ciphertext':doc['wrapped_key'],'context':_encode(binding)})
    try:
        dek,nonce,content = _decode(result['plaintext']),_decode(doc['nonce']),_decode(doc['ciphertext'])
        if len(dek)!=32 or len(nonce)!=12 or not 17<=len(content)<=MAX_SECRET+16:
            raise ValueError
        return AESGCM(dek).decrypt(nonce,content,binding).decode('utf8')
    except (ValueError, KeyError, TypeError, InvalidTag):
        raise KmsError('Organization credential could not be authenticated') from None


async def rewrap(org_id, profile_id, provider, base_url, value):
    binding = _binding(org_id,profile_id,provider,base_url)
    doc = _parse(value)
    result = await _transit(org_id,'rewrap',{'ciphertext':doc['wrapped_key'],'context':_encode(binding)})
    wrapped = _wrapped(result)
    if wrapped.split(':',2)[1] == doc['wrapped_key'].split(':',2)[1]:
        return value
    doc['wrapped_key'] = wrapped
    return _pack(doc)
