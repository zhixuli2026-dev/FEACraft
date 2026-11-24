"""Stage a local command using the existing filesystem seal."""
from contextlib import asynccontextmanager
from functools import partial

from ..db import SessionLocal
from . import case_copy_io, case_journal_recovery, document_admission, local_file_budget, local_rerun_seal
from .async_ops import finish_before_cancel
from .case_command_handler import CaseCommandHandler
from .case_read_capacity import file_read_deadline
from .case_write import CaseWriteConflict
from .owner_storage_budget import owner_for_case
from .request_deadline import STAGING_CLEANUP_SECONDS


SEAL_SECONDS = STAGING_CLEANUP_SECONDS


async def check_peak(original, user_id, size):
    async with SessionLocal() as db:
        case, _ = await document_admission.authorize(db, original.id, user_id)
        owner_id = (await owner_for_case(db, case)).id
        await finish_before_cancel(db.rollback())
        inventory = await local_file_budget.lock_for_prepared_edit(db, owner_id)
        case, _ = await document_admission.authorize(db, original.id, user_id)
        if (case.s3_prefix, case.dict_revision) != (original.s3_prefix, original.dict_revision):
            raise CaseWriteConflict('input_stale', '算例在封存准备期间发生变化，请重新提交')
        await local_file_budget.check_quota(db, case, size, inventory=inventory)
        await finish_before_cancel(db.rollback())


@asynccontextmanager
async def seal_request(case, user_id, code, label):
    async with file_read_deadline(code,label,seconds=SEAL_SECONDS):
        pending = case_journal_recovery.pending_local(case)
        if pending is not None:
            async def checked(session):
                current, _ = await document_admission.authorize(session, case.id, user_id)
                return current

            await case_journal_recovery.recover_local(checked, pending)
        with case_copy_io.admit(code=code+'_busy',label=label):
            yield


@asynccontextmanager
async def stage(case, user_id, job_type, spec, *, input_paths=None):
    handler=CaseCommandHandler(job_type) if job_type!='shell' else None
    if handler is not None:
        handler.validate_spec(spec)
    all_live=handler is None and input_paths is None
    async with seal_request(case,user_id,'command_seal','封存作业输入'):
        async with local_rerun_seal.prepare(case,spec,input_paths,None,None,all_live=all_live,
                command_handler=handler,check_peak=partial(check_peak,case,user_id)) as staged:
            yield staged


@asynccontextmanager
async def stage_batch(case,user_id,job_type,specs):
    handler=CaseCommandHandler(job_type) if job_type!='shell' else None
    if handler is not None:
        for spec in specs:
            handler.validate_spec(spec)
    async with seal_request(case,user_id,'batch_seal','封存批次输入'):
        async with local_rerun_seal.prepare_batch(case,specs,handler,check_peak=partial(check_peak,case,user_id)) as staged:
            yield staged
