"""Bound manual inspection and recovery to an authorized page of seal history."""
import base64
import hashlib
import json
import uuid

from botocore.exceptions import ClientError
from fastapi import HTTPException
from sqlalchemy import select

from .. import storage
from ..models import Job, JobAttempt
from . import attempt_objects, attempt_seal_journal as seals, attempt_workspace
from . import storage_maintenance as maintenance
from .case_provision import check_deadline
from .case_write import bounded_case_read
from .durable_events import json_text
from .s3_live_store import Client, missing


def binding(case, user_id, *, apply):
    value = [user_id, case.id, case.workspace_id, seals.binding(case.s3_prefix),
             str(storage._fs_root().resolve()), apply]
    return hashlib.sha256(json_text(value).encode('utf8')).hexdigest()


def _cursor(scope, phase, after):
    value = json_text([1, scope, phase, after]).encode('utf8')
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _position(cursor, scope, phases):
    if cursor is None:
        return next(iter(phases)), ''
    try:
        data = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        version, observed, phase, after = json.loads(data)
        if (type(version) is not int or version != 1 or observed != scope or phase not in phases
                or not isinstance(after, str) or len(after.encode('utf8')) > 1024
                or '/' in after.rstrip('/') or '\\' in after or '\x00' in after):
            raise ValueError()
        return phase, after
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(422, {'code':'invalid_attempt_maintenance_cursor',
                                  'message':'封存记录游标无效，请从第一页重试'}) from None


def select_page(prefix, scope, cursor, limit, apply):
    locations = maintenance.seal_directories()
    if not apply:
        if storage._use_s3():
            locations['untracked_remote'] = attempt_workspace.PREFIX
        locations['untracked_local'] = attempt_workspace.PREFIX
    first, after = _position(cursor, scope, locations)
    phases = list(locations)
    selections = []
    for phase in phases[phases.index(first):]:
        check_deadline()
        names, successor = maintenance.child_page(prefix, locations[phase], after if phase == first else '',
            max(1, limit), local=phase in {'node','untracked_local'}, paired=phase == 'writing')
        if not names:
            continue
        if not limit:
            return selections, _cursor(scope, phase, '')
        selections.append((phase, names))
        limit -= len(names)
        if successor:
            return selections, _cursor(scope, phase, successor)
    return selections, None


def _attempt_id(name):
    name = name.removeprefix('.preparing-')
    try:
        return name if str(uuid.UUID(name)) == name else None
    except ValueError:
        return None


def _record_exists(prefix, path):
    if not storage._use_s3():
        return seals.local(prefix, path).exists()
    try:
        storage.s3().head_object(**attempt_objects.arguments(prefix, path))
    except ClientError as error:
        if missing(error):
            return False
        raise
    return True


def _remote_directory(prefix, relative):
    client = storage.s3()
    raw = client.raw if isinstance(client, Client) else client
    options = attempt_objects.arguments(prefix, relative)
    key = options.pop('Key').rstrip('/') + '/'
    response = raw.list_objects_v2(**options, Prefix=key, MaxKeys=1)
    return bool(response.get('Contents'))


def _untracked_rows(prefix, phase, names, owned):
    result = []
    for entry in names:
        check_deadline()
        name = entry.rstrip('/')
        identifier = _attempt_id(name)
        if identifier in owned:
            continue
        if identifier is not None and any(_record_exists(prefix, seals.PREFIX+'/'+part+'/'+identifier+'.json')
                                          for part in ('pending','history')):
            continue
        path = attempt_workspace.PREFIX+'/'+name
        try:
            canonical = attempt_workspace.canonical_path(path) == path
        except ValueError:
            canonical = False  # Keep diagnostic evidence for legacy noncanonical names.
        if storage._use_s3() and canonical:
            if phase == 'untracked_remote' and entry.endswith('/') and _record_exists(prefix, path):
                continue  # A flat object with the same logical name was listed earlier.
            if phase == 'untracked_local' and (_record_exists(prefix, path) or _remote_directory(prefix, path)):
                continue  # The remote page already owns this name in the diagnostic.
        result.append({'path':path,'status':'untracked_retained'})
    return result


async def _untracked(db, case, phase, names):
    ids = {_attempt_id(name.rstrip('/')) for name in names}
    ids.discard(None)
    owned = set()
    if ids:
        query = select(JobAttempt.id).join(Job, Job.id == JobAttempt.job_id).where(
            Job.case_id == case.id, JobAttempt.id.in_(ids))
        owned = set(await db.scalars(query))
    return await bounded_case_read(_untracked_rows, case.s3_prefix, phase, names, owned,
                                   code='attempt_preparations',label='封存诊断')


async def execute(db, case, selections, *, apply):
    result = {'results':[]}
    if not apply:
        result['untracked'] = []
    for phase, names in selections:
        check_deadline()
        if phase.startswith('untracked_'):
            result['untracked'].extend(await _untracked(db, case, phase, names))
        else:
            result['results'].extend(await maintenance.recover_seal_names(db, case, phase, names, apply=apply))
    if not apply:
        result['untracked'].sort(key=lambda row:row['path'])
    return result
