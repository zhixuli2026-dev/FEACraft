"""Seal standalone CadQuery inputs before recording the job and publication."""
import hashlib
from types import SimpleNamespace

from sqlalchemy import select

from .. import storage
from ..models import JobAttempt
from . import case_copy_io, case_journal_recovery, command_input_seal, document_admission
from . import geometry_publication, job_execution, local_rerun_seal, step_handlers
from .async_ops import finish_before_cancel
from .case_provision import io
from .case_read_capacity import file_read_deadline
from .case_write import CaseWriteConflict
from .case_write_capacity import committing
from .casedict import CASE_DICT_PATH
from .file_read import FileRead, FileReadConflict
from .geometry_handler import INPUT_PATHS
from .local_source_reads import LocalSourceReads
from .publication_read_set import PublicationReadSet
from .quota import check_job_quota
from .request_deadline import STAGING_CLEANUP_SECONDS


CODE = 'geometry_seal'
LABEL = '封存几何输入'
SCRIPT = 'A-geo/geometry.py'
BUDGET = {'timeout_seconds':86400, 'max_log_bytes':16*1024*1024}


def prepared_step(handler, command, prefix, manifest, generated, saved_source_sha256):
    records = manifest['files']
    script = records.get(SCRIPT)
    if saved_source_sha256 is not None and (script is None or script['sha256'] != saved_source_sha256):
        raise CaseWriteConflict('file_conflict', '保存的几何脚本已有更新', path=SCRIPT)
    if script is None:
        raise ValueError('A-geo/geometry.py 不存在')
    expected = records[CASE_DICT_PATH]
    try:
        reader = FileRead(prefix, CASE_DICT_PATH)
        if reader.size != expected['size']:
            raise FileReadConflict('Dictionary size changed')
        raw = b''.join(reader.chunks())
        if hashlib.sha256(raw).hexdigest() != expected['sha256']:
            raise FileReadConflict('Dictionary bytes differ from their seal')
    except (FileNotFoundError, FileReadConflict) as error:
        raise CaseWriteConflict('input_stale', '几何封存期间 caseDict 已变化', path=CASE_DICT_PATH) from error
    text = raw.decode('utf8').replace('\r\n', '\n').replace('\r', '\n')
    return handler.prepared(command, geometry_publication.describe_source(text, records, generated), generated)


async def submit(db, case_id, user_id, mode, key, digest, parameters, saved_source_sha256):
    async with file_read_deadline(CODE, LABEL, seconds=STAGING_CLEANUP_SECONDS):
        case, user = await document_admission.authorize(db, case_id, user_id)
        replay = await job_execution.lookup_submission(db, case_id, user_id, key, digest)
        if replay is not None:
            return replay, None
        await check_job_quota(db, user, case_id=case.id)
        db.expunge(case)
        await finish_before_cancel(db.rollback())

        handler = step_handlers.get_handler('geometry.'+mode)
        command = await io(handler.command, case.s3_prefix, parameters, BUDGET)
        observations = PublicationReadSet()
        sources = LocalSourceReads()
        step = SimpleNamespace(spec=command[0], input_paths=list(INPUT_PATHS))

        if storage._use_s3():
            async with command_input_seal.stage_submission(db,case,user_id,key,digest,code=CODE,label=LABEL) as (replay,lease,owner_id):
                if replay is not None:
                    return replay, None
                generated = await handler.generated_paths(observations, case)
                observations.seal()
                prepared = None

                async def verify(manifest):
                    nonlocal prepared
                    prepared = await io(prepared_step,handler,command,case.s3_prefix,manifest,generated,saved_source_sha256)

                staged = await command_input_seal.upload(lease,owner_id,step,case.dict_revision,verify,recheck_live=True)
                return await _commit(db,case_id,user_id,key,digest,mode,prepared,staged,observations,None)

        pending = case_journal_recovery.pending_local(case)
        if pending is not None:
            async def checked(session):
                current, _ = await document_admission.authorize(session, case_id, user_id)
                return current

            await case_journal_recovery.recover_local(checked, pending)
        with case_copy_io.admit(code=CODE+'_busy', label=LABEL), sources.collect():
            generated = await handler.generated_paths(observations, case)
            observations.seal()
            sources.seal()
            async with local_rerun_seal.prepare(case,step.spec,step.input_paths,None,None,all_live=False) as staged:
                prepared = await io(prepared_step,handler,command,case.s3_prefix,staged.draft,generated,saved_source_sha256)
                return await _commit(db,case_id,user_id,key,digest,mode,prepared,staged,observations,sources)


async def _commit(db, case_id, user_id, key, digest, mode, prepared, staged, observations, sources):
    case, user = await document_admission.authorize(db, case_id, user_id)
    async with committing(db,case,wait_for_quota=mode=='preview',defer_journal=True):
        await document_admission.check_access(db, case, user)
        replay = await job_execution.lookup_submission(db, case.id, user.id, key, digest)
        if replay is not None:
            return replay, None
        await check_job_quota(db,user,case_id=case.id)
        await observations.require_current(db)
        if sources is not None:
            await sources.require_current()
        job, _ = await job_execution.submit(db,case_id=case.id,user_id=user.id,key=key,digest=digest,
            job_type=prepared.job_type,command=prepared.spec['command'],spec=prepared.spec,
            input_paths=prepared.input_paths,staged_workspace=staged)
        attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
        publication = await step_handlers.bind(db,case,job,attempt,prepared)
    return job, publication
