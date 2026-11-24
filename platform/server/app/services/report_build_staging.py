"""Prepare public S3 report builds before opening the submission transaction."""
from .. import storage
from ..db import SessionLocal
from . import command_input_seal, document_admission, report_handler
from .attempt_workspace import inventory
from .case_provision import io
from .case_read_view import inspect, remote_file_versions
from .case_write import CaseWriteConflict, bounded_case_read
from .casedict import CASE_DICT_PATH, parse_casedict
from .postprocess_executor import config_from_case_dict
from .publication_inputs import resolve
from .publication_read_set import PublicationReadSet
from .step_handlers import get_handler


def configuration(prefix):
    def read(root):
        dictionary=inventory(root,[CASE_DICT_PATH])
        parsed=parse_casedict((root/CASE_DICT_PATH).read_text(encoding='utf8'))
        return config_from_case_dict(parsed)['run_dir'],dictionary

    if not storage.exists(prefix,'E-report/report.md'):
        from fastapi import HTTPException
        raise HTTPException(400,'E-report/report.md 不存在')
    return inspect(prefix,read,paths=[CASE_DICT_PATH])


async def prepare(case_id,user_id,prefix,revision):
    run_dir,dictionary=await bounded_case_read(configuration,prefix,code='report_preparation',label='报告输入准备')
    async with SessionLocal() as db:
        case,user=await document_admission.authorize(db,case_id,user_id)
        _require_case(case,prefix,revision)
        db.expunge_all()
    observations=PublicationReadSet()
    async with SessionLocal() as db:
        pending=await get_handler('report.build').prepare(db,case,user,
            parameters={'run_dir':run_dir,'mode':'markdown','allow_standalone':True},
            budget={'timeout_seconds':300,'max_log_bytes':16*1024*1024},observations=observations)
    if pending.admission['guard']['dictionary']!=dictionary:
        raise CaseWriteConflict('input_stale','Report dictionary changed while selecting inputs')
    return await document_admission.complete(pending,case_id,user_id),observations


def _require_case(case,prefix,revision):
    if case.s3_prefix!=prefix or case.dict_revision!=revision:
        raise CaseWriteConflict('input_stale','Report case revision changed during preparation',
                                current_revision=case.dict_revision)


def _require_versions(prefix,prepared):
    versions=prepared.admission['case_versions']
    if versions is None or remote_file_versions(prefix,versions)!=versions:
        raise CaseWriteConflict('input_stale','Report source files changed during preparation')
    report_handler.require_toolchain(prepared)


async def upload(lease,owner_id,case,revision,prepared,observations):
    prefix=prepared.spec['case_prefix']
    _require_case(case,prefix,revision)
    await io(_require_versions,prefix,prepared)
    publications=(await resolve(observations,case,prepared.input_publications)
                  if prepared.input_publications else [])
    observations.seal()

    async def verify(manifest):
        document_admission.validate_workspace(prepared,manifest)

    return await command_input_seal.upload(lease,owner_id,prepared,revision,verify,publications=publications)


async def verify_commit(db,case,prepared,revision,observations):
    _require_case(case,prepared.spec['case_prefix'],revision)
    await bounded_case_read(_require_versions,case.s3_prefix,prepared,code='report_seal',label='核验报告封存')
    await observations.require_current(db)
