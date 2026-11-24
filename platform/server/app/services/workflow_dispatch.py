"""Durable continuation of ready registered steps, including build -> solve.

Ready steps and admitted attempts waiting for their first claim advance.
Failed/cancelled workflows and human confirmation nodes never advance here.
"""
import asyncio,json,logging
from sqlalchemy import exists,func,select

from ..db import SessionLocal
from ..models import Case,JobAttempt,User,Workspace,WorkflowRun,WorkflowStep
from .step_handlers import CONTRACT
from .workflow_state import TERMINAL

logger=logging.getLogger(__name__)


async def pending(limit=32):
    native=WorkflowStep.handler.not_in({'case.preflight','engineering.confirm'})
    first_claim = ((WorkflowStep.status=='running') & exists(select(JobAttempt.id).where(
        JobAttempt.id==WorkflowStep.active_attempt_id,JobAttempt.phase=='queued',
        JobAttempt.claim_token.is_(None),JobAttempt.cancel_requested_at.is_(None))))
    actionable=exists(select(WorkflowStep.id).where(WorkflowStep.run_id==WorkflowRun.id,native,
        ((WorkflowStep.status=='ready') | first_claim | (WorkflowStep.status.in_({'running','finalizing','cancelling'}) &
            exists(select(JobAttempt.id).where(JobAttempt.id==WorkflowStep.active_attempt_id,JobAttempt.phase.in_(TERMINAL)))))))
    cases=select(WorkflowRun.id,WorkflowRun.created_at,Workspace.user_id.label('owner_id'),
        func.row_number().over(partition_by=WorkflowRun.case_id,order_by=(WorkflowRun.created_at,WorkflowRun.id)).label('case_rank'))\
        .join(Case,Case.id==WorkflowRun.case_id).join(Workspace,Workspace.id==Case.workspace_id)\
        .where(WorkflowRun.status.not_in(TERMINAL),WorkflowRun.plan_json.contains(CONTRACT),
            (WorkflowRun.status=='queued')|actionable).subquery()
    owners=select(cases,func.row_number().over(partition_by=cases.c.owner_id,
        order_by=(cases.c.case_rank,cases.c.created_at,cases.c.id)).label('owner_rank')).subquery()
    async with SessionLocal() as db:
        return list(await db.scalars(select(owners.c.id).order_by(owners.c.owner_rank,owners.c.case_rank,
            owners.c.created_at,owners.c.id).limit(limit)))


async def recover_once():
    from .workflow_execution import dispatch
    semaphore=asyncio.Semaphore(4);outcomes={}
    async def advance(identifier):
        async with semaphore:
            try:
                async with SessionLocal() as db:
                    run=await db.get(WorkflowRun,identifier)
                    if run is None or run.status in TERMINAL:return
                    if json.loads(run.plan_json).get('execution_contract')!=CONTRACT:return
                    user=await db.get(User,run.created_by)
                    if user is None:return
                    outcomes[identifier]=await dispatch(db,identifier,user)
            except asyncio.CancelledError:raise
            except Exception as error:
                # The original ready identity remains persisted. Admission
                # failures cannot authorize a replacement Job or bypass access.
                outcomes[identifier]=getattr(error,'code',type(error).__name__)
                logger.debug('Workflow continuation deferred: %s (%s)',identifier,outcomes[identifier])
    await asyncio.gather(*(advance(identifier) for identifier in await pending()))
    return outcomes


async def recover_forever(stop):
    while not stop.is_set():
        try:await recover_once()
        except asyncio.CancelledError:raise
        except Exception:logger.warning('Workflow continuation scan failed',exc_info=True)
        try:await asyncio.wait_for(stop.wait(),timeout=2)
        except asyncio.TimeoutError:pass
