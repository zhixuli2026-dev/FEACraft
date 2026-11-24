"""Freeze a draft and its verified publication references outside the SQL write transaction."""
from ..db import SessionLocal
from . import command_input_seal, document_admission
from .case_provision import io
from .case_write import CaseWriteConflict
from .publication_inputs import PublicationInputStale
from .publication_read_set import PublicationReadSet
from .report_handler import document_inputs
from .step_handlers import get_handler
from .workflow_state import ExecutionConflict


async def prepare(lease, owner_id, user_id, revision, handler, inputs, overwrite):
    case_id = lease.case.id
    async with SessionLocal() as db:
        case,user = await document_admission.authorize(db,case_id,user_id)
        if case.dict_revision!=revision:
            raise CaseWriteConflict('revision_conflict','caseDict 已被其他操作修改，请刷新后重试',
                                    current_revision=case.dict_revision)
        db.expunge_all()
    await io(inputs.verify_versions,case.s3_prefix)
    observations = PublicationReadSet()
    try:
        selected = await document_inputs(observations,case,inputs.run_dir)
    except PublicationInputStale as error:
        raise ExecutionConflict('input_stale',str(error)) from error
    async with SessionLocal() as db:
        step = await get_handler(handler).prepare(db,case,user,
            parameters={'run_dir':inputs.run_dir,'overwrite':overwrite},
            budget={'timeout_seconds':120,'max_log_bytes':4*1024*1024},
            draft_inputs=inputs,document_sources=selected)
    observations.seal()

    async def verify(manifest):
        await io(inputs.verify_manifest,manifest,step)

    workspace = await command_input_seal.upload(lease,owner_id,step,revision,verify,publications=selected[1])
    return step,workspace,observations


async def verify_commit(db, case, step, observations):
    if case.name!=step.descriptor['case_name']:
        raise ExecutionConflict('input_stale','算例名称在报告准备期间已变化，请重试')
    if observations is not None:
        try:
            await observations.require_current(db)
        except PublicationInputStale as error:
            raise ExecutionConflict('input_stale',str(error)) from error
