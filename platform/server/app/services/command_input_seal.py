"""Stage S3 inputs under a case lease, then bind them to a Job."""
import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
import json
import uuid

from fastapi import HTTPException

from .. import storage
from ..db import SessionLocal
from ..models import Case
from . import attempt_modes, attempt_objects as objects, attempt_seal_journal as seals, attempt_workspace
from . import case_copy_io, owner_storage_budget, storage_operations
from .async_ops import finish_before_cancel
from .case_command_handler import PreparedRemoteCommand
from .case_provision import check_deadline, io
from .case_read_capacity import file_read_deadline
from .case_write import CaseWriteConflict
from .document_admission import authorize
from .durable_events import json_text
from .quota import check_storage_quota, prepare_storage_inventory
from .s3_case_files import UnsupportedConditions, require_conditions
from .s3_file_transactions import _conditional
from .s3_storage_recovery import recover_reserved
from .seal_storage_budget import estimate
from .storage_admission import InventoryOutdated, quota_revision


@dataclass(frozen=True)
class ArchivedInputs:
    manifest: dict
    versions: dict[str, tuple[int, str]]
    case_revision: int


@dataclass(frozen=True)
class StagedInputs:
    id: str
    owner_id: int
    command: PreparedRemoteCommand | None
    draft: dict
    versions: dict[str, tuple[int, str]]
    mode_document: bytes | None
    archive: ArchivedInputs | None = None
    recheck_live: bool = False
    source_guard: Callable | None = None

    @property
    def retry_source(self):
        return self.archive.manifest if self.archive is not None else None

    async def bind_retry(self,db,case,attempt):
        await bind_prepared(db,case,attempt,self)

    def require_submission(self,case,spec,paths,publications,generated,*,all_live):
        if self.command is not None or self.archive is not None:
            raise ValueError('Prepared workspace differs from its input request')
        attempt_workspace.require_staged_submission(self.draft['manifest'],case,spec,paths,publications,generated,
                                                    all_live=all_live)

    async def bind_submission(self,db,case,attempt):
        return await bind_prepared(db,case,attempt,self)

    @property
    def pending(self):
        return seals.PREFIX + '/pending/' + self.id + '.json'

    def _publish_manifest(self, case, attempt):
        prefix = case.s3_prefix
        manifest = self.draft['manifest']
        if attempt.id!=self.id or self.draft['binding']!=objects.binding(prefix):
            raise CaseWriteConflict('input_stale','Staged inputs no longer match their case or attempt')
        if self.source_guard is not None:
            self.source_guard(case,attempt)
        elif self.archive is None:
            if (manifest['source']['dict_revision']!=case.dict_revision
                    or attempt_modes.read_remote(prefix)!=self.mode_document):
                raise CaseWriteConflict('input_stale','Case revision or modes changed during staging')
            if self.recheck_live:
                all_live=manifest['source']['selection_mode']=='case'
                paths=[] if all_live else manifest['selection']
                observed=objects.selection(prefix,paths,all_live,
                    known_directories=attempt_modes.directories(self.mode_document,paths,all_live))
                if observed!=manifest['source']['object_versions']:
                    raise CaseWriteConflict('input_stale','Current rerun inputs changed before commit')
        elif (attempt.retry_of_attempt_id!=self.archive.manifest['attempt_id']
                or case.dict_revision!=self.archive.case_revision
                or objects.sealed_input_versions(prefix,self.archive.manifest)!=self.archive.versions):
            raise CaseWriteConflict('input_stale','Retry source versions changed during staging')
        base = manifest['inputs_path'].removesuffix('/inputs')
        recorded = {'inputs/' + path: version for path, version in self.versions.items()}
        observed = {row['path']: (row['size'], row['etag']) for row in storage._s3_list(prefix, base)}
        if observed != recorded:
            raise CaseWriteConflict('input_stale', 'Staged command input versions changed before commit')
        raw, etag = seals.read(prefix, self.pending)
        if raw != json_text(self.draft).encode('utf8') or not etag:
            raise CaseWriteConflict('file_recovery_required', 'Staged command recovery record changed')
        manifest = {**manifest, 'source': {**manifest['source'], 'job_id': attempt.job_id}}
        plan = seals.describe_plan(case, attempt, manifest)
        seals.validate(case, self.id, plan)
        encoded = json_text(manifest).encode('utf8')
        plan_bytes = json_text(plan).encode('utf8')
        if len(encoded) > 16 * 1024 * 1024 or len(plan_bytes) > seals.MAX_RECORD:
            raise ValueError('Command seal metadata exceeds its record limit')
        # Promote the durable rollback inventory first. Either form is safe
        # after a crash, including when the conditional PUT reply was lost.
        _conditional(prefix, self.pending, plan_bytes, etag, {})
        promoted, _ = seals.read(prefix, self.pending)
        if promoted != plan_bytes:
            raise CaseWriteConflict('file_recovery_required', 'Promoted command recovery record changed')
        objects.put_new(prefix, base + '/manifest.json', encoded)
        return plan


async def bind_prepared(db, case, attempt, staged):
    lease = storage_operations.current()
    if lease is None or lease.case.id != case.id:
        raise CaseWriteConflict('file_sync_busy', 'Staged command inputs no longer hold the case lease')
    await lease.require_locked(db)
    owner = await owner_storage_budget.owner_for_case(db, case)
    if owner.id != staged.owner_id:
        raise CaseWriteConflict('input_stale', 'Case storage owner changed during command staging')
    plan = await io(staged._publish_manifest, case, attempt)
    manifest = plan['manifest']
    current = json.loads(attempt.input_manifest_json)
    attempt.input_manifest_json = json_text({**current, 'workspace': manifest})
    await db.flush()
    await seals.witness(db, case, attempt, plan)
    return manifest


async def _check_quota(lease, owner_id, size):
    while True:
        check_deadline()
        async with SessionLocal() as db:
            revision = await quota_revision(db, owner_id)
        measured = await prepare_storage_inventory(owner_id, revision)
        try:
            async with lease.control() as db:
                case = await db.get(Case, lease.case.id)
                owner = await owner_storage_budget.owner_for_case(db, case)
                await check_storage_quota(db, owner, size, inventory=measured)
            return
        except InventoryOutdated:
            await asyncio.sleep(.05)


def _estimate(case, identity, draft):
    provisional = SimpleNamespace(id=identity.id, job_id=2**63-1)
    manifest = {**draft, 'source': {**draft['source'], 'job_id': provisional.job_id}}
    draft_bytes = len(json_text(seals.describe_plan(case, identity, draft)).encode('utf8'))
    return estimate(case, provisional, None, manifest=manifest).user_bytes + draft_bytes


@asynccontextmanager
async def stage_case(case_id, user_id, *, code, label, validate_locked=None):
    from .request_deadline import STAGING_CLEANUP_SECONDS

    async with SessionLocal() as db:
        case, _ = await authorize(db, case_id, user_id)
        owner_id = (await owner_storage_budget.owner_for_case(db, case)).id

    async def checked(db):
        case, _ = await authorize(db, case_id, user_id)
        if (await owner_storage_budget.owner_for_case(db, case)).id != owner_id:
            raise CaseWriteConflict('input_stale', 'Case storage owner changed during input staging')
        return case

    async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
        with case_copy_io.admit(code=code+'_busy',label=label):
            await owner_storage_budget.recover_owner(owner_id, wait=True, require_available=True)
            async with storage_operations.hold(checked,purpose='staging',validate_locked=validate_locked) as lease:
                await recover_reserved(lease)
                prefix = lease.case.s3_prefix
                await io(require_conditions, prefix)
                pending = await io(storage.list_tree, prefix, seals.PREFIX + '/pending')
                recovered = await seals.recover_pending(None, lease.case, [entry['path'] for entry in pending], lease=lease)
                if any(row['status'] == 'recovery_required' for row in recovered):
                    raise CaseWriteConflict('file_recovery_required', 'Previous attempt seal requires recovery')
                yield lease, owner_id


@asynccontextmanager
async def stage_submission(db, case, user_id, key, digest, *, code, label, probe_first=False, allow_legacy=False,
                           wait_for_capacity=False, validate_locked=None):
    from .job_execution import lookup_submission

    inspected = not probe_first
    can_wait = wait_for_capacity or key is not None
    async with AsyncExitStack() as scopes:
        while True:
            replay = await lookup_submission(db, case.id, user_id, key, digest)
            if replay is not None:
                db.expunge(replay)
            await finish_before_cancel(db.rollback())
            if replay is not None:
                yield replay, None, None
                return
            try:
                if not inspected:
                    # An unsupported backend must never leave a new recovery
                    # lease that prevents an existing legacy shell submission.
                    with case_copy_io.admit(code=code+'_busy',label=label):
                        async with SessionLocal() as probe_db:
                            selected, _ = await authorize(probe_db,case.id,user_id)
                            if selected.s3_prefix != case.s3_prefix:
                                raise CaseWriteConflict('input_stale','封存期间算例的存储来源已变化')
                            async with owner_storage_budget.hold_case(probe_db,selected):
                                await io(require_conditions,selected.s3_prefix)
                                # Settle this probe's credit before the seal
                                # acquires credit from the same owner.
                                await finish_before_cancel(probe_db.rollback())
                    inspected = True
                lease, owner_id = await scopes.enter_async_context(stage_case(case.id,user_id,code=code,label=label,
                    validate_locked=validate_locked))
                break
            except UnsupportedConditions:
                if not allow_legacy:
                    raise
                yield None, None, None
                return
            except CaseWriteConflict as error:
                if not can_wait or error.code != 'file_sync_busy':
                    raise
            except HTTPException as error:
                if (not can_wait or error.status_code != 409 or not isinstance(error.detail, dict)
                        or error.detail.get('code') != 'storage_quota_busy'):
                    raise
            await asyncio.sleep(.1)
        if lease.case.s3_prefix != case.s3_prefix:
            raise CaseWriteConflict('input_stale', '封存期间算例的存储来源已变化')
        # A same-key competitor can commit between the lookup and acquisition.
        replay = await lookup_submission(db, case.id, user_id, key, digest)
        if replay is not None:
            db.expunge(replay)
        await finish_before_cancel(db.rollback())
        yield replay, lease, owner_id


def _input_snapshot(case, step, revision, *, publications=None, all_live=False, generated_inputs=None):
    identifier = str(uuid.uuid4())
    identity = SimpleNamespace(id=identifier, job_id=None)
    snapshot = objects.Snapshot(case.s3_prefix,identifier,step.input_paths,publications=publications,
        all_live=all_live,generated_inputs=generated_inputs,
        provenance={'case_id':case.id,'job_id':None,'dict_revision':revision,
                    'retry_of_attempt_id':None,'selection_mode':'case' if all_live else 'explicit','runtime':step.spec['runtime']})
    return identity, snapshot


async def upload(lease, owner_id, step, revision, verify, *, publications=None, command=None,
                 all_live=False, generated_inputs=None, recheck_live=False):
    identity, snapshot = _input_snapshot(lease.case,step,revision,publications=publications,
                                        all_live=all_live,generated_inputs=generated_inputs)
    try:
        await io(snapshot.inspect)
        draft = await io(snapshot.local.describe)
        admitted = await io(_estimate,lease.case,identity,draft)
        await _check_quota(lease,owner_id,admitted)
        return await _upload_preflight(lease,owner_id,identity,snapshot,admitted,verify,
                                       command=command,recheck_live=recheck_live)
    finally:
        await io(snapshot.cleanup)


async def upload_operations(lease, owner_id, steps, revision, verify):
    """Admit the combined S3 peak before materializing attempt workspaces."""
    case=lease.case
    pending=[]
    try:
        for step in steps:
            identity,snapshot=_input_snapshot(case,step,revision,generated_inputs=step.generated_inputs)
            await io(snapshot.inspect)
            admitted=await io(_estimate,case,identity,snapshot.local.describe())
            pending.append((step,identity,snapshot,admitted))
        await _check_quota(lease,owner_id,sum(item[3] for item in pending))
        staged=[]
        for step,identity,snapshot,admitted in pending:
            async def checked(manifest):
                await io(verify,step,manifest)

            staged.append(await _upload_preflight(lease,owner_id,identity,snapshot,admitted,checked,recheck_live=True))
            await io(snapshot.cleanup)
        return staged
    finally:
        for _,_,snapshot,_ in pending:
            await io(snapshot.cleanup)


async def _upload_preflight(lease, owner_id, identity, snapshot, admitted, verify, *, command=None, recheck_live=False):
    from .tree_metadata_native import PATH as mode_path

    prefix = lease.case.s3_prefix
    await io(snapshot.materialize)
    manifest = snapshot.local.describe()
    if await io(_estimate,lease.case,identity,manifest)>admitted:
        raise ValueError('Materialized seal exceeds its preflight storage estimate')
    if verify is not None:
        await verify(manifest)
    draft = await io(seals.prepare,lease.case,identity,snapshot)
    contents = dict(snapshot.local.generated_inputs)
    if snapshot.local.mode_input is not None:
        contents[mode_path]=snapshot.local.mode_input
    extra = (snapshot.local.sources, contents) if snapshot.local.sources or contents else ()
    versions = await io(objects.put_input_members,prefix,manifest['inputs_path'],manifest['files'],
                        snapshot.local.source,*extra)
    await io(snapshot.require_source)
    return StagedInputs(identity.id,owner_id,command,draft,versions,snapshot.mode_document,recheck_live=recheck_live)


async def upload_command_batch(lease, owner_id, job_type, specs, *, input_paths=None):
    from .case_command_handler import CaseCommandHandler
    from .s3_case_files import observe_source

    case = lease.case
    shell = job_type == 'shell'
    if not shell:
        handler = CaseCommandHandler(job_type)
        for spec in specs:
            handler.validate_spec(spec)
    async with lease.control() as db:
        revision = (await db.get(Case,case.id)).dict_revision
    all_live = shell and input_paths is None
    if shell:
        paths = input_paths or []
    else:
        source = await io(observe_source,case.s3_prefix)
        paths = sorted({path.split('/')[0] for path in source.source['files']})
    staged = []
    admitted = None
    selection = None
    for spec in specs:
        command = None if shell else PreparedRemoteCommand(handler.prepared(case.id,spec,source.source,paths),source)
        identity = SimpleNamespace(id=str(uuid.uuid4()),job_id=None)
        snapshot = objects.Snapshot(case.s3_prefix,identity.id,paths,all_live=all_live,
            provenance={'case_id':case.id,'job_id':None,'dict_revision':revision,
                        'retry_of_attempt_id':None,'selection_mode':'case' if all_live else 'explicit','runtime':spec['runtime']})
        try:
            await io(snapshot.inspect)
            estimate_bytes = await io(_estimate,case,identity,snapshot.local.describe())
            if shell:
                observed = snapshot.versions, snapshot.mode_document
                if selection is None:
                    selection = observed
                elif observed != selection:
                    raise CaseWriteConflict('input_stale','Batch input versions changed during staging')
            else:
                await command.verify(case,job_type,spec)
            if admitted is None:
                # This command batch has one input selection, runtime and revision.
                admitted = estimate_bytes
                await _check_quota(lease,owner_id,admitted*len(specs))
            elif estimate_bytes>admitted:
                raise CaseWriteConflict('input_stale','Batch input storage changed during staging')

            verify = partial(command.verify_seal,case) if command is not None else None
            item = await _upload_preflight(lease,owner_id,identity,snapshot,admitted,verify,
                                           command=command,recheck_live=shell)
            staged.append(item)
        finally:
            await io(snapshot.cleanup)
    return staged


def retry_manifest(case,previous,identifier):
    from .attempt_toolchain import host

    relative=f'{attempt_workspace.PREFIX}/{identifier}'
    source=previous['source']
    return {**previous,'attempt_id':identifier,'inputs_path':relative+'/inputs','run_path':relative+'/run',
        'source':{'case_id':case.id,'job_id':None,'dict_revision':source['dict_revision'],
                  'retry_of_attempt_id':previous['attempt_id'],'runtime':source['runtime'],
                  'selection_mode':source.get('selection_mode','explicit')},'toolchain':host(source['runtime'])}


async def upload_retry(lease,owner_id,case_revision,previous):
    case=lease.case
    identity=SimpleNamespace(id=str(uuid.uuid4()),job_id=None)
    original_versions=await io(objects.sealed_input_versions,case.s3_prefix,previous)
    manifest=await io(retry_manifest,case,previous,identity.id)
    await io(seals.validate,case,identity.id,seals.describe_plan(case,identity,manifest))
    await _check_quota(lease,owner_id,await io(_estimate,case,identity,manifest))
    draft=await io(seals.prepare_manifest,case,identity,manifest)
    write=partial(objects.put_input_members,archive=(previous['inputs_path'],original_versions))
    versions=await io(write,case.s3_prefix,manifest['inputs_path'],manifest['files'],None)
    archive=ArchivedInputs(previous,original_versions,case_revision)
    return StagedInputs(identity.id,owner_id,None,draft,versions,None,archive)


async def upload_checkpoint(lease,owner_id,snapshot,source_guard):
    from .tree_metadata_native import PATH as mode_path

    case=lease.case
    manifest=snapshot.describe()
    identity=SimpleNamespace(id=manifest['attempt_id'],job_id=None)
    await io(seals.validate,case,identity.id,seals.describe_plan(case,identity,manifest))
    await _check_quota(lease,owner_id,await io(_estimate,case,identity,manifest))
    draft=await io(seals.prepare_manifest,case,identity,manifest)
    contents=dict(snapshot.generated_inputs)
    if snapshot.mode_input is not None:
        contents[mode_path]=snapshot.mode_input
    versions=await io(objects.put_input_members,case.s3_prefix,manifest['inputs_path'],manifest['files'],
                      snapshot.source,snapshot.sources,contents)
    return StagedInputs(identity.id,owner_id,None,draft,versions,None,source_guard=source_guard)


@asynccontextmanager
async def stage(command, case_id, user_id):
    async with stage_case(case_id,user_id,code='command_seal',label='封存命令输入') as (lease,owner_id):
        spec = command.step.spec
        await command.verify(lease.case,command.step.job_type,spec)
        async with lease.control() as db:
            revision = (await db.get(Case,case_id)).dict_revision

        async def verify(manifest):
            await command.verify_seal(lease.case,manifest)

        yield await upload(lease,owner_id,command.step,revision,verify,command=command)
