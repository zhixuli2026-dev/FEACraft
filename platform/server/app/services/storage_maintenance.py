"""Account-scoped recovery and evidence-based retention; never age-delete case data."""
import asyncio
from dataclasses import dataclass
import hashlib
import heapq
import json
import logging
import math
import os
from pathlib import PurePosixPath
import re
import stat
import time
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, select

from .. import storage
from ..config import get_settings
from ..db import SessionLocal
from ..models import Case, DurableEvent, User, Workspace
from . import case_provision as provision
from .async_ops import finish_before_cancel
from . import file_transactions as journal
from . import uploads
from .case_write import CaseWriteConflict, assert_case_available, coordinate
from .durable_events import json_text

log = logging.getLogger(__name__)
OWNER_PAGE_SIZE = 32
CASE_PAGE_SIZE = 32
UPLOAD_PAGE_SIZE = 32
UPLOAD_DIRECTORY = '.vibeflow/uploads'
UPLOAD_CURSOR = UPLOAD_DIRECTORY + '/.maintenance.json'
UPLOAD_CURSOR_SCHEMA = 'vibeflow.upload-maintenance.v1'
PROVISION_PAGE_SIZE = 32
PROVISION_PHASES = ('staging', 'pending', 'history')
PROVISION_CURSOR = '.maintenance.json'
PROVISION_CURSOR_SCHEMA = 'vibeflow.provision-maintenance.v1'
SEAL_PAGE_SIZE = 32
SEAL_CURSOR = '.vibeflow/attempt-provisions/.maintenance.json'
SEAL_CURSOR_SCHEMA = 'vibeflow.seal-maintenance.v1'


def delete_record(prefix, path, raw, etag):
    """Revalidate the exact record; S3 deletion also checks its object version."""
    current, version = provision.read_record(prefix, path)
    if current != raw or version != etag:
        raise ValueError('Maintenance record changed')
    provision.check_deadline()
    if storage._use_s3():
        storage.s3().delete_object(**provision.objects.arguments(prefix, path), IfMatch=etag)
    else:
        provision.local_path(prefix, path).unlink()


def modified(prefix, path):
    if storage._use_s3():
        return storage.s3().head_object(**provision.objects.arguments(prefix, path))['LastModified'].timestamp()
    return provision.local_path(prefix, path).stat().st_mtime


@dataclass(frozen=True)
class StageCleanup:
    path: str
    part: str
    raw: bytes
    etag: str | None
    intent: dict
    observed: tuple


def observe_staging(user_id, now, paths=None):
    """Verify temporary bodies and process witnesses before write admission."""
    if storage._use_s3():
        return [], []  # Conditional object writes do not use this filesystem staging area.
    owner = provision.owner_prefix(user_id)
    if paths is None:
        names = {entry['path'] for entry in storage.list_tree(owner, 'staging')}
    else:
        root = provision.local_path(owner, 'staging')
        names = set()
        for path in paths:
            match = re.fullmatch(r'staging/([0-9a-f]{32})\.json', path)
            related = (path, f'staging/{match[1]}.part') if match else (path,)
            names.update(item for item in related if os.path.lexists(root / PurePosixPath(item).name))
    outcomes, prepared, handled = [], [], set()
    cutoff = now - get_settings().provision_stage_retention_seconds
    for path in sorted(names):
        provision.check_deadline()
        match = re.fullmatch(r'staging/([0-9a-f]{32})\.json', path)
        if not match:
            continue
        part = 'staging/' + match[1] + '.part'
        handled.update((path, part))
        try:
            raw, etag = provision.read_record(owner, path)
            intent = json.loads(raw)
            created = intent['created_at']
            expected = intent['file']
            if (intent['schema'] not in {'vibeflow.provision-stage.v1', 'vibeflow.provision-stage.v2'} or intent['id'] != match[1]
                    or intent['owner'] != owner or intent['binding'] != provision.binding(owner)
                    or type(created) not in (float, int) or not math.isfinite(created)
                    or set(expected) != {'size', 'sha256'} or type(expected['size']) is not int
                    or not 0 <= expected['size'] <= provision.MAX_FILE_BYTES
                    or not isinstance(expected['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', expected['sha256'])):
                raise ValueError('Staging identity differs')
            if intent['schema'] == 'vibeflow.provision-stage.v2':
                from .process_identity_native import probe
                validate_stage_target(user_id, intent['target'])
                process = probe(intent['process'])
                if process['state'] != 'exited':
                    outcomes.append({'path': path, 'status': 'active_writer' if process['state'] == 'match' else 'unverified_writer_retained'})
                    continue
            elif created > cutoff or modified(owner, path) > cutoff:
                outcomes.append({'path': path, 'status': 'retained_recent'})
                continue
            observed = capture_stage_part(user_id, part, intent)
            prepared.append(StageCleanup(path, part, raw, etag, intent, observed))
        except TimeoutError:
            raise
        except Exception as error:
            outcomes.append({'path': path, 'status': 'recovery_required', 'reason': type(error).__name__})
    outcomes.extend({'path': path, 'status': 'unowned_staging_retained'} for path in sorted(names - handled))
    return outcomes, prepared


def apply_staging(user_id, prepared):
    owner = provision.owner_prefix(user_id)
    outcomes = []
    for stage in prepared:
        try:
            provision.check_deadline()
            latest = provision.read_record(owner, stage.path)
            if latest[0] is None:
                continue
            if latest != (stage.raw, stage.etag) or stage_part_version(user_id, stage.part, stage.intent) != stage.observed:
                raise ValueError('Staging intent or file version changed during cleanup')
            target, _ = stage.observed
            if target is not None:
                target.unlink()
            delete_record(owner, stage.path, stage.raw, stage.etag)
            outcomes.append({'path': stage.path, 'status': 'cleaned'})
        except TimeoutError:
            raise
        except (ValueError, OSError, KeyError, TypeError) as error:
            outcomes.append({'path': stage.path, 'status': 'recovery_required', 'reason': type(error).__name__})
    return outcomes


def clean_staging(user_id, now, paths=None):
    """Compatibility helper for isolated process/legacy-stage checks."""
    outcomes, prepared = observe_staging(user_id, now, paths)
    return outcomes + apply_staging(user_id, prepared)


async def recover_stages(db, user_id, paths=None, *, wait=False):
    from .local_provision_budget import hold

    outcomes, prepared = await provision.io(observe_staging, user_id, time.time(), paths)
    if prepared:
        async with hold(db, user_id, wait=wait):
            await provision.lock_owner(db, user_id)
            outcomes.extend(await provision.io(apply_staging, user_id, prepared))
            await finish_before_cancel(db.commit())
    return outcomes


def validate_stage_target(user_id, target):
    """An installation target must belong to this account's creation protocol."""
    from .attempt_workspace import canonical_path, assert_mutable_path
    prefix, path = target['prefix'], target['path']
    if canonical_path(path) != path:
        raise ValueError('Noncanonical staging target')
    if prefix == provision.owner_prefix(user_id):
        if not re.fullmatch(r'(claims/[1-9][0-9]*|(?:pending|history)/[0-9a-f-]{36})\.json', path):
            raise ValueError('Unrecognized creation metadata target')
    elif re.fullmatch(r'workspaces/' + str(user_id) + r'/cases/(?:[0-9a-f]{32}/)?[1-9][0-9]*', prefix):
        assert_mutable_path(prefix, path)
    else:
        raise ValueError('Staging target belongs to another account or namespace')


def stage_part_version(user_id, part, intent):
    """Two links are admitted only for the exact temporary/installed file pair."""
    from . import local_file_edits

    owner = provision.owner_prefix(user_id)
    if intent['schema'] == 'vibeflow.provision-stage.v1':
        path = provision.local_path(owner, part)
        version = local_file_edits._version(path)
        return (path, version) if version is not None else (None, None)

    def leaf(prefix, relative):
        name = PurePosixPath(relative)
        parent = (journal._path(storage._fs_root(), prefix) if name.parent.as_posix() == '.' else
                  provision.local_path(prefix, name.parent.as_posix()))
        path = parent / name.name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or
                info.st_nlink not in {1, 2} or info.st_size > provision.MAX_FILE_BYTES):
            raise ValueError('Staging contains an unrecognized link or special file')
        return path, info
    try:
        path, info = leaf(owner, part)
    except FileNotFoundError:
        return None, None
    if info.st_nlink == 2:
        target, target_info = leaf(intent['target']['prefix'], intent['target']['path'])
        if target_info.st_nlink != 2 or not os.path.samefile(path, target):
            raise ValueError('Staging link differs from the installed target')
    changed = local_file_edits._change_time(path) if os.name == 'nt' else info.st_ctime_ns
    version = _stage_identity(info)
    if _stage_identity(path.lstat()) != version:
        raise ValueError('Staging file changed during version inspection')
    return path, (*version, changed)


def _stage_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)


def capture_stage_part(user_id, part, intent):
    from .attempt_workspace import digest_file
    from .request_deadline import current

    observed = stage_part_version(user_id, part, intent)
    path, _ = observed
    if path is not None and (digest_file(path, deadline=current()) != intent['file']
                            or stage_part_version(user_id, part, intent) != observed):
        raise ValueError('Staging file is incomplete or changed')
    return observed


def empty_case_directories(prefix, paths):
    """rmdir only: a concurrent file or unrecognized link prevents removal."""
    if storage._use_s3():
        return
    empty_local_directories(prefix, paths)


def empty_local_directories(prefix, paths):
    """Remove proven empty local directories, including S3 execution caches."""
    root = journal._path(storage._fs_root(), prefix)
    if not root.exists():
        return
    def remove_empty(path):
        deadline = time.monotonic() + 2
        while True:
            # Windows indexers may briefly retain a directory handle. Retry
            # only sharing violations, with path validation on every attempt.
            journal._path(storage._fs_root(), path.relative_to(storage._fs_root()).as_posix())
            try:
                path.rmdir()
                return
            except FileNotFoundError:
                return
            except OSError as error:
                if getattr(error, 'winerror', None) not in {32, 33} or time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
    directories = set()
    for relative in paths:
        parent = journal._path(root, relative).parent
        while parent != root:
            directories.add(parent)
            parent = parent.parent
    for path in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        journal._path(storage._fs_root(), path.relative_to(storage._fs_root()).as_posix())
        if path.is_dir():
            remove_empty(path)
    remove_empty(root)


async def clean_history(db, user_id, now, paths=None):
    days = get_settings().provision_history_retention_days
    if not days:
        return []
    owner = provision.owner_prefix(user_id)
    conditions_ready=False
    outcomes = []
    if paths is None:
        paths = [entry['path'] for entry in await provision.io(storage.list_tree, owner, 'history')]
    for path in paths:
        provision.check_deadline()
        try:
            match = re.fullmatch(r'history/([0-9a-f-]{36})\.json', path)
            if not match or str(provision.uuid.UUID(match[1])) != match[1]:
                raise ValueError('Invalid history identity')
            identifier = match[1]
            if await provision.io(modified, owner, path) > now - days * 86400:
                continue
            raw, etag = await provision.io(provision.read_record, owner, path)
            receipt = json.loads(raw); plan = receipt['plan']
            await provision.io(provision.validate_plan, user_id, identifier, plan)
            pending, _ = await provision.io(provision.read_record, owner, f'pending/{identifier}.json')
            if pending is not None:
                outcomes.append({'path': path, 'status': 'pending_retained'})
                continue
            witness = await db.get(DurableEvent, identifier, populate_existing=True)
            prefix = plan['binding']['prefix']
            if receipt['outcome'] == 'committed':
                expected = {'provision_id': identifier, 'prefix': prefix,
                            'plan_sha256': hashlib.sha256(json_text(plan).encode()).hexdigest()}
                if (witness is None or witness.type != provision.EVENT or witness.case_id != plan['case_id']
                        or json.loads(witness.payload_json) != expected):
                    raise ValueError('History lacks matching committed proof')
            elif receipt['outcome'] == 'rolled_back':
                referenced = await db.scalar(select(Case.id).where(Case.s3_prefix == prefix).limit(1))
                if witness is not None or referenced is not None or await provision.io(storage.list_tree, prefix):
                    raise ValueError('Rolled-back history has live content or references')
                await provision.io(empty_case_directories, prefix, plan['files'])
            else:
                raise ValueError('Unknown history outcome')
            if storage._use_s3() and not conditions_ready:
                from .s3_case_files import require_conditions
                await provision.io(require_conditions, owner)
                conditions_ready=True
            await provision.io(delete_record, owner, path, raw, etag)
            outcomes.append({'path': path, 'status': 'expired_history_removed'})
        except TimeoutError:
            raise
        except Exception as error:
            outcomes.append({'path': path, 'status': 'recovery_required', 'reason': type(error).__name__})
    return outcomes


def upload_page(prefix, after):
    """Collect one page of upload candidates without reading their children."""
    limit = UPLOAD_PAGE_SIZE
    if storage._use_s3():
        from .s3_live_store import Client
        client = storage.s3()
        if isinstance(client, Client):
            client = client.raw  # The logical file catalog excludes private upload staging.
        stem = storage._s3_key(prefix, UPLOAD_DIRECTORY) + '/'
        page = client.list_objects_v2(Bucket=get_settings().s3_bucket, Prefix=stem, Delimiter='/',
                                      StartAfter=stem + after, MaxKeys=limit + 1)
        keys = [row['Prefix'] for row in page.get('CommonPrefixes', [])]
        keys.extend(row['Key'] for row in page.get('Contents', []))
        if any(not key.startswith(stem) or key <= stem + after for key in keys):
            raise ValueError('Upload listing did not respect its page boundary')
        names = sorted(key[len(stem):] for key in keys)
        more = bool(page.get('IsTruncated')) or len(names) > limit
        if more and not names:
            raise ValueError('Upload listing did not advance')
        selected = names[:limit]
        identifiers = [name[:-1] for name in selected if re.fullmatch(r'[0-9a-f]{32}/', name)]
    else:
        from .upload_recovery_source import budget
        base = provision.local_path(prefix, UPLOAD_DIRECTORY)
        try:
            entries = os.scandir(base)
        except FileNotFoundError:
            return [], ''
        # Filesystem enumeration is unordered. Keep only one page of immediate
        # names while scanning; do not traverse or open any session's staging.
        def candidates():
            for entry in entries:
                budget()
                if entry.name > after and re.fullmatch(r'[0-9a-f]{32}(?:\.completing)?', entry.name):
                    yield entry.name
        with entries:
            names = heapq.nsmallest(limit + 1, candidates())
        more = len(names) > limit
        selected = names[:limit]
        identifiers = [name.removesuffix('.completing') for name in selected]
    return list(dict.fromkeys(identifiers)), selected[-1] if more else ''


def read_cursor(prefix, path, local=False):
    if storage._use_s3() and not local:
        from .s3_file_transactions import _read
        raw, etag, _ = _read(prefix, path, 4096)
    else:
        target = provision.local_path(prefix, path)
        try:
            with target.open('rb') as stream:
                raw = stream.read(4097)
        except FileNotFoundError:
            raw = None
        etag = None
    if raw is not None and len(raw) > 4096:
        raise ValueError('Oversized maintenance cursor')
    return raw, etag


def read_upload_cursor(prefix):
    raw, etag = read_cursor(prefix, UPLOAD_CURSOR)
    if raw is None:
        return '', None, None
    value = json.loads(raw)
    after = value['after']
    if (value['schema'] != UPLOAD_CURSOR_SCHEMA or not isinstance(after, str) or
            len(after.encode('utf8')) > 1024 or '/' in after.rstrip('/')):
        raise ValueError('Invalid upload maintenance cursor')
    return after, raw, etag


def write_cursor(prefix, path, before, value, local=False):
    if before != read_cursor(prefix, path, local):
        raise ValueError('Maintenance cursor changed')
    raw = json_text(value).encode('utf8')
    if storage._use_s3() and not local:
        from .s3_case_files import require_conditions
        from .s3_file_transactions import _conditional
        require_conditions(prefix)
        provision.check_deadline()
        _conditional(prefix, path, raw, before[1], {'ContentType': 'application/json'})
    else:
        from .fs_directory import retry_sharing_violation
        target = provision.local_path(prefix, path)
        temporary = target.with_name('.maintenance-' + uuid.uuid4().hex + '.tmp')
        try:
            provision.check_deadline()
            journal._write_new(temporary, raw)
            provision.check_deadline()
            retry_sharing_violation(lambda: os.replace(temporary, target))
            journal._sync(target.parent)
        finally:
            temporary.unlink(missing_ok=True)


def write_upload_cursor(prefix, before, after):
    write_cursor(prefix, UPLOAD_CURSOR, before[1:], {'schema': UPLOAD_CURSOR_SCHEMA, 'after': after})


def child_page(prefix, directory, after, limit, *, local=False, paired=False):
    """Select direct children. Paired local intents and payloads share a cursor position."""
    if storage._use_s3() and not local:
        from .s3_live_store import Client
        client = storage.s3()
        if isinstance(client, Client):
            client = client.raw
        stem = storage._s3_key(prefix, directory) + '/'
        response = client.list_objects_v2(Bucket=get_settings().s3_bucket, Prefix=stem,
                                          Delimiter='/', StartAfter=stem + after, MaxKeys=limit + 1)
        keys = [row['Key'] for row in response.get('Contents', [])]
        keys.extend(row['Prefix'] for row in response.get('CommonPrefixes', []))
        if any(not key.startswith(stem) or key <= stem + after for key in keys):
            raise ValueError('Constructor listing returned an invalid page')
        names = sorted(key[len(stem):] for key in keys)
        more = bool(response.get('IsTruncated')) or len(names) > limit
    else:
        from .storage_listing_budget import ListingBudget
        budget = ListingBudget()
        try:
            entries = os.scandir(provision.local_path(prefix, directory))
        except FileNotFoundError:
            return [], ''

        def candidates():
            for entry in entries:
                budget.visit()
                name = entry.name
                if paired and re.fullmatch(r'[0-9a-f]{32}\.part', name):
                    name = name.removesuffix('.part') + '.json'
                if name > after:
                    yield name

        with entries:
            names = heapq.nsmallest(limit + 1, candidates())
        more = len(names) > limit
    if more and not names:
        raise ValueError('Constructor listing did not advance')
    selected = list(dict.fromkeys(names[:limit]))
    return selected, (selected[-1] if more else '')


def provision_page(owner, phase, after):
    names, next_after = child_page(owner, phase, after, PROVISION_PAGE_SIZE, paired=phase == 'staging')
    return [phase + '/' + name for name in names], next_after


def provision_plan(user_id):
    """Discover candidate pages before obtaining an owner write transaction."""
    owner = provision.owner_prefix(user_id)
    before = read_cursor(owner, PROVISION_CURSOR)
    phase, after = 'staging', ''
    if before[0] is not None:
        value = json.loads(before[0])
        phase, after = value['phase'], value['after']
        if (value['schema'] != PROVISION_CURSOR_SCHEMA or value['owner'] != owner or phase not in PROVISION_PHASES
                or not isinstance(after, str) or len(after.encode('utf8')) > 1024 or '/' in after.rstrip('/')):
            raise ValueError('Invalid constructor maintenance cursor')
    pages = []
    for directory in PROVISION_PHASES[PROVISION_PHASES.index(phase):]:
        provision.check_deadline()
        if (directory == 'staging' and storage._use_s3()
                or directory == 'history' and not get_settings().provision_history_retention_days):
            continue
        paths, next_after = provision_page(owner, directory, after if directory == phase else '')
        pages.append((directory, paths))
        if next_after:
            return before, pages, (directory, next_after)
    return before, pages, None


async def maintain_provisions(db, user_id, result):
    from .case_copy_io import admit
    from .request_deadline import scope as deadline_scope
    async with deadline_scope(get_settings().storage_measurement_timeout_seconds):
        with admit(code='case_provision_busy', label='构造恢复'):
            return await _maintain_provision_page(db, user_id, result)


async def _maintain_provision_page(db, user_id, result):
    from .local_provision_budget import hold as local_capacity
    from .owner_storage_budget import hold as owner_capacity

    owner = provision.owner_prefix(user_id)
    before, pages, next_page = await provision.io(provision_plan, user_id)
    if not any(paths for _, paths in pages) and before[0] is None:
        return None
    async with owner_capacity(db, user_id):
        if before != await provision.io(read_cursor, owner, PROVISION_CURSOR):
            return None
        # Each observed record is applied under its own short transaction.
        # The cursor may only advance against the original page revision.
        for phase, paths in pages:
            if paths and phase == 'staging':
                result['staging'].extend(await recover_stages(db, user_id, paths))
            elif paths and phase == 'pending':
                result['creation'].extend(await provision.recover_pending(db, user_id, paths))
        async with local_capacity(db, user_id):
            await provision.lock_owner(db, user_id)
            if before != await provision.io(read_cursor, owner, PROVISION_CURSOR):
                await finish_before_cancel(db.rollback())
                return None  # This page is already superseded by another API.
            for phase, paths in pages:
                if paths and phase == 'history':
                    result['history'].extend(await clean_history(db, user_id, time.time(), paths))
            phase, after = next_page or ('staging', '')
            if next_page or before[0] is not None:
                await provision.io(write_cursor, owner, PROVISION_CURSOR, before,
                                   {'schema': PROVISION_CURSOR_SCHEMA, 'owner': owner, 'phase': phase, 'after': after})
            await finish_before_cancel(db.commit())
    return {'phase': phase, 'next_after': after} if next_page else None


def seal_directories():
    from . import attempt_seal_journal as seals, attempt_workspace
    if storage._use_s3():
        return {'pending': seals.PREFIX+'/pending', 'node': attempt_workspace.PREFIX}
    return {'writing': seals.PREFIX+'/writing', 'pending': seals.PREFIX+'/pending'}


def seal_page(prefix, case_id):
    from . import attempt_seal_journal as seals
    phases = seal_directories()
    binding = {'case_id': case_id, 'store': seals.binding(prefix), 'node': str(storage._fs_root().resolve())}
    before = read_cursor(prefix, SEAL_CURSOR, True)
    first = next(iter(phases))
    old = json.loads(before[0]) if before[0] is not None else None
    phase, after = (first, '') if old is None else (old['phase'], old['after'])
    if old is not None and (old['schema'] != SEAL_CURSOR_SCHEMA or old['binding'] != binding
            or phase not in phases or not isinstance(after, str) or len(after.encode('utf8')) > 1024
            or '/' in after.rstrip('/') or '\\' in after):
        raise ValueError('Invalid attempt maintenance cursor')

    ordered = list(phases)
    for index in range(ordered.index(phase), len(ordered)):
        selected = ordered[index]
        names, remaining = child_page(prefix, phases[selected], after if selected == phase else '', SEAL_PAGE_SIZE,
                                      local=selected == 'node', paired=selected == 'writing')
        if names:
            following = (selected, remaining) if remaining else (ordered[(index+1) % len(ordered)], '')
            break
    else:
        selected, names, following = first, [], (first, '')
    next_phase, next_after = following
    value = {'schema': SEAL_CURSOR_SCHEMA, 'binding': binding, 'phase': next_phase, 'after': next_after}
    if not names and (old is None or old == value):
        return None
    return before, selected, names, value if old != value else None


async def recover_seal_names(db, case, phase, names, *, apply=True):
    from . import attempt_plan_writes, attempt_seal_journal as seals
    prefix = case.s3_prefix
    if phase == 'writing':
        return await journal._io(attempt_plan_writes.recover, prefix, selected=names, apply=apply)
    if phase == 'pending':
        paths = [seals.PREFIX+'/pending/'+name for name in names]
        return await seals.recover_pending(db, case, paths, apply=apply)
    return await seals.recover_node_caches(db, case, names=names, apply=apply)


async def recover_seal_page(db, case, prefix, page):
    if page is None:
        return []
    before, phase, names, value = page
    if before != await provision.io(read_cursor, prefix, SEAL_CURSOR, True):
        return []  # Another request advanced the same node's page under this lock.
    results = await recover_seal_names(db, case, phase, names)
    if value is not None:
        await provision.io(write_cursor, prefix, SEAL_CURSOR, before, value, True)
    return results


async def upload_receipts(db, case, candidates):
    receipts = {}
    for identifier in candidates:
        try:
            receipts[identifier] = await uploads.completion_record(db, case, identifier)
        except (ValueError, KeyError, TypeError) as error:
            receipts[identifier] = error
    return receipts


async def observe_uploads(db, case, prefix, candidates, observe):
    receipts = await upload_receipts(db, case, candidates)
    await db.commit()
    observed = {}
    for identifier, receipt in receipts.items():
        if receipt is None or isinstance(receipt, Exception):
            continue
        try:
            proof = await provision.io(observe, prefix, identifier, receipt)
            observed[identifier] = (receipt, proof)
        except TimeoutError:
            raise
        except (OSError, ValueError, KeyError, TypeError, CaseWriteConflict, BotoCoreError, ClientError):
            pass  # A changed or unverifiable part stays in place.
    return observed


async def clean_upload_page(case_id, prefix, before, after, receipts, observations, cleanup):
    outcomes = []
    if (receipts or after != before[0]) and before != await provision.io(read_upload_cursor, prefix):
        return outcomes
    for identifier, receipt in receipts.items():
        try:
            if isinstance(receipt, Exception):
                raise receipt
            if receipt is None:
                status = 'incomplete_upload_retained'
            else:
                original, proof = observations.get(identifier, (None, None))
                cleaned = original == receipt and await provision.io(cleanup, prefix, identifier, receipt, proof)
                status = 'completed_upload_cleaned' if cleaned else 'recovery_required'
            outcomes.append({'case_id': case_id, 'upload_id': identifier, 'status': status})
        except TimeoutError:
            raise
        except Exception as error:
            outcomes.append({'case_id': case_id, 'upload_id': identifier,
                             'status': 'recovery_required', 'reason': type(error).__name__})
    if after != before[0]:
        try:
            await provision.io(write_upload_cursor, prefix, before, after)
        except Exception as error:
            outcomes.append({'case_id': case_id, 'status': 'recovery_required',
                             'reason': type(error).__name__})
    return outcomes


async def clean_uploads(db, case, user_id, prefix, before, candidates, after, seal_scan, attempt_outcomes,
                        local_observations):
    async with coordinate(db, case, content_write=False):
        workspace = await db.get(Workspace, case.workspace_id, populate_existing=True)
        if case.s3_prefix != prefix or workspace is None or workspace.user_id != user_id:
            raise ValueError('Maintenance case changed its storage binding')
        attempt_outcomes.extend(await recover_seal_page(db, case, prefix, seal_scan))
        receipts = await upload_receipts(db, case, candidates)
        return await clean_upload_page(case.id, prefix, before, after, receipts, local_observations,
                                       uploads.cleanup_completed)


async def clean_remote_uploads(case_id, user_id, prefix, before, candidates, after, observations):
    from . import s3_uploads, storage_operations
    from .s3_case_files import require_conditions
    from .s3_storage_recovery import recover_reserved

    async def checked(db):
        case = await db.scalar(select(Case).join(Workspace).where(
            Case.id == case_id, Workspace.user_id == user_id).execution_options(populate_existing=True))
        assert_case_available(case)
        if case.s3_prefix != prefix:
            raise ValueError('Maintenance case changed its storage binding')
        return case

    async with storage_operations.hold(checked, purpose='staging') as lease:
        await recover_reserved(lease)
        async with lease.control() as db:
            receipts = await upload_receipts(db, lease.case, candidates)
        if any(proof is not None and receipts.get(identifier) == original
               for identifier, (original, proof) in observations.items()):
            await provision.io(require_conditions, prefix)
        return await clean_upload_page(case_id, prefix, before, after, receipts, observations, s3_uploads.apply_completed)


async def has_case_work(db, case):
    """A negative read only skips this pass; any later journal stays for the next one."""
    if not storage._use_s3():
        return True
    from .s3_file_transactions import PENDING
    return await provision.io(storage.exists, case.s3_prefix, PENDING)


async def maintain_owner(db, user_id, *, after_case_id=0, through_case_id=None):
    """Use one admission slot and one deadline for the complete maintenance page."""
    from .case_copy_io import admit
    from .case_read_capacity import file_read_deadline

    async with file_read_deadline('storage_maintenance', '存储维护'):
        with admit(code='storage_maintenance_busy', label='存储维护'):
            return await _maintain_owner_page(db, user_id, after_case_id, through_case_id)


async def _maintain_owner_page(db, user_id, after_case_id, through_case_id):
    from .owner_storage_budget import hold as owner_capacity

    cases = select(Case.id).join(Workspace).where(Workspace.user_id == user_id)
    if through_case_id is None:
        through_case_id = await db.scalar(select(func.max(Case.id)).join(Workspace).where(Workspace.user_id == user_id)) or 0
        await db.commit()

    result = {name: [] for name in ('creation', 'staging', 'history', 'uploads', 'attempts')}
    result['provision_scan'] = None
    if not after_case_id:
        try:
            result['provision_scan'] = await maintain_provisions(db, user_id, result)
        except (ValueError, OSError, KeyError, TypeError, ClientError) as error:
            await db.rollback()
            result['creation'].append({'status': 'recovery_required', 'reason': type(error).__name__})

    # Reads precede write admission. Each remote upload write receives its own durable claim.
    identifiers = list(await db.scalars(cases.where(Case.id > after_case_id, Case.id <= through_case_id)
                                       .order_by(Case.id).limit(CASE_PAGE_SIZE + 1)))
    await db.commit()
    more = len(identifiers) > CASE_PAGE_SIZE
    identifiers = identifiers[:CASE_PAGE_SIZE]
    result['case_scan'] = {'through_case_id': through_case_id, 'next_after_case_id': identifiers[-1] if more else None}
    from .request_deadline import scope as deadline_scope
    from .trash import is_case_deleted
    for identifier in identifiers:
        provision.check_deadline()
        try:
            case = await db.scalar(select(Case).join(Workspace).where(Case.id == identifier, Workspace.user_id == user_id)
                                   .execution_options(populate_existing=True))
            if case is None or is_case_deleted(identifier):
                await db.rollback()
                continue
            prefix = case.s3_prefix
            await db.commit()
            try:
                async with deadline_scope(get_settings().storage_measurement_timeout_seconds):
                    before = await provision.io(read_upload_cursor, prefix)
                    candidates, after = await provision.io(upload_page, prefix, before[0])
            except Exception as error:
                # Upload discovery is independent of the file and seal journals.
                result['uploads'].append({'case_id': identifier, 'status': 'recovery_required',
                                          'reason': type(error).__name__})
                before, candidates, after = ('', None, None), [], ''
            async with deadline_scope(get_settings().storage_measurement_timeout_seconds):
                try:
                    seal_scan = await provision.io(seal_page, prefix, identifier)
                except TimeoutError:
                    raise
                except Exception as error:
                    result['attempts'].append({'case_id': identifier, 'status': 'recovery_required',
                                               'reason': type(error).__name__})
                    seal_scan = None
                if storage._use_s3():
                    from .s3_uploads import observe_completed

                    if candidates or after != before[0] or await has_case_work(db, case):
                        observations = await observe_uploads(db, case, prefix, candidates, observe_completed)
                        result['uploads'].extend(await clean_remote_uploads(
                            identifier, user_id, prefix, before, candidates, after, observations))
                    if seal_scan is None:
                        continue
                    candidates, after, observations = [], before[0], {}
                else:
                    observations = await observe_uploads(db, case, prefix, candidates, uploads._observe_completed_part)
                async with owner_capacity(db, user_id):
                    result['uploads'].extend(await clean_uploads(db, case, user_id, prefix, before, candidates,
                                                                 after, seal_scan, result['attempts'], observations))
                    await db.commit()
        except asyncio.CancelledError:
            await db.rollback()
            raise
        except Exception as error:
            await db.rollback()
            result['uploads'].append({'case_id': identifier, 'status': 'recovery_required', 'reason': type(error).__name__})
    return result


async def maintain_accounts(stop):
    async with SessionLocal() as db:
        through_user = await db.scalar(select(func.max(User.id))) or 0
    cursor = 0
    while not stop.is_set() and cursor < through_user:
        async with SessionLocal() as db:
            identifiers = list(await db.scalars(select(User.id).where(User.id > cursor, User.id <= through_user)
                                                .order_by(User.id).limit(OWNER_PAGE_SIZE)))
        if not identifiers:
            break
        cursor = identifiers[-1]
        pages = [(identifier, 0, None) for identifier in identifiers]
        while pages and not stop.is_set():
            pending, pages = pages, []
            for identifier, after, through in pending:
                if stop.is_set():
                    break
                try:
                    async with SessionLocal() as db:
                        result = await maintain_owner(db, identifier, after_case_id=after, through_case_id=through)
                    scan = result.pop('case_scan')
                    result.pop('provision_scan')
                    if scan['next_after_case_id'] is not None:
                        pages.append((identifier, scan['next_after_case_id'], scan['through_case_id']))
                    unresolved = sum(row['status'] == 'recovery_required' for rows in result.values() for row in rows)
                    if unresolved:
                        log.warning('Storage maintenance needs review: owner=%s records=%s', identifier, unresolved)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log.warning('Storage maintenance deferred: owner=%s reason=%s', identifier, type(error).__name__)


async def run_forever(stop):
    """Start immediately and interleave bounded local cache work with account maintenance."""
    from .private_cache import RecoverySweep
    from .reservation_history import compact_page

    sweep = RecoverySweep()
    history_cursors = {}
    try:
        while not stop.is_set():
            if sweep.complete:
                sweep = RecoverySweep()
            try:
                cache_results = await provision.io(sweep.page)
                unresolved = sum(row['status'] in {'recovery_required', 'deferred'} for row in cache_results)
                if unresolved:
                    log.warning('Private cache maintenance needs review: records=%s', unresolved)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('Private cache maintenance deferred: reason=%s', type(error).__name__)
            await maintain_accounts(stop)
            if not stop.is_set():
                try:
                    for ledger, report in (await compact_page(history_cursors)).items():
                        if report['rewritten']:
                            log.info('Reservation history compacted: ledger=%s rows=%s text_bytes_saved=%s',
                                     ledger, report['rewritten'], report['bytes_saved'])
                        if report['unreadable']:
                            log.warning('Reservation history retained: ledger=%s unreadable=%s', ledger, report['unreadable'])
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log.warning('Reservation history compaction deferred: reason=%s', type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=get_settings().storage_maintenance_interval_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        sweep.close()  # The worker thread has drained before cancellation reaches here.
