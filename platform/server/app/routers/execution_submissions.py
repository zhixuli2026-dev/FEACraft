"""Read original admission receipts without dispatching or re-running work."""
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import JobSubmissionRequest, User, WorkflowRun
from ..services import job_execution
from ..services.calculix_pressure_transfer import PressureTransferRequest
from ..services.durable_events import lock_case_execution
from .workspaces import get_owned_case

router = APIRouter(prefix='/api/v1', tags=['jobs'])


class LookupIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['single', 'batch', 'workflow', 'native']
    key: str = Field(min_length=1, max_length=128, pattern=r'^[!-~]+$')
    body: dict


class NativeRestart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    publication_id: str
    checkpoint_path: str


class NativeLookup(BaseModel):
    model_config = ConfigDict(extra='forbid')
    solver: Literal['calculix', 'su2', 'elmer', 'moose', 'code-aster']
    input_path: str
    restart: NativeRestart | None = None
    pressure_transfer: PressureTransferRequest | None = None

    def submission(self):
        from ..services.native_solver_submission import submission_payload

        if self.restart is not None and self.solver not in {'moose', 'code-aster'}:
            raise ValueError('Solver does not accept a restart reference')
        fields = {'config_path' if self.solver == 'su2' else 'input_path': self.input_path}
        if self.restart is not None:
            fields['restart'] = self.restart.model_dump()
        if self.pressure_transfer is not None:
            if self.solver != 'calculix':
                raise ValueError('Solver does not accept a pressure transfer')
            fields['pressure_transfer'] = self.pressure_transfer.model_dump(mode='json',exclude_none=True)
        return submission_payload(self.solver, fields)


@router.post('/cases/{case_id}/execution-submission')
async def lookup(case_id: int, body: LookupIn, user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)):
    from .jobs import JobIn, BatchJobIn, _latest_attempt, _read_batch
    from .workflows import RunIn
    from ..services import job_delivery, workflow_scheduler
    from ..services.case_evidence import safe_run_dir
    if len(json.dumps(body.body, ensure_ascii=False)) > 240000:
        raise HTTPException(400, 'Submission lookup is too large')
    await get_owned_case(case_id, user, db, write=False)
    # Serialize with admission, then roll back the control lock. Not-found is
    # only an observation, never permission to abandon an uncertain request.
    await lock_case_execution(db, case_id)
    try:
        model = WorkflowRun if body.kind == 'workflow' else JobSubmissionRequest
        row = await db.scalar(select(model).where(model.case_id == case_id,
            model.created_by == user.id, model.idempotency_key == body.key))
        if row is None:
            return {'found': False, 'case_id': case_id, 'kind': body.kind, 'id': None}
        try:
            if body.kind == 'workflow':
                value = RunIn.model_validate(body.body)
                payload = {'target': value.target, 'run_dir': safe_run_dir(value.run_dir),
                    'expected_revision': value.expected_revision, **value.budget.model_dump(),
                    'parameters': workflow_scheduler.normalized_parameters(value.parameters.model_dump(exclude_none=True))}
            elif body.kind == 'single':
                value = JobIn.model_validate(body.body)
                payload = value.model_dump(exclude={'input_paths', 'input_publications'})
                if value.run_dir is not None:
                    payload['run_dir'] = safe_run_dir(value.run_dir)
                if value.input_paths is not None:
                    payload['input_paths'] = value.input_paths
                if value.input_publications is not None:
                    from ..services.publication_inputs import normalize
                    payload['input_publications'] = normalize([p.model_dump() for p in value.input_publications])
            elif body.kind == 'native':
                payload = NativeLookup.model_validate(body.body).submission()
            else:
                value = BatchJobIn.model_validate(body.body)
                payload = {'endpoint': 'jobs.batch', 'payload': value.model_dump(exclude={'operations'})
                    if value.operations is None else value.model_dump(exclude_none=True)}
            if row.request_hash != job_execution.request_hash(payload):
                raise HTTPException(409, {'code': 'idempotency_conflict', 'message': 'Original submission has a different payload'})
        except (ValueError, ValidationError, job_execution.ExecutionConflict) as error:
            raise HTTPException(409, {'code': 'idempotency_conflict', 'message': 'Original submission payload cannot be matched'}) from error
        identifier = row.id if body.kind == 'workflow' else row.job_id
        if body.kind in {'single', 'native'}:
            from ..models import Job
            job = await db.get(Job, row.job_id, populate_existing=True)
            if job is None or job.case_id != case_id:
                raise HTTPException(409, 'Original job binding is unavailable')
        if body.kind == 'batch':
            job = await job_execution.lookup_submission(db, case_id, user.id, body.key, row.request_hash)
            if job is None or job.case_id != case_id:
                raise HTTPException(409, 'Original batch job is unavailable')
            attempt = await _latest_attempt(db, job.id)
            delivery = await job_delivery.intent(db, job, attempt) if attempt else None
            group = delivery.get('batch') if delivery else None
            if not group:
                raise HTTPException(409, 'Original batch binding is unavailable')
            case = await get_owned_case(case_id, user, db, write=False)
            from ..services.case_read_capacity import reading
            async with reading(db, case):
                manifest = _read_batch(case, group['id'])
            if manifest['job_ids'] != group['jobs'] or manifest['max_concurrent'] != group['max_concurrent']:
                raise HTTPException(409, 'Original batch manifest differs from its admission')
            identifier = group['id']
        return {'found': True, 'case_id': case_id, 'kind': body.kind, 'id': identifier}
    finally:
        await db.rollback()
