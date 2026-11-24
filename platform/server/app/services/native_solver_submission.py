"""Prepare registered native solver jobs outside SQL write transactions."""
from functools import partial

from fastapi import HTTPException
from sqlalchemy import select

from .. import storage
from ..models import Job, JobAttempt
from . import attempt_objects, case_copy_io, case_journal_recovery, command_input_seal, document_admission
from . import job_delivery, job_execution, legacy_job_admission, local_rerun_seal, publication_inputs, request_lifecycle, step_handlers
from .async_ops import finish_before_cancel
from .case_provision import io
from .case_read_capacity import file_read_deadline
from .case_write import CaseWriteConflict
from .case_write_capacity import committing
from .local_source_reads import LocalSourceReads
from .publication_read_set import PublicationReadSet
from .quota import check_job_quota
from .request_deadline import STAGING_CLEANUP_SECONDS
from .workflow_state import ExecutionConflict


BUDGET = {'timeout_seconds':86400, 'max_log_bytes':16*1024*1024}


def submission_payload(backend, body):
    return {'endpoint': backend + '.solve', 'payload': body}


def _check_remote(prefix, expected):
    if attempt_objects.selection(prefix,[],True) != expected:
        raise CaseWriteConflict('input_stale','求解输入或输出目标在封存期间发生变化')


def _require_inputs(step, manifest):
    source = step.descriptor['source']['files']
    if source[step.descriptor['input']]['size'] > 1024*1024:
        raise HTTPException(413,'Solver input exceeds 1 MiB')
    for path, expected in source.items():
        sealed = manifest['files'].get(path)
        if sealed is None or any(sealed[key] != expected[key] for key in ('size','sha256')):
            raise CaseWriteConflict('input_stale','求解依赖与封存内容不一致',path=path)


def _parameters(prefix, raw_input, canonical_path, find_executable, label):
    path = canonical_path(raw_input)
    if not storage.exists(prefix,path):
        raise HTTPException(404,label+' input does not exist')
    if storage.file_size(prefix,path)>1024*1024:
        raise HTTPException(413,label+' input exceeds 1 MiB')
    try:
        executable = find_executable()
    except FileNotFoundError as error:
        raise HTTPException(503,str(error)) from error
    return {'input_path':path,'executable':executable}


async def _commit(db,case_id,user_id,key,digest,step,staged,observations,check_live):
    case,user = await document_admission.authorize(db,case_id,user_id)
    async with committing(db,case,defer_journal=True):
        await document_admission.check_access(db,case,user)
        replay = await job_execution.lookup_submission(db,case_id,user_id,key,digest)
        if replay is not None:
            return replay.id,False
        await check_job_quota(db,user,case_id=case_id)
        await observations.require_current(db)
        await check_live()
        job,_ = await job_execution.submit(db,case_id=case_id,user_id=user_id,key=key,digest=digest,
            job_type=step.job_type,command=step.spec['command'],spec=step.spec,input_paths=step.input_paths,
            input_publications=step.input_publications,
            generated_inputs=step.generated_inputs,staged_workspace=staged)
        attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
        await step_handlers.bind(db,case,job,attempt,step)
    return job.id,True


async def _stage(db,case_id,user_id,key,digest,backend,label,raw_input,canonical_path,find_executable,extra):
    code = backend+'_seal'
    caption = '封存 '+label+' 输入'
    async with file_read_deadline(code,caption,seconds=STAGING_CLEANUP_SECONDS):
        case,user = await document_admission.authorize(db,case_id,user_id)
        replay = await job_execution.lookup_submission(db,case_id,user_id,key,digest)
        if replay is not None:
            return replay.id,False
        await check_job_quota(db,user,case_id=case_id)
        db.expunge(case)
        await finish_before_cancel(db.rollback())
        handler = step_handlers.get_handler(backend+'.solve')
        observations = PublicationReadSet()

        async def prepare():
            parameters = await io(_parameters,case.s3_prefix,raw_input,canonical_path,find_executable,label)
            step = await handler.prepare(None,case,None,parameters={**parameters,**(extra or {})},budget=BUDGET,observations=observations)
            publications = (await publication_inputs.resolve(observations,case,step.input_publications)
                            if step.input_publications else None)
            observations.seal()
            return step,publications

        if storage._use_s3():
            async with command_input_seal.stage_submission(db,case,user_id,key,digest,code=code,label=caption) as (replay,lease,owner_id):
                if replay is not None:
                    return replay.id,False
                versions = await io(attempt_objects.selection,case.s3_prefix,[],True)
                check_live = partial(io,_check_remote,case.s3_prefix,versions)
                step,publications = await prepare()

                async def verify(manifest):
                    _require_inputs(step,manifest)
                    await check_live()

                staged = await command_input_seal.upload(lease,owner_id,step,case.dict_revision,verify,
                    publications=publications,generated_inputs=step.generated_inputs,recheck_live=True)
                return await _commit(db,case_id,user_id,key,digest,step,staged,observations,check_live)

        pending = case_journal_recovery.pending_local(case)
        if pending is not None:
            async def checked(session):
                current,_ = await document_admission.authorize(session,case_id,user_id)
                return current

            await case_journal_recovery.recover_local(checked,pending)
        sources = LocalSourceReads()
        with case_copy_io.admit(code=code+'_busy',label=caption), sources.collect():
            step,publications = await prepare()
            sources.seal()
            async with local_rerun_seal.prepare(case,step.spec,step.input_paths,publications,step.generated_inputs,all_live=False) as staged:
                _require_inputs(step,staged.draft)
                return await _commit(db,case_id,user_id,key,digest,step,staged,observations,sources.require_current)


async def solve(db,case_id,user_id,key,body,*,backend,label,raw_input,canonical_path,find_executable,request,extra=None):
    try:
        job_execution.validate_key(key)
        digest = job_execution.request_hash(submission_payload(backend, body))
        job_id,created = await request_lifecycle.wait(
            _stage(db,case_id,user_id,key,digest,backend,label,raw_input,canonical_path,find_executable,extra),request=request)
        if created:
            try:
                await job_delivery.deliver(job_id)
            except job_delivery.DeliveryDeferred:
                pass
        return await legacy_job_admission.response(db,await db.get(Job,job_id),backend=backend,case_id=case_id)
    except ExecutionConflict as error:
        raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except FileNotFoundError as error:
        raise HTTPException(404,label+' dependency does not exist: '+str(error)) from error
    except CaseWriteConflict:
        raise
    except ValueError as error:
        raise HTTPException(400,str(error)) from error
