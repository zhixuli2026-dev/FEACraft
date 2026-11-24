"""Stage whole-case registered operations before committing their jobs."""
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial

from sqlalchemy import select

from .. import storage
from ..db import SessionLocal
from ..models import Case, JobAttempt
from . import job_execution, legacy_job_admission, step_handlers
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .document_admission import authorize, check_access


def _stale():
    return CaseWriteConflict('input_stale','工程作业的算例来源在封存期间发生变化')


def _verify_workspace(step, manifest):
    from .tree_metadata_native import PATH as mode_path

    source=step.descriptor['source']
    private={mode_path, *(step.generated_inputs or {})}
    files={path:record for path,record in manifest['files'].items() if path not in private}
    if files!=source['files'] or set(source['directories'])-set(manifest['directories']):
        raise _stale()


async def _prepare_steps(case, user_id, preparers):
    async with SessionLocal() as db:
        selected,user=await authorize(db,case.id,user_id)
        if (selected.s3_prefix,selected.dict_revision)!=(case.s3_prefix,case.dict_revision):
            raise _stale()
        db.expunge(user)
    steps=[]
    for prepare in preparers:
        # Whole-case registered handlers inspect files before reading job
        # quota. Close each member's session before reading the next member.
        async with SessionLocal() as db:
            step=await prepare(db,case,user)
        if step.input_publications is not None or step.admission is not None:
            raise ValueError('Whole-case registered operation requires unsupported inputs')
        steps.append(step)
    return steps


@dataclass(frozen=True)
class PreparedOperations:
    steps: list[step_handlers.PreparedStep]
    workspaces: list
    require_current: Callable[[], Awaitable[None]]


@asynccontextmanager
async def stage_local(case, user_id, preparers, *, code, label):
    from . import local_rerun_seal, typed_command_submission
    from .local_source_reads import LocalSourceReads

    reads=LocalSourceReads()
    async with typed_command_submission.seal_request(case,user_id,code,label):
        with reads.collect():
            steps=await _prepare_steps(case,user_id,preparers)
        reads.seal()
        async with local_rerun_seal.prepare_operations(case,steps,verify=_verify_workspace,
                check_peak=partial(typed_command_submission.check_peak,case,user_id)) as workspaces:
            yield PreparedOperations(steps,workspaces,reads.require_current)


@asynccontextmanager
async def stage_remote(db, case, user_id, key, digest, preparers, *, code, label):
    from . import command_input_seal
    from .case_provision import io
    from .case_read_capacity import file_read_deadline
    from .case_read_view import remote_live_version
    from .request_deadline import STAGING_CLEANUP_SECONDS

    async with file_read_deadline(code,label,seconds=STAGING_CLEANUP_SECONDS):
        async with command_input_seal.stage_submission(db,case,user_id,key,digest,code=code,label=label,
                probe_first=True,allow_legacy=True) as (replay,lease,owner):
            if replay is not None or lease is None:
                yield None
                return
            async with lease.control() as session:
                selected=await session.get(Case,case.id)
                session.expunge(selected)
            version=await io(remote_live_version,selected.s3_prefix)

            async def require_current():
                if await io(remote_live_version,selected.s3_prefix)!=version:
                    raise _stale()

            steps=await _prepare_steps(selected,user_id,preparers)
            await require_current()
            workspaces=await command_input_seal.upload_operations(lease,owner,steps,selected.dict_revision,_verify_workspace)
            yield PreparedOperations(steps,workspaces,require_current)


async def submit(db, case, user, key, endpoint, payload, prepare, *, request):
    """Preserve the existing request hash, publication binding and job receipt."""
    from .quota import check_job_quota
    from .request_lifecycle import wait

    job_execution.validate_key(key)
    digest=job_execution.request_hash({'endpoint':endpoint,'payload':payload})
    case_id,user_id=case.id,user.id
    replay=await job_execution.lookup_submission(db,case_id,user_id,key,digest)
    if replay is not None:
        return replay,False
    db.expunge(case)
    await finish_before_cancel(db.rollback())
    admission={'code':'registered_seal','label':'封存工程作业输入'}

    async def persist():
        staging=(stage_remote(db,case,user_id,key,digest,(prepare,),**admission) if storage._use_s3()
                 else stage_local(case,user_id,(prepare,),**admission))
        async with staging as staged:
            selected,actor=await authorize(db,case_id,user_id)
            async with legacy_job_admission.prepare(db,selected,actor,key,endpoint,payload) as admitted:
                if admitted.replay is not None:
                    return admitted.replay,False
                workspace=None
                if staged is None:
                    step=await prepare(db,selected,actor)
                else:
                    await check_access(db,selected,actor)
                    await check_job_quota(db,actor,case_id=case_id)
                    step,workspace=staged.steps[0],staged.workspaces[0]
                job,created=await job_execution.submit(db,case_id=case_id,user_id=user_id,key=key,digest=admitted.digest,
                    job_type=step.job_type,command=step.spec['command'],spec=step.spec,input_paths=step.input_paths,
                    generated_inputs=step.generated_inputs,staged_workspace=workspace)
                if created:
                    attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
                    await step_handlers.bind(db,selected,job,attempt,step)
                    if staged is not None:
                        await staged.require_current()
                return job,created

    return await wait(persist(),request=request)
