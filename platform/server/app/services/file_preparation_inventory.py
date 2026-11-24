"""Read-only, paged inventory; listing a private version never authorizes recovery."""
from __future__ import annotations

import heapq
import os
import stat
import uuid

from botocore.exceptions import ClientError

from .. import storage
from . import file_transactions as journal


def identity(name):
    try:
        return name if str(uuid.UUID(name)) == name else None
    except ValueError:
        return None


def local(prefix, limit, after):
    root = storage._fs_path(prefix, '.')
    if not root.exists():
        return [], False
    base = journal._path(root, journal.PREFIX)
    if not base.exists():
        return [], False
    # Only the top level is scanned. Never follow a residual link or read a
    # partial backup/manifest merely to report that it has been retained.
    with os.scandir(base) as entries:
        names = heapq.nsmallest(limit + 1, (entry.name for entry in entries
            if entry.name.startswith('preparing-') and entry.name > after))
    rows = []
    for name in names[:limit]:
        try:
            info = (base / name).lstat()
        except FileNotFoundError:
            info = None  # Keep the observed cursor even if the entry vanished.
        link = info is not None and (stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400))
        kind = 'missing' if info is None else 'link' if link else 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
        identifier = identity(name.removeprefix('preparing-'))
        rows.append({'cursor': name, 'path': journal.PREFIX + '/' + name,
                     'transaction_id': identifier, 'kind': kind,
                     'state': 'preparation_retained' if kind == 'directory' and identifier else 'unverified_retained',
                     'commit_state': 'not_determined', 'decision': 'retain'})
    return rows, len(names) > limit


def remote(prefix, limit, after, pending_id):
    from ..config import get_settings
    from .s3_live_store import Client
    client = storage.s3()
    # This fixed private subtree is deliberately not a logical live-file
    # listing. Read raw provider metadata so a page does not load every case
    # file or every historical transaction into the logical catalog adapter.
    if isinstance(client, Client):
        client = client.raw
    stem = storage._s3_key(prefix, journal.PREFIX + '/objects') + '/'
    args = {'Bucket': get_settings().s3_bucket, 'Prefix': stem,
            'Delimiter': '/', 'MaxKeys': limit + 1}
    if after:
        args['StartAfter'] = stem + after
    page = client.list_objects_v2(**args)
    entries = [(row['Prefix'], 'directory') for row in page.get('CommonPrefixes', [])]
    entries.extend((row['Key'], 'object') for row in page.get('Contents', []))
    entries.sort()
    rows = []
    for key, kind in entries[:limit]:
        if not key.startswith(stem) or key[len(stem):] <= after:
            raise ValueError('Unexpected preparation listing boundary')
        name = key[len(stem):]
        if '/' in name.rstrip('/') or not name:
            raise ValueError('Unexpected preparation listing depth')
        identifier = identity(name.rstrip('/')) if kind == 'directory' else None
        receipt = False
        if kind == 'directory':
            try:
                client.head_object(Bucket=args['Bucket'], Key=key + 'resolution.json')
                receipt = True
            except ClientError as error:
                if error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 404:
                    raise
        state = ('referenced_by_pending' if identifier and identifier == pending_id else
                 'receipt_retained' if receipt else 'preparation_retained' if identifier else 'unverified_retained')
        rows.append({'cursor': name, 'path': journal.PREFIX + '/objects/' + name.rstrip('/'),
                     'transaction_id': identifier, 'kind': kind, 'state': state,
                     'receipt_present': receipt, 'commit_state': 'not_determined', 'decision': 'retain'})
    more = bool(page.get('IsTruncated')) or len(entries) > limit
    if more and not rows:
        raise ValueError('Preparation listing did not advance')
    return rows, more


def inspect(prefix, *, limit=100, after='', pending_id=None):
    if type(limit) is not int or not 1 <= limit <= 200 or not isinstance(after, str) or len(after) > 1024:
        raise ValueError('Invalid preparation inventory page')
    if storage._use_s3():
        rows, more = remote(prefix, limit, after, pending_id)
    else:
        rows, more = local(prefix, limit, after)
    return {'entries': rows, 'has_more': more,
            'next_cursor': rows[-1]['cursor'] if more and rows else None,
            'scope': 'file_transaction_preparations', 'read_only': True}
