"""Static professional handlers: preparation, proof, cancellation and recovery."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import re
from typing import Protocol

from .workflow_state import ExecutionConflict

CONTRACT = 'vibeflow.step-handler.v1'


@dataclass(frozen=True)
class PreparedStep:
    key: str
    version: str
    job_type: str
    spec: dict
    input_paths: list
    input_publications: list | None
    publication_kind: str
    descriptor: dict
    admission: dict | None = None
    generated_inputs: dict[str,bytes] | None = None

    def record(self):
        if self.admission is not None and not self.admission.get('ready'):
            raise ExecutionConflict('evidence_invalid','Native admission preparation is incomplete')
        from .job_execution import request_hash
        from .attempt_toolchain import declared
        handler = get_handler(self.key, self.version)
        record = {'schema':CONTRACT, 'key':self.key, 'version':self.version,
            'execution_sha256':request_hash(self.spec), 'publication_kind':self.publication_kind,
            'cancellation':handler.cancellation, 'recovery':handler.recovery,
            'toolchain':declared(self.key,self.version,self.spec,self.descriptor)}
        from .handler_environment import declare
        environment=declare(self.key,self.spec,self.descriptor)
        if environment is not None:record['environment']=environment
        return record


class StepHandler(Protocol):
    key: str
    version: str
    job_type: str
    publication_kind: str
    cancellation: dict
    recovery: dict

    async def prepare(self, db, case, user, *, parameters: dict, budget: dict) -> PreparedStep: ...
    def validate_spec(self, spec: dict) -> None: ...
    def artifacts(self, run, descriptor, output, frozen_files) -> dict: ...
    def source(self, prefix, descriptor) -> dict: ...


@lru_cache(maxsize=1)
def registered():
    from .geometry_handler import GeometryHandler
    from .mesh_handler import MeshHandler
    from .solve_handler import SolveHandler
    from .prepared_solve_handler import PreparedSolveHandler
    from .solve_resume_handler import ResumeHandler
    from .postprocess_handler import PostprocessHandler
    from .report_handler import ReportHandler
    from .compose_handler import ComposeHandler
    from .latex_handler import LatexHandler
    from .pdf_number_handler import PdfNumberHandler
    from .ppt_handler import PptHandler
    from .ppt_media_handler import PptMediaHandler
    from .legacy_postprocess_handler import LegacyPostprocessHandler
    from .calculix_handler import CalculixHandler
    from .su2_handler import Su2Handler
    from .elmer_handler import ElmerHandler
    from .moose_handler import MooseHandler
    from .code_aster_handler import CodeAsterHandler
    from .paraview_handler import ParaViewHandler
    from .mpi_handler import MpiHandler
    from .workflow_mpi_handler import WorkflowMpiHandler
    from .udf_handler import UdfHandler
    from .workflow_udf_handler import WorkflowUdfHandler
    from .flotherm_convert_handler import FlothermConvertHandler
    from .flotherm_library_handler import FlothermLibraryHandler
    from .case_command_handler import CaseCommandHandler, TYPES as COMMAND_TYPES
    handlers = (GeometryHandler('preview'), GeometryHandler('export'), MeshHandler(), SolveHandler(), PreparedSolveHandler(), ResumeHandler(), PostprocessHandler(), ReportHandler(),
        ComposeHandler('E-report/report.md'), ComposeHandler('E-report/report.generated.md'), LatexHandler(), PdfNumberHandler(), PptHandler(), LegacyPostprocessHandler(), CalculixHandler(), Su2Handler(), ParaViewHandler(), MpiHandler(), WorkflowMpiHandler(), UdfHandler('build'), UdfHandler('integrate'), WorkflowUdfHandler('build'), WorkflowUdfHandler('integrate'), FlothermConvertHandler(), FlothermLibraryHandler())
    handlers += (ElmerHandler(), MooseHandler(), CodeAsterHandler()) + tuple(CaseCommandHandler(kind) for kind in sorted(COMMAND_TYPES))
    handlers += (PptMediaHandler(),)
    return {(handler.key, handler.version):handler for handler in handlers}


def get_handler(key, version='1') -> StepHandler:
    handler = registered().get((key, version))
    if handler is None:
        raise ExecutionConflict('recovery_required', 'Professional handler/version is not registered: '+str(key))
    return handler


def kind_for(handler,descriptor):
    selector=getattr(handler,'publication_channel',None)
    kind=selector(descriptor) if selector else handler.publication_kind
    if not isinstance(kind,str) or not re.fullmatch(r'[a-z][a-z0-9:-]{0,79}',kind):
        raise ValueError('Invalid registered publication channel')
    return kind


def publication_handler(descriptor):
    if descriptor.get('handler')=='prepared-solve.v1':
        return get_handler('solve.openfoam.prepared',descriptor.get('handler_version','1'))
    if descriptor.get('handler') in {'workflow-udf-build.v1','workflow-udf.v1'}:
        return get_handler('udf.build.workflow' if descriptor['handler']=='workflow-udf-build.v1' else 'solve.openfoam.udf',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='workflow-mpi.v1':
        return get_handler('solve.openfoam.mpi',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='solve.resume.v1':
        return get_handler('solve.openfoam.resume',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='case-command.v1':
        return get_handler('command.'+descriptor['job_type'],descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='flotherm-library.v1':
        return get_handler('flotherm.build-library',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='flotherm-convert.v1':
        return get_handler('flotherm.convert',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='udf.v1' and descriptor.get('mode') in {'build','integrate'}:
        return get_handler('udf.'+descriptor['mode'],descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='mpi.v1':
        return get_handler('mpi.openfoam',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='paraview.v1':
        return get_handler('paraview.run',descriptor.get('handler_version','1'))
    # Preserve the descriptor identity already used by immutable geometry v1
    # bundles. Unknown names never become imports or shell commands.
    if descriptor.get('handler')=='su2.v1':
        return get_handler('su2.solve',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='calculix.v1':
        return get_handler('calculix.solve',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='elmer.v1':
        return get_handler('elmer.solve',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='moose.v1':
        return get_handler('moose.solve',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='code-aster.v1':
        return get_handler('code-aster.solve',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='postprocess-case.v1':
        return get_handler('postprocess.case',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='pdf-number.v1':
        return get_handler('pdf.number',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='ppt.v1':
        return get_handler('ppt.build',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='ppt-media.v1':
        return get_handler('ppt.video',descriptor.get('handler_version','1'))
    if descriptor.get('handler')=='report-compose.v1':
        from .compose_artifacts import OUTPUTS
        try:
            return get_handler(OUTPUTS[descriptor['output']][0],descriptor.get('handler_version','1'))
        except (KeyError,ExecutionConflict) as error:
            raise ValueError('Composition handler or output is unavailable') from error
    if descriptor.get('handler') in {'mesh.v1','solve.v1','postprocess.v1','report.v1','latex.v1'}:
        try:
            key = {'mesh.v1':'mesh.generate','solve.v1':'solve.openfoam','postprocess.v1':'postprocess.run','report.v1':'report.build','latex.v1':'latex.build'}[descriptor['handler']]
            return get_handler(key, descriptor.get('handler_version','1'))
        except ExecutionConflict as error:
            raise ValueError('Publication handler version is unavailable') from error
    if descriptor.get('handler') == 'geometry.v1' and descriptor.get('mode') in {'preview','export'}:
        try:
            return get_handler('geometry.'+descriptor['mode'], descriptor.get('handler_version','1'))
        except ExecutionConflict as error:
            raise ValueError('Publication handler version is unavailable') from error
    raise ValueError('Publication has no registered professional validator')


def validate_binding(job, attempt):
    from .job_execution import request_hash
    document = json.loads(attempt.input_manifest_json)
    record = document.get('handler')
    if record is None:
        return None
    if not isinstance(record, dict) or record.get('schema') != CONTRACT:
        raise ExecutionConflict('evidence_invalid', 'Professional handler record is damaged')
    handler = get_handler(record.get('key'), record.get('version'))
    spec = document.get('execution_spec', {})
    reservation = json.loads(attempt.publication_json)
    from .handler_environment import local_spec
    handler.validate_spec(local_spec(record,spec,reservation.get('descriptor',{})))
    try:
        channel=kind_for(handler,reservation.get('descriptor',{}))
    except (ValueError,KeyError,TypeError) as error:
        raise ExecutionConflict('evidence_invalid','Professional publication channel is invalid') from error
    if (job.type != handler.job_type or job.command != spec.get('command') or
            record.get('execution_sha256') != request_hash(spec) or
            record.get('publication_kind') != channel or
            not document.get('workspace') or reservation.get('schema') != 'vibeflow.artifact-publication.v1'):
        raise ExecutionConflict('evidence_invalid', 'Professional execution does not match its prepared contract')
    try:
        validator = publication_handler(reservation.get('descriptor', {}))
    except ValueError as error:
        raise ExecutionConflict('evidence_invalid', 'Professional publication descriptor is invalid') from error
    if (validator.key, validator.version) != (handler.key, handler.version):
        raise ExecutionConflict('evidence_invalid', 'Professional publication belongs to a different handler')
    if 'toolchain' in record:
        from .attempt_toolchain import declared
        expected = declared(handler.key,handler.version,spec,reservation['descriptor'])
        if record['toolchain'] != expected:
            raise ExecutionConflict('evidence_invalid', 'Prepared toolchain differs from its execution or publication binding')
    return handler


async def require_binding(db, job, attempt):
    """Legacy Jobs stay compatible; registered workflow contracts cannot vanish."""
    from ..models import WorkflowRun, WorkflowStep
    handler = validate_binding(job, attempt)
    if attempt.step_id:
        step = await db.get(WorkflowStep, attempt.step_id, populate_existing=True)
        run = await db.get(WorkflowRun, step.run_id, populate_existing=True) if step else None
        if run and json.loads(run.plan_json).get('execution_contract') == CONTRACT:
            if (handler is None or (handler.key, handler.version) != (step.handler, step.handler_version) or
                    run.case_id != job.case_id):
                raise ExecutionConflict('evidence_invalid', 'Registered workflow attempt lost its professional binding')
    return handler


async def bind(db, case, job, attempt, prepared, *, deliver_intent=True):
    from .. import storage
    if storage._use_s3() and not prepared.key.startswith('command.'):
        import asyncio
        from .async_ops import finish_before_cancel
        if prepared.key not in {'geometry.preview','geometry.export','mesh.generate','solve.openfoam','solve.openfoam.prepared','solve.openfoam.resume','postprocess.run','report.build',
                                'report.compose','report.compose-generated','latex.build','pdf.number','ppt.build','ppt.video',
                                'postprocess.case','calculix.solve','su2.solve','elmer.solve','moose.solve','code-aster.solve',
                                'paraview.run','mpi.openfoam','solve.openfoam.mpi',
                                'udf.build','udf.integrate','udf.build.workflow','solve.openfoam.udf','flotherm.convert','flotherm.build-library'}:
            raise ExecutionConflict('runtime_unavailable','This professional handler has not completed its S3 integration')
        from .s3_case_files import require_conditions
        await finish_before_cancel(asyncio.to_thread(require_conditions,case.s3_prefix))
    from .document_admission import validate_sealed
    validate_sealed(prepared,attempt)
    if prepared.generated_inputs:
        import hashlib
        from .attempt_workspace import canonical_path
        frozen=json.loads(attempt.input_manifest_json).get('workspace',{})
        expected={canonical_path(path):{'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),
            'executable':False} for path,data in prepared.generated_inputs.items()}
        if (set(frozen.get('generated_inputs',[]))!=set(expected) or
                any(frozen.get('files',{}).get(path)!=record for path,record in expected.items())):
            raise ExecutionConflict('evidence_invalid','Generated configuration differs from its sealed handler request')
    from .artifact_publication import reserve
    from .durable_events import json_text
    handler = get_handler(prepared.key, prepared.version)
    if prepared.job_type != handler.job_type or prepared.publication_kind != kind_for(handler,prepared.descriptor):
        raise ExecutionConflict('evidence_invalid', 'Prepared handler identity is inconsistent')
    publication = await reserve(db, case, attempt, kind=prepared.publication_kind,
        descriptor=prepared.descriptor)
    document = json.loads(attempt.input_manifest_json)
    document['handler'] = prepared.record()
    attempt.input_manifest_json = json_text(document)
    validate_binding(job, attempt)
    await db.flush()
    from ..models import JobSubmissionRequest
    from sqlalchemy import select
    from .job_delivery import request as request_delivery
    submission = await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id == job.id))
    if submission is None:
        raise ExecutionConflict('evidence_invalid','Professional delivery requires its admitted requester')
    if deliver_intent:
        from .workflow_batches import delivery_mode
        await request_delivery(db,job,user_id=submission.created_by,mode=await delivery_mode(db,attempt))
    return publication


@dataclass(frozen=True)
class LaunchStatus:
    """Durable submission state, including work owned by another API/worker."""
    job_id: int
    status: str

    @property
    def public_status(self):
        return self.status


async def launch(job_id):
    from sqlalchemy import select
    from ..db import SessionLocal
    from ..models import Job, JobAttempt
    from . import jobrunner, job_execution, job_delivery
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id)
            .order_by(JobAttempt.number.desc()).limit(1))
        if job is None or attempt is None or await require_binding(db, job, attempt) is None:
            raise ExecutionConflict('evidence_invalid', 'Job has no prepared professional contract')
        if await job_delivery.intent(db,job,attempt) is None:
            raise ExecutionConflict('evidence_invalid','Professional job has no durable delivery intent')
        identifier=attempt.id
    # Admission already committed its delivery intent. A scanner or another
    # worker may claim it before this HTTP request reaches launch; that is a
    # successful handoff, not an admission conflict or permission to run twice.
    try:
        outcome=await job_delivery.deliver(job_id)
    except job_delivery.DeliveryDeferred:
        outcome='deferred'  # Durable delivery will retry when capacity returns.
    if outcome not in {'started','already_owned_or_terminal','enqueued','deferred'}:
        raise ExecutionConflict('recovery_required','Professional delivery requires reconciliation: '+outcome)
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id)
            .order_by(JobAttempt.number.desc()).limit(1))
        if job is None or attempt is None or attempt.id!=identifier:
            raise ExecutionConflict('state_conflict','Professional attempt changed during delivery')
        proc=jobrunner.get_proc(job_id)
        return LaunchStatus(job_id,job_execution.public_status(job,attempt,
            local_status=proc.status if proc else None))
