"""Live source/dependency validation under a renewable content reservation.

File reads run without an open case transaction. The final transaction must
recheck the complete local proof, database read sets and reservation, then
release the reservation atomically with the terminal result.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import math
import time

from ..models import Case
from . import case_validation as fence
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .durable_events import json_text
from .publication_inputs import PublicationInputStale, validate
from .publication_read_set import PublicationReadSet
from .workflow_state import ExecutionConflict


class JobHistoryRecoveryRequired(ExecutionConflict):
    """Source validation cannot proceed until historical Job writes are reconciled."""


def snapshot(job,attempt,case):
    return {'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,
        'claim_sha256':hashlib.sha256(attempt.claim_token.encode()).hexdigest(),
        'prefix':case.s3_prefix,'revision':case.dict_revision,'workflow_state':case.workflow_state,
        'input':attempt.input_manifest_json,'output':attempt.output_manifest_json,
        'publication':attempt.publication_json}


def error_record(error):
    return {'type':type(error).__name__,'message':str(error)[:2000]}


class SourceValidation:
    def __init__(self,owner):
        from ..config import get_settings
        settings=get_settings()
        self.owner=owner
        self.lease_seconds=settings.job_lease_seconds
        self.interval=settings.job_heartbeat_seconds
        self.reservation=None
        self.snapshot=None
        self.prepared_binding=None
        self.dependencies=PublicationReadSet()
        self.publication=PublicationReadSet()
        self.dependency_error=None
        self.publication_error=None
        self.source_stale=False
        self.alias_plan_sha256=None
        self.binding=None
        self.renewal=None
        self.renewal_error=None
        self.released=False

    def fingerprint(self):
        reservation=self.reservation
        return json_text({'snapshot':self.snapshot,'prepared':self.prepared_binding,
            'dependencies':self.dependencies.fingerprint(),'publication':self.publication.fingerprint(),
            'dependency_error':self.dependency_error,'publication_error':self.publication_error,
            'source_stale':self.source_stale,'alias_plan_sha256':self.alias_plan_sha256,
            'reservation':[reservation.case_id,reservation.job_id,reservation.attempt_id,
                reservation.generation,hashlib.sha256(reservation.token.encode()).hexdigest()]})

    async def acquire(self, status, prepared):
        from ..db import SessionLocal
        from .job_execution import _owned
        from .workflow_state import TERMINAL
        deadline=None
        while True:
            case_id=None
            try:
                async with SessionLocal() as db:
                    job,attempt=await _owned(db,self.owner)
                    if status!='succeeded' or job.status in TERMINAL or attempt.phase in TERMINAL:
                        return None
                    from .execution_budget import current
                    execution_budget=current(self.owner)
                    if execution_budget is not None:execution_budget.check()
                    if prepared is not None and prepared.error is not None:
                        # A rejected private result cannot publish. Waiting for
                        # a success fence here can wait forever on the very
                        # child whose missing stop receipt caused the rejection.
                        # The terminal transaction still checks the binding and
                        # leaves unknown drainage blocked for recovery.
                        return None
                    manifest=json.loads(attempt.input_manifest_json).get('workspace')
                    if not manifest and (prepared is None or prepared.error is not None):
                        return None
                    if deadline is None:
                        # Renewal duration is not a writer's allowed lifetime.
                        # Use the submitted budget to bound admission waiting;
                        # whole-attempt accounting belongs to the lifetime layer.
                        budget=json.loads(attempt.input_manifest_json)['execution_spec']['timeout_seconds']
                        if isinstance(budget,bool) or not isinstance(budget,(int,float)) or not math.isfinite(budget) or not 0<budget<=86400:
                            raise ExecutionConflict('evidence_invalid','Source validation has no valid wait budget')
                        deadline=time.monotonic()+(execution_budget.remaining() if execution_budget is not None else budget)
                    case=await db.get(Case,job.case_id,populate_existing=True)
                    case_id=case.id
                    self.reservation=await fence.reserve_locked(db,self.owner,lease_seconds=self.lease_seconds)
                    self.snapshot=snapshot(job,attempt,case)
                    db.expunge(case)
                    await db.commit()
                    self.renewal=asyncio.create_task(self._renew())
                    return case
            except CaseWriteConflict as error:
                if error.code not in {'validation_busy','validation_job_busy','validation_native_busy'}:
                    raise
                from .execution_budget import current
                execution_budget=current(self.owner)
                if execution_budget is not None:execution_budget.check()
                requires_recovery=getattr(error,'requires_recovery',False)
                if requires_recovery or deadline is None or time.monotonic()>=deadline:
                    conflict=(JobHistoryRecoveryRequired if requires_recovery and error.code=='validation_job_busy'
                              else ExecutionConflict)
                    raise conflict('recovery_required',
                        'Source validation is waiting for case writers or unverified history: '+error.code) from error
                # Separate committed revocation; never embedded in a writer txn.
                if case_id is not None:
                    await fence.revoke_expired(case_id)
                await asyncio.sleep(.1)

    async def _renew(self):
        from ..db import SessionLocal
        try:
            while True:
                await asyncio.sleep(self.interval)
                async with SessionLocal() as db:
                    await fence.renew_locked(db,self.owner,self.reservation,lease_seconds=self.lease_seconds)
                    await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Retain exclusion until all file work drains. A failed renewal is
            # irreversible for this proof even if a later DB query succeeds.
            self.renewal_error=error

    async def stop_renewal(self):
        if self.renewal is not None:
            self.renewal.cancel()
            await asyncio.gather(self.renewal,return_exceptions=True)
            self.renewal=None

    async def inspect(self,case,prepared):
        manifest=json.loads(self.snapshot['input']).get('workspace')
        if manifest and prepared is None:
            from .attempt_workspace import verify
            try:
                await finish_before_cancel(asyncio.to_thread(verify,case.s3_prefix,manifest))
                if manifest.get('storage',{}).get('backend')=='s3':
                    from .attempt_objects import verify_outputs
                    await finish_before_cancel(asyncio.to_thread(verify_outputs,case.s3_prefix,manifest,
                        json.loads(self.snapshot['output'])))
            except (OSError,ValueError,KeyError,TypeError) as error:
                self.dependency_error=error_record(error)
        if manifest and manifest.get('publications'):
            try:
                await validate(self.dependencies,case,manifest)
            except PublicationInputStale as error:
                self.dependency_error=error_record(error)
        self.dependencies.seal()
        if prepared is not None and prepared.error is None:
            from .step_handlers import publication_handler,kind_for
            try:
                handler=publication_handler(prepared.descriptor)
                if prepared.kind!=kind_for(handler,prepared.descriptor):
                    raise ValueError('Publication kind does not match its registered handler')
                validator=getattr(handler,'validate_publication',None)
                if validator is not None:
                    await validator(self.publication,case,prepared.descriptor)
                observed=await finish_before_cancel(asyncio.to_thread(handler.source,case.s3_prefix,prepared.descriptor))
                self.source_stale=observed!=prepared.descriptor['source']
                before_publish=getattr(handler,'before_publish',None)
                if before_publish is not None and not self.source_stale:
                    await finish_before_cancel(asyncio.to_thread(before_publish,case.s3_prefix,prepared.descriptor))
                if not self.source_stale:
                    from dataclasses import replace
                    from .publication_alias_journal import prepare as prepare_aliases
                    row=replace(prepared.publication,manifest_sha256=prepared.bundle_sha256)
                    from .. import storage
                    if storage._use_s3():
                        # Remote alias preparation authenticates this exact
                        # metadata, the inventory and all file bytes itself.
                        from .publication_journal import bundle_manifest
                        bundle=bundle_manifest(row,prepared.records,prepared.descriptor,prefix=case.s3_prefix)
                    else:
                        from .artifact_publication import read_manifest
                        _,bundle=await finish_before_cancel(asyncio.to_thread(read_manifest,case.s3_prefix,row))
                    self.alias_plan_sha256=await finish_before_cancel(asyncio.to_thread(prepare_aliases,case.s3_prefix,row,bundle))
            except (OSError,ValueError,KeyError,TypeError) as error:
                self.publication_error=error_record(error)
        self.publication.seal()
        if prepared is not None and prepared.verification_binding()!=self.prepared_binding:
            raise ValueError('Publication preparation changed during source validation')
        self.binding=self.fingerprint()

    async def require_current(self,db,job,attempt,case,prepared):
        if (self.binding is None or self.binding!=self.fingerprint()
                or snapshot(job,attempt,case)!=self.snapshot
                or (prepared.verification_binding() if prepared is not None else None)!=self.prepared_binding):
            raise PublicationInputStale('来源校验不再属于当前完整作业快照')
        await self.require_lease(db)

    async def require_lease(self,db):
        if self.renewal_error is not None:
            raise fence.ValidationConflict('来源校验续租失败，不能提交成功',stale=True) from self.renewal_error
        await fence.require_current_locked(db,self.owner,self.reservation)

    async def require_dependencies(self,db):
        await self.dependencies.require_current(db)
        if self.dependency_error is not None:
            raise PublicationInputStale(self.dependency_error['message'])

    async def require_publication(self,db):
        await self.publication.require_current(db)
        if self.publication_error is not None:
            raise ValueError(self.publication_error['message'])
        return not self.source_stale

    async def release_locked(self,db,status):
        if status=='succeeded':
            if self.renewal_error is not None:
                raise fence.ValidationConflict('来源校验续租失败，不能提交成功',stale=True)
            await fence.release_current_locked(db,self.owner,self.reservation)
        else:
            await fence.release_locked(db,self.reservation)

    async def close(self):
        from ..db import SessionLocal
        await self.stop_renewal()
        if self.reservation is not None and not self.released:
            async with SessionLocal() as db:
                await fence.release_locked(db,self.reservation)
                await db.commit()
            self.released=True


@asynccontextmanager
async def for_finish(owner,status,prepared):
    validation=SourceValidation(owner)
    try:
        case=await validation.acquire(status,prepared)
        # Other case-bound native writers may have changed private files before
        # their drain receipts were committed. Hash only after reserving the
        # now-quiescent case, not before waiting for those writers to finish.
        from .artifact_publication import verify_files_for_finish
        prepared=await verify_files_for_finish(owner,prepared)
        if case is None:
            yield None,prepared
        else:
            validation.prepared_binding=prepared.verification_binding() if prepared is not None else None
            await validation.inspect(case,prepared)
            yield validation,prepared
    finally:
        await finish_before_cancel(validation.close())
