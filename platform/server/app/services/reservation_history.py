"""Bounded history compaction for finalized account and case receipts."""
import asyncio
import json
import re

from sqlalchemy import Text, and_, case, cast, func, select, update

from ..db import SessionLocal
from ..models import OwnerStorageReservation, StorageWriteReservation
from ..reservation_receipts import MAX_RECEIPT_BYTES, PREFIX, pack, unpack
from .async_ops import finish_before_cancel


LEDGERS = (OwnerStorageReservation, StorageWriteReservation)
PAGE_SIZE = 32


def _closed(model, row, value):
    if not isinstance(value, dict):
        return False
    if model is OwnerStorageReservation:
        return (value.get('schema') == 'vibeflow.owner-capacity.v1' and value.get('id') == row.id
                and value.get('binding') == json.loads(row.binding_json) and value.get('size') == row.size
                and value.get('closed') is True and 'pending' in value and value['pending'] is None
                and not value.get('upload_recovery_required'))
    if value.get('schema') != 'vibeflow.remote-write-receipt.v1':
        return False
    status = value.get('status')
    if status == 412:
        return True
    etag = value.get('etag')
    if not isinstance(etag, str) or not etag:
        return False
    if status == 200:
        return True
    digest = value.get('sha256')
    return status == 'verified_immutable' and isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest) is not None


def _rewrite(model, rows, restore):
    replacements, unreadable = [], 0
    for row in rows:
        try:
            original = unpack(row.raw) if restore else row.raw
            if not _closed(model, row, json.loads(original)):
                if restore:
                    unreadable += 1
                continue
            selected = original if restore else pack(original)
        except (ValueError, TypeError):
            unreadable += 1
            continue
        if selected is not None:
            replacements.append((row.id, row.raw, selected))
    return replacements, unreadable


async def rewrite_page(model, after, *, restore=False):
    """Read a short snapshot, compress outside SQL, then compare-and-swap."""
    raw = cast(model.receipt_json, Text)
    settled = (model.state == 'settled', model.remaining_bytes == 0)
    encoded = raw.startswith(PREFIX, autoescape=True)
    target_format = encoded if restore else (~encoded & (func.length(raw) <= MAX_RECEIPT_BYTES))
    eligible = and_(*settled, target_format)
    async with SessionLocal() as db:
        # Page by the primary key before skipping ineligible rows. An already
        # compressed ledger must also take only one bounded page per pass.
        page = select(model.id, case((eligible, model.binding_json)).label('binding_json'), model.size,
                      case((eligible, raw)).label('raw'))
        if after is not None:
            page = page.where(model.id > after)
        rows = (await db.execute(page.order_by(model.id).limit(PAGE_SIZE))).all()
    next_after = rows[-1].id if len(rows) == PAGE_SIZE else None
    result = {'examined': len(rows), 'rewritten': 0, 'bytes_saved': 0, 'unreadable': 0}
    candidates = [row for row in rows if row.raw is not None]
    if not candidates:
        return next_after, result
    replacements, result['unreadable'] = await finish_before_cancel(asyncio.to_thread(_rewrite, model, candidates, restore))
    if replacements:
        async with SessionLocal() as db:
            for identifier, previous, replacement in replacements:
                changed = await db.execute(update(model).where(model.id == identifier, *settled, raw == previous)
                                           .values(receipt_json=replacement).execution_options(synchronize_session=False))
                if changed.rowcount == 1:
                    result['rewritten'] += 1
                    result['bytes_saved'] += len(previous.encode('utf-8')) - len(replacement.encode('utf-8'))
            await finish_before_cancel(db.commit())
    return next_after, result


async def compact_page(cursors):
    result = {}
    for model in LEDGERS:
        cursors[model], result[model.__tablename__] = await rewrite_page(model, cursors.get(model))
    return result


async def count_encoded():
    async with SessionLocal() as db:
        return {model.__tablename__: await db.scalar(select(func.count()).select_from(model)
            .where(cast(model.receipt_json, Text).startswith(PREFIX, autoescape=True))) for model in LEDGERS}
