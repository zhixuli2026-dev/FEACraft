"""Workspace catalog of immutable, portable case material records."""

from datetime import timezone
import hashlib
import json

from sqlalchemy import func, insert, literal, select

from ..models import MaterialLibraryRevision as Revision
from . import casedict
from .case_write import CaseWriteConflict


KEY = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
MAX_ENTRY_BYTES = 64 * 1024


def encode(name, entry):
    body = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(body.encode('utf8')) > MAX_ENTRY_BYTES:
        raise ValueError('材料超过 64 KiB')
    draft = {'workflowState': 'A', 'caseMeta': {}, 'materials': {'entries': {name: entry}}}
    issues = casedict.validate_case_dict_data(draft)
    if issues:
        raise ValueError('，'.join(f"{issue['path']} {issue['message']}" for issue in issues))
    return body


def serialize(row, *, full=False):
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    value = {'key': row.key, 'revision': row.revision, 'archived': row.material_json is None,
             'created_at': created_at.astimezone(timezone.utc).isoformat()}
    if row.material_json is not None:
        value['sha256'] = hashlib.sha256(row.material_json.encode('utf8')).hexdigest()
        if full:
            value['material'] = json.loads(row.material_json)
    return value


async def list_entries(db, workspace_id, *, after, limit, include_archived):
    latest = (select(Revision.key, func.max(Revision.revision).label('revision'))
              .where(Revision.workspace_id == workspace_id, Revision.key > after)
              .group_by(Revision.key).subquery())
    query = (select(Revision).join(latest, (Revision.key == latest.c.key) & (Revision.revision == latest.c.revision))
             .where(Revision.workspace_id == workspace_id).order_by(Revision.key).limit(limit+1))
    if not include_archived:
        query = query.where(Revision.material_json.is_not(None))
    rows = (await db.scalars(query)).all()
    return {'items': [serialize(row) for row in rows[:limit]],
            'next_after': rows[limit-1].key if len(rows) > limit else None}


async def get(db, workspace_id, key, revision=None):
    if revision is not None:
        return await db.get(Revision, (workspace_id, key, revision))
    return await db.scalar(select(Revision).where(Revision.workspace_id == workspace_id, Revision.key == key)
                           .order_by(Revision.revision.desc()).limit(1))


async def copy_to_workspace(db, source_id, target_id):
    columns = ('workspace_id', 'key', 'revision', 'material_json', 'created_at')
    records = select(literal(target_id), Revision.key, Revision.revision, Revision.material_json, Revision.created_at)
    await db.execute(insert(Revision).from_select(columns, records.where(Revision.workspace_id == source_id)))


async def append(db, workspace_id, key, expected_revision, body):
    row = await get(db, workspace_id, key)
    head = row.revision if row else 0
    if row and row.material_json == body and expected_revision in {head-1, head}:
        return row
    if expected_revision != head:
        raise CaseWriteConflict('material_revision_conflict', '材料库版本已变化，请刷新后重试', current_revision=head)
    if row is None and body is None:
        raise FileNotFoundError(key)
    updated = Revision(workspace_id=workspace_id, key=key, revision=head+1, material_json=body)
    db.add(updated)
    await db.flush()
    return updated


def _literal(value):
    if isinstance(value, str):
        return casedict.LiteralDictString(value)
    if isinstance(value, dict):
        return {field: _literal(item) for field, item in value.items()}
    if isinstance(value, list):
        return [_literal(item) for item in value]
    return value


def apply(text, name, snapshot, workspace_id):
    current = casedict.parse_casedict(text).get('materials', {})
    if not isinstance(current, dict):
        raise ValueError('materials 必须是字典块')
    entries = current.get('entries', {})
    if not isinstance(entries, dict):
        raise ValueError('materials.entries 必须是字典块')
    keep_entries = dict.fromkeys(entries, casedict.UNCHANGED_DICT_ENTRY)
    keep_entries[name] = {**_literal(snapshot['material']), 'libraryRef': {
        'workspaceId': workspace_id, 'entry': snapshot['key'], 'revision': snapshot['revision'], 'sha256': snapshot['sha256']}}
    keep_fields = dict.fromkeys(current, casedict.UNCHANGED_DICT_ENTRY)
    keep_fields['entries'] = keep_entries
    return casedict.set_block(text, 'materials', keep_fields)
