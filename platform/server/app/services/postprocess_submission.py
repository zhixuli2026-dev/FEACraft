"""Prepare file-backed and registered postprocessing before admission writes."""
from functools import partial

from sqlalchemy import select

from .. import storage
from ..models import JobAttempt
from . import job_execution, publication_command_submission, step_handlers
from .async_ops import finish_before_cancel
from .case_evidence import safe_run_dir
from .case_provision import io
from .case_read_capacity import file_read_deadline
from .case_write import CaseWriteConflict
from .case_write_capacity import committing
from .casedict import CASE_DICT_PATH, parse_casedict
from .document_admission import authorize, check_access
from .local_source_reads import observe
from .postprocess_executor import config_from_case_dict
from .quota import check_job_quota
from .request_deadline import STAGING_CLEANUP_SECONDS
from .request_lifecycle import wait


CODE='postprocess_seal'
LABEL='封存后处理输入'
BUDGET={'timeout_seconds':86400,'max_log_bytes':16*1024*1024}


def _stale():
    return CaseWriteConflict('input_stale','算例输入在封存期间发生变化')


def _run_directory(prefix, requested):
    with observe(storage._fs_path(prefix,'.'),(CASE_DICT_PATH,)):
        text=storage.get_bytes(prefix,CASE_DICT_PATH).decode('utf8')
    return safe_run_dir(config_from_case_dict(parse_casedict(text),requested)['run_dir'])


async def _prepare(db, case, actor, requested, registered, expected_run, **options):
    run_dir=await io(_run_directory,case.s3_prefix,requested)
    if registered and run_dir!=expected_run:
        raise _stale()
    handler=step_handlers.get_handler('postprocess.run' if registered else 'postprocess.case')
    return await handler.prepare(db,case,actor,parameters={'run_dir':run_dir},budget=BUDGET,**options)


async def submit(db, case, user, key, payload, *, registered, request):
    case_id,user_id=case.id,user.id
    requested=payload['run_dir']
    db.expunge(case)
    await finish_before_cancel(db.rollback())

    async def persist():
        async with file_read_deadline(CODE,LABEL,seconds=STAGING_CLEANUP_SECONDS):
            expected_run=await io(_run_directory,case.s3_prefix,requested) if registered else None
            identity=({'handler':'postprocess.v1','run_dir':expected_run} if registered
                      else {'endpoint':'postprocess.run','payload':payload})
            digest=job_execution.request_hash(identity)
            replay=await job_execution.lookup_submission(db,case_id,user_id,key,digest)
            if replay is not None:
                return replay,None
            await finish_before_cancel(db.rollback())
            prepare=partial(_prepare,requested=requested,registered=registered,expected_run=expected_run)
            async with publication_command_submission.stage_step(db,case,user_id,prepare,registered=registered,
                    code=CODE,label=LABEL,key=key,digest=digest) as staged:
                selected,actor=await authorize(db,case_id,user_id)
                async with committing(db,selected):
                    replay=await job_execution.lookup_submission(db,case_id,user_id,key,digest)
                    if replay is not None:
                        return replay,None
                    workspace=None
                    if staged is None:
                        prepared=await prepare(db,selected,actor)
                    else:
                        await check_access(db,selected,actor)
                        await check_job_quota(db,actor,case_id=case_id)
                        prepared,workspace=staged
                    job,created=await job_execution.submit(db,case_id=case_id,user_id=user_id,key=key,digest=digest,
                        job_type=prepared.job_type,command=prepared.spec['command'],spec=prepared.spec,
                        input_paths=prepared.input_paths,input_publications=prepared.input_publications,
                        staged_workspace=workspace)
                    if not created:
                        return job,None
                    attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
                    publication=await step_handlers.bind(db,selected,job,attempt,prepared)
                    return job,publication

    return await wait(persist(),request=request)
