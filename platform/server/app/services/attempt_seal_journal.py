"""Sealed-input preparation plans and SQL proof, serialized by the case lock."""
import hashlib
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import select

from .. import storage
from ..models import DurableEvent, Job, JobAttempt
from . import attempt_workspace as workspace, attempt_objects as objects
from . import file_transactions as journal
from .durable_events import append_event, json_text
from .request_deadline import current

PREFIX = '.vibeflow/attempt-provisions'
SCHEMA = 'vibeflow.attempt-seal-plan.v1'
DRAFT_SCHEMA = 'vibeflow.attempt-seal-draft.v1'
EVENT = 'attempt.inputs_sealed'
MAX_RECORD = 64 * 1024 * 1024


def event_id(identifier):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'vibeflow:attempt-seal:' + identifier))


def binding(prefix):
    if storage._use_s3():
        return objects.binding(prefix)
    return {'backend': 'fs', 'prefix': prefix, 'root': str(storage._fs_path(prefix, '.'))}


def local(prefix, path):
    return journal._path(storage._fs_root(), prefix + '/' + path)


def read(prefix, path):
    if storage._use_s3():
        from .s3_file_transactions import _read
        raw, etag, _ = _read(prefix, path, MAX_RECORD)
        return raw, etag
    try:
        with local(prefix, path).open('rb') as stream:
            data = stream.read(MAX_RECORD + 1)
    except FileNotFoundError:
        return None, None
    deadline = current()
    if deadline is not None:
        deadline.check()
    if len(data) > MAX_RECORD:
        raise ValueError('Oversized sealed-input recovery record')
    return data, None


def put(prefix, path, data):
    if len(data) > MAX_RECORD:
        raise ValueError('Sealed-input plan exceeds 64 MiB')
    if storage._use_s3():
        objects.put_new(prefix, path, data)
        return
    from .attempt_plan_writes import put as install
    install(prefix, path, data)


def inventories(manifest, remote=False):
    files = {side + '/' + path: {key: row[key] for key in ('size', 'sha256')}
             for side in (('inputs',) if remote else ('inputs', 'run')) for path, row in manifest['files'].items()}
    encoded = (json_text(manifest) if remote else json.dumps(manifest, ensure_ascii=False, sort_keys=True)).encode('utf8')
    files['manifest.json'] = objects.digest(encoded)
    return files


def prepare(case, attempt, snapshot):
    selected = snapshot.local if storage._use_s3() else snapshot
    return prepare_manifest(case,attempt,selected.describe())


def prepare_manifest(case,attempt,manifest):
    root=storage._fs_path(case.s3_prefix,'.')
    relative=f'{workspace.PREFIX}/{attempt.id}'
    if (root/relative).exists() or (root/workspace.PREFIX/('.preparing-'+attempt.id)).exists():
        raise ValueError('Attempt preparation destination already exists')
    if storage._use_s3() and storage.list_tree(case.s3_prefix, relative):
        raise ValueError('Attempt object destination already exists')
    plan = describe_plan(case, attempt, manifest)
    put(case.s3_prefix, PREFIX + '/pending/' + attempt.id + '.json', json_text(plan).encode('utf8'))
    return plan


def describe_plan(case, attempt, manifest):
    schema = DRAFT_SCHEMA if attempt.job_id is None else SCHEMA
    return {'schema': schema, 'attempt_id': attempt.id, 'case_id': case.id, 'job_id': attempt.job_id,
            'binding': binding(case.s3_prefix), 'manifest': manifest}


def validate(case, identifier, plan):
    from .tree_metadata_native import PATH as mode_path
    job_id = plan['job_id']
    valid_job = (storage._use_s3() and job_id is None if plan['schema'] == DRAFT_SCHEMA
                 else plan['schema'] == SCHEMA and type(job_id) is int and job_id > 0)
    if (str(uuid.UUID(identifier)) != identifier or not valid_job
            or plan['attempt_id'] != identifier or plan['case_id'] != case.id
            or plan['binding'] != binding(case.s3_prefix)):
        raise ValueError('Sealed-input plan identity differs')
    manifest = plan['manifest'];base = workspace.PREFIX + '/' + identifier
    if (manifest['schema'] != workspace.SCHEMA or manifest['attempt_id'] != identifier
            or manifest['inputs_path'] != base + '/inputs' or manifest['run_path'] != base + '/run'
            or manifest['source']['case_id'] != case.id or manifest['source']['job_id'] != plan['job_id']
            or not isinstance(manifest['files'], dict) or len(manifest['files']) > workspace.MAX_FILES
            or not isinstance(manifest['directories'], list)):
        raise ValueError('Sealed-input manifest differs')
    if storage._use_s3() and manifest.get('storage') != binding(case.s3_prefix):
        raise ValueError('Sealed-input object binding differs')
    generated=manifest.get('generated_inputs',[])
    if (not isinstance(generated,list) or any(not isinstance(path,str) or path not in manifest['files'] for path in generated)
            or generated!=sorted(set(generated))):
        raise ValueError('Invalid generated sealed-input selection')
    private_files={path for path in generated if path.rpartition('/')[0]=='.vibeflow/prepared'}
    private_directories={'.vibeflow','.vibeflow/prepared'} if private_files else set()
    if mode_path in manifest['files']:
        private_files.add(mode_path)
        private_directories.add('.vibeflow')
    total = 0
    for path, expected in manifest['files'].items():
        if (workspace.canonical_path(path) != path or
                (path.split('/')[0].casefold()=='.vibeflow' and
                 (path not in private_files or expected.get('executable') is not False))):
            raise ValueError('Invalid sealed-input path')
        if (type(expected['size']) is not int or not 0 <= expected['size'] <= workspace.MAX_BYTES
                or not isinstance(expected['sha256'], str) or len(expected['sha256']) != 64
                or any(char not in '0123456789abcdef' for char in expected['sha256'])):
            raise ValueError('Invalid sealed-input metadata')
        total += expected['size']
    if total > workspace.MAX_BYTES:
        raise ValueError('Sealed-input size exceeds original limit')
    for path in manifest['directories']:
        if (workspace.canonical_path(path) != path or
                (path.split('/')[0].casefold()=='.vibeflow' and path not in private_directories)):
            raise ValueError('Invalid sealed-input directory')


def proof(plan):
    return {'attempt_id': plan['attempt_id'], 'plan_sha256': hashlib.sha256(json_text(plan).encode()).hexdigest()}


async def witness(db, case, attempt, plan):
    if json.loads(attempt.input_manifest_json).get('workspace') != plan['manifest']:
        raise ValueError('Sealed-input SQL manifest differs from plan')
    await append_event(db, case.id, EVENT, proof(plan), attempt_id=attempt.id, event_id=event_id(attempt.id))
    db.info.setdefault('attempt_seals', {})[attempt.id] = db.get_transaction()


def directory_paths(manifest):
    return list(inventories(manifest)) + [side + '/' + name + '/.empty-placeholder'
                                        for side in ('inputs', 'run') for name in manifest['directories']]


def inspect_local(prefix, relative, expected, paths=None):
    base = local(prefix, relative)
    if not base.exists():
        return []
    allowed = {'inputs', 'run'}
    for name in paths or expected:
        parts = name.split('/')
        allowed.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    found = {}
    deadline = current()
    def walk_error(error):
        raise error
    for directory, dirs, names in os.walk(base, followlinks=False, onerror=walk_error):
        for name in dirs + names:
            if deadline is not None:
                deadline.check()
            path = Path(directory) / name
            workspace.ordinary(path)
            name = path.relative_to(base).as_posix()
            if path.is_dir():
                if name not in allowed:
                    raise ValueError('Uncommitted attempt contains unowned directories')
            else:
                found[name] = path
    if set(found) - set(expected):
        raise ValueError('Uncommitted attempt contains unowned files')
    result = []
    for name, path in found.items():
        local(prefix, relative + '/' + name)
        if journal._capture(path, deadline=current()) != expected[name]:
            raise ValueError('Uncommitted attempt is partial or externally changed')
        result.append((path, expected[name]))
    return result


def rollback_local(plan):
    prefix = plan['binding']['prefix']; identifier = plan['attempt_id']
    relatives = (workspace.PREFIX + '/.preparing-' + identifier, workspace.PREFIX + '/' + identifier)
    expected = inventories(plan['manifest']); paths = directory_paths(plan['manifest'])
    pending = [item for relative in relatives for item in inspect_local(prefix, relative, expected, paths)]
    deadline = current()
    for path, metadata in pending:
        local(prefix, path.relative_to(storage._fs_path(prefix, '.')).as_posix())
        if journal._capture(path, deadline=deadline) != metadata:
            raise ValueError('Attempt changed during cache cleanup')
        if deadline is not None:
            deadline.check()
        path.unlink()
    from .storage_maintenance import empty_local_directories
    for relative in relatives:
        empty_local_directories(prefix + '/' + relative, paths + ['inputs/.empty-placeholder', 'run/.empty-placeholder'])


def rollback(plan):
    """Preflight all relevant locations before deleting any admitted file."""
    prefix = plan['binding']['prefix'];identifier = plan['attempt_id'];manifest = plan['manifest']
    base = workspace.PREFIX + '/' + identifier
    stage = workspace.PREFIX + '/.preparing-' + identifier
    expected = inventories(manifest)
    paths = directory_paths(manifest)
    pending = inspect_local(prefix, stage, expected, paths) + inspect_local(prefix, base, expected, paths)
    remote = []
    if storage._use_s3():
        from .s3_streams import snapshot
        targets = inventories(manifest, remote=True)
        if plan['schema'] == DRAFT_SCHEMA:
            del targets['manifest.json']
        actual = {row['path'][len(base)+1:] for row in storage.list_tree(prefix, base)}
        if actual - set(targets):
            raise ValueError('Uncommitted remote attempt contains unowned files')
        for path in actual:
            observed, etag, _ = snapshot(prefix, base + '/' + path, limit=workspace.MAX_BYTES)
            if observed != targets[path]:
                raise ValueError('Uncommitted remote attempt changed')
            remote.append((base + '/' + path, etag))
    deadline = current()
    for path, metadata in pending:
        local(prefix, path.relative_to(storage._fs_path(prefix, '.')).as_posix())
        if journal._capture(path, deadline=deadline) != metadata:
            raise ValueError('Attempt changed during cleanup')
        if deadline is not None:
            deadline.check()
        path.unlink()
    for path, etag in remote:
        if deadline is not None:
            deadline.check()
        storage.s3().delete_object(**objects.arguments(prefix, path), IfMatch=etag)
    # Include declared empty directories, but do not remove arbitrary extra dirs.
    from .storage_maintenance import empty_local_directories
    for relative in (stage, base):
        empty_local_directories(prefix + '/' + relative, paths + ['inputs/.empty-placeholder', 'run/.empty-placeholder'])


def finish(prefix, path, raw, etag, plan, outcome):
    history = PREFIX + '/history/' + plan['attempt_id'] + '.json'
    receipt = json_text({'plan': plan, 'outcome': outcome}).encode('utf8')
    existing, _ = read(prefix, history)
    if existing is None:
        put(prefix, history, receipt)
    elif existing != receipt:
        raise ValueError('Attempt recovery receipt differs')
    current, version = read(prefix, path)
    if (current, version) != (raw, etag):
        raise ValueError('Attempt recovery plan changed')
    if storage._use_s3():
        storage.s3().delete_object(**objects.arguments(prefix, path), IfMatch=etag)
    else:
        local(prefix, path).unlink()


async def recover_locked(db, case, *, apply=True):
    from .attempt_plan_writes import recover as recover_writes
    results = await journal._io(recover_writes, case.s3_prefix, apply=apply)
    pending = await journal._io(storage.list_tree, case.s3_prefix, PREFIX + '/pending')
    results.extend(await recover_pending(db, case, [entry['path'] for entry in pending], apply=apply))
    results.extend(await recover_node_caches(db, case, apply=apply))
    return results


async def _sql_outcome(db, case, identifier, plan, *, rollback_path=None):
    current = db.info.get('attempt_seals', {}).get(identifier)
    if current is not None and current is db.get_transaction() and current.is_active:
        return 'active_transaction'
    event = await db.get(DurableEvent, event_id(identifier), populate_existing=True)
    attempt = await db.get(JobAttempt, identifier, populate_existing=True)
    if event is None:
        if attempt is not None:
            raise ValueError('Attempt exists without matching seal proof; retained')
        if rollback_path is not None:
            await _require_drained_writes(db, case, identifier, rollback_path)
        return 'uncommitted'
    if (plan['schema'] != SCHEMA or event.case_id != case.id or event.attempt_id != identifier
            or event.type != EVENT or json.loads(event.payload_json) != proof(plan) or attempt is None
            or attempt.job_id != plan['job_id']
            or json.loads(attempt.input_manifest_json).get('workspace') != plan['manifest']):
        raise ValueError('Sealed-input commit proof differs')
    return 'committed'


async def _require_drained_writes(db, case, identifier, path):
    """Keep a pending seal intact while a conditional PUT may still arrive."""
    from ..models import Case, Workspace
    from ..storage_models import OwnerStorageReservation, StorageWriteReservation

    pending = storage._s3_key(case.s3_prefix, path)
    members = storage._s3_key(case.s3_prefix, workspace.PREFIX + '/' + identifier + '/')
    manifest = members + 'manifest.json'
    requests = await db.scalars(select(StorageWriteReservation.request_json).where(
        StorageWriteReservation.case_id == case.id, StorageWriteReservation.state == 'active'))
    for request in requests:
        key = json.loads(request)['key']
        if key == pending or key.startswith(members):
            raise ValueError('A staged input PUT has no durable acknowledgement; retained')

    # A failed final metadata PUT may also be retained by the short owner
    # grant. Its recovered receipt identifies the outstanding key exactly.
    from .owner_storage_budget import exclusion
    owner_id = await db.scalar(select(Workspace.user_id).join(Case).where(Case.id == case.id))
    if owner_id is None:
        raise ValueError('Staged input storage owner is missing')
    grants = await db.execute(select(OwnerStorageReservation.id, OwnerStorageReservation.receipt_json).where(
        OwnerStorageReservation.user_id == owner_id, OwnerStorageReservation.state == 'active'))
    own_grant = exclusion(db, owner_id)
    for grant_id, receipt in grants:
        if grant_id == own_grant:
            continue
        evidence = json.loads(receipt)
        if not evidence or (evidence.get('pending') or {}).get('key') in (pending, manifest):
            raise ValueError('A seal metadata PUT has no durable acknowledgement; retained')


async def recover_pending(db, case, paths, *, apply=True, lease=None):
    from .case_provision import check_deadline
    results = []
    for path in paths:
        check_deadline()
        try:
            identifier = path.removeprefix(PREFIX + '/pending/').removesuffix('.json')
            if path != PREFIX + '/pending/' + identifier + '.json':
                raise ValueError('Unexpected preparation entry')
            raw, etag = await journal._io(read, case.s3_prefix, path)
            if raw is None:
                continue  # A concurrent full recovery already settled this selection.
            plan = json.loads(raw)
            await journal._io(validate, case, identifier, plan)
            rollback_path = path if apply and storage._use_s3() else None
            if lease is None:
                outcome = await _sql_outcome(db, case, identifier, plan, rollback_path=rollback_path)
            else:
                async with lease.control() as control:
                    outcome = await _sql_outcome(control, case, identifier, plan, rollback_path=rollback_path)
            if apply and outcome != 'active_transaction':
                if storage._use_s3():
                    from .s3_case_files import require_conditions
                    await journal._io(require_conditions, case.s3_prefix)
                check_deadline()
                if outcome == 'uncommitted':
                    await journal._io(rollback, plan)
                    outcome = 'rolled_back'
                check_deadline()
                await journal._io(finish, case.s3_prefix, path, raw, etag, plan, outcome)
            results.append({'attempt_id': identifier, 'status': outcome})
        except TimeoutError:
            raise
        except Exception as error:
            results.append({'record': path, 'status': 'recovery_required', 'reason': str(error)})
    return results


def local_attempts(prefix):
    from .storage_listing_budget import ListingBudget

    parent = local(prefix, workspace.PREFIX)
    budget = ListingBudget()
    names = set()
    try:
        entries = os.scandir(parent)
    except FileNotFoundError:
        return names
    with entries:
        for entry in entries:
            budget.visit()
            names.add(entry.name)
    return names


async def unowned_local_attempts(db, case, *, names=None):
    """Probe the SQL ownership of exactly the local, canonical attempt IDs."""
    candidates = set()
    if names is None:
        names = await journal._io(local_attempts, case.s3_prefix)
    for name in names:
        identifier = name.removeprefix('.preparing-')
        try:
            if str(uuid.UUID(identifier)) == identifier:
                candidates.add(identifier)
        except ValueError:
            continue

    ordered = sorted(candidates)
    for start in range(0, len(ordered), 64):
        owned = await db.scalars(select(JobAttempt.id).join(Job, Job.id == JobAttempt.job_id).where(
            Job.case_id == case.id, JobAttempt.id.in_(ordered[start:start + 64])))
        candidates.difference_update(owned)
    return candidates


async def recover_node_caches(db, case, *, apply=True, names=None):
    """Reconcile this node after a different API finished remote rollback.

    Caller holds the case lock for mutation. Committed caches remain execution
    state; a rollback receipt alone never overrides a surviving SQL attempt.
    """
    if not storage._use_s3():
        return []
    results = []
    from .case_provision import check_deadline
    for identifier in sorted(await unowned_local_attempts(db, case, names=names)):
        check_deadline()
        try:
            pending = PREFIX + '/pending/' + identifier + '.json'
            if (await journal._io(read, case.s3_prefix, pending))[0] is not None:
                continue  # The regular pending-plan recovery owns this case.
            history = PREFIX + '/history/' + identifier + '.json'
            raw, etag = await journal._io(read, case.s3_prefix, history)
            if raw is None:
                continue  # Legacy local caches have no deletion proof.
            receipt = json.loads(raw)
            plan = receipt['plan']
            await journal._io(validate, case, identifier, plan)
            if receipt['outcome'] == 'committed':
                continue
            if receipt['outcome'] != 'rolled_back':
                raise ValueError('Unknown attempt recovery outcome')
            if (await db.get(JobAttempt, identifier, populate_existing=True) is not None or
                    await db.get(DurableEvent, event_id(identifier), populate_existing=True) is not None):
                raise ValueError('Rollback cache receipt conflicts with SQL ownership')
            base = workspace.PREFIX + '/' + identifier
            if await journal._io(storage.list_tree, case.s3_prefix, base):
                raise ValueError('Rolled-back attempt has remote objects again')
            # Diagnose integrity even for read-only requests.
            expected = inventories(plan['manifest']); paths = directory_paths(plan['manifest'])
            for relative in (base, workspace.PREFIX + '/.preparing-' + identifier):
                await journal._io(inspect_local, case.s3_prefix, relative, expected, paths)
            if apply:
                if ((await journal._io(read, case.s3_prefix, history)) != (raw, etag) or
                        (await journal._io(read, case.s3_prefix, pending))[0] is not None):
                    raise ValueError('Attempt rollback receipt changed')
                check_deadline()
                await journal._io(rollback_local, plan)
            results.append({'attempt_id': identifier, 'status': 'node_cache_removed' if apply else 'node_cache_pending'})
        except TimeoutError:
            raise
        except Exception as error:
            results.append({'attempt_id': identifier, 'status': 'recovery_required', 'reason': str(error)})
    return results


async def untracked(db, case):
    """Old directories without a SQL owner or plan are evidence, never trash."""
    known = set(await db.scalars(select(JobAttempt.id).join(Job, Job.id == JobAttempt.job_id).where(Job.case_id == case.id)))
    pending = await journal._io(storage.list_tree, case.s3_prefix, PREFIX + '/pending')
    known.update(row['path'].rsplit('/', 1)[-1].removesuffix('.json') for row in pending)
    history = await journal._io(storage.list_tree, case.s3_prefix, PREFIX + '/history')
    known.update(row['path'].rsplit('/', 1)[-1].removesuffix('.json') for row in history)
    def directories():
        if storage._use_s3():
            return {row['path'][len(workspace.PREFIX)+1:].split('/')[0]
                    for row in storage.list_tree(case.s3_prefix, workspace.PREFIX)} | local_attempts(case.s3_prefix)
        parent = local(case.s3_prefix, workspace.PREFIX)
        return {path.name for path in parent.iterdir()} if parent.exists() else set()
    names = await journal._io(directories)
    return [{'path': workspace.PREFIX + '/' + name, 'status': 'untracked_retained'}
            for name in sorted(names) if name.removeprefix('.preparing-') not in known]
