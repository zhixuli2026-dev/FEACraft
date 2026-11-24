"""One execution deadline from the durable claim through terminal commit.

Queue waiting is excluded. A new phase or native child cannot renew the budget.
Expiration stops admission/execution, but never claims an in-flight writer drained.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field
import asyncio
from datetime import timezone
import json
import math
import time

from .workflow_state import ExecutionConflict

_current=ContextVar('execution_budget',default=None)
WINDOWS_COMPUTE_SCHEMA = 'vibeflow.windows-compute-policy.v1'
WINDOWS_COMPUTE_USAGE = 'attempt.windows_compute_usage'


def windows_compute_policy(previous=None):
    from ..config import get_settings

    previous_policy = json.loads(previous.input_manifest_json).get('windows_compute_policy') if previous else None
    if previous_policy is not None:
        return validate_windows_compute_policy(previous_policy)
    settings = get_settings()
    return {'schema': WINDOWS_COMPUTE_SCHEMA,
            'user_cpu_seconds': settings.execution_windows_cpu_limit_seconds,
            'committed_memory_bytes': settings.execution_windows_memory_limit_mb * 1024 * 1024}


def validate_windows_compute_policy(value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'user_cpu_seconds', 'committed_memory_bytes'}
            or value['schema'] != WINDOWS_COMPUTE_SCHEMA
            or type(value['user_cpu_seconds']) is not int or not 0 <= value['user_cpu_seconds'] <= 86400
            or type(value['committed_memory_bytes']) is not int or not 0 <= value['committed_memory_bytes'] <= 2**40):
        raise ValueError('Invalid frozen Windows compute policy')
    return dict(value)


def windows_compute_violation(policy, usage):
    if policy is None:
        return None
    cpu_seconds = policy['user_cpu_seconds']
    memory_bytes = policy['committed_memory_bytes']
    if cpu_seconds and (usage['kernel_cpu_limit_reached'] or usage['cpu_user_100ns'] >= cpu_seconds * 10_000_000):
        return 'cpu_limit'
    if memory_bytes and usage['peak_job_memory_bytes'] > memory_bytes:
        return 'memory_limit'
    return None


def windows_compute_receipt(policy, scope):
    usage=scope.compute_usage()
    if usage['active_processes']!=0:
        raise ValueError('Windows compute scope still has active members after stop')
    return {'schema':'vibeflow.windows-compute-usage.v1','policy':policy,
            'violation':windows_compute_violation(policy,usage),**usage}


def native_compute_policy(previous=None):
    from ..config import get_settings
    from .native_compute_v1 import validate_policy

    saved=json.loads(previous.input_manifest_json).get('native_compute_policy') if previous else None
    if saved is not None:
        return validate_policy(saved)
    settings=get_settings()
    return validate_policy({'cpu_limit_ns':settings.execution_wsl_cpu_limit_seconds*1_000_000_000,
                            'memory_limit_bytes':settings.execution_wsl_memory_limit_mb*1024**2})


def native_compute_violation(policy, observation):
    if policy['memory_limit_bytes'] and observation.get('memory_oom_kills',0)>0:
        return 'memory_limit'
    if policy['cpu_limit_ns'] and observation.get('cpu_used_ns',0)>=policy['cpu_limit_ns']:
        return 'cpu_limit'
    return None


def linux_compute_policy(previous=None):
    from ..config import get_settings
    from .native_compute_v1 import validate_policy

    saved = json.loads(previous.input_manifest_json).get('linux_compute_policy') if previous else None
    if saved is not None:
        return validate_policy(saved)
    settings = get_settings()
    return validate_policy({'cpu_limit_ns':settings.execution_linux_cpu_limit_seconds*1_000_000_000,
                            'memory_limit_bytes':settings.execution_linux_memory_limit_mb*1024**2})


class BudgetExceeded(ExecutionConflict):
    def __init__(self,kind='timeout'):
        super().__init__('resource_limit','Execution exceeded its whole-attempt '+kind+' budget')


@dataclass
class Budget:
    attempt_id: str
    deadline: float
    max_log_bytes: int
    committed: bool=False
    log_bytes: int=0
    log_sources: dict=field(default_factory=dict)
    unsettled_output_bytes: int=0
    violation: str|None=None
    violation_source: str|None=None
    exhausted: asyncio.Event=field(default_factory=asyncio.Event)
    storage_policy: dict|None=None
    storage_observation: dict|None=None
    storage_watch: object=None
    windows_compute_policy: dict|None=None
    native_compute_policy: dict|None=None
    linux_compute_policy: dict|None=None

    def remaining(self):
        return max(0.0,self.deadline-time.monotonic())

    def check(self):
        if not self.committed:
            if self.violation is not None:raise BudgetExceeded(self.violation)
            if self.remaining()<=0:
                self.trip('timeout','deadline')
                raise BudgetExceeded()

    def trip(self,kind,source):
        if not self.committed:
            if self.violation is None:self.violation,self.violation_source=kind,source
            self.exhausted.set()

    def consume_output(self,size,source):
        """Charge owned pipe reads and validated diagnostic-file growth.

Forwarded bytes cross another pipe and consume that pipe's budget too. This is
an execution-output allowance, not the eventual human-visible log file length.
Diagnostic files are sampled separately; their bytes are not pipe observations.
All consumers run on the owner's event loop; no await splits this reservation.
"""
        if type(size) is not int or size<0 or source not in {'root','native.worker','native.finalizer','diagnostic.worker','diagnostic.finalizer'}:
            raise ValueError('Invalid owned output accounting')
        if self.committed:return size
        allowed=min(size,max(0,self.max_log_bytes-self.log_bytes-self.unsettled_output_bytes))
        self.log_bytes+=size
        self.log_sources[source]=self.log_sources.get(source,0)+size
        if allowed<size:self.trip('log_limit',source)
        return allowed

    def usage(self):
        diagnostics=sum(value for key,value in self.log_sources.items() if key.startswith('diagnostic.'))
        value={'schema':'vibeflow.execution-output-usage.v2','max_log_bytes':self.max_log_bytes,
            'observed_output_bytes':self.log_bytes,'observed_pipe_bytes':self.log_bytes-diagnostics,
            'observed_diagnostic_bytes':diagnostics,'by_source':dict(self.log_sources),
            'violation':self.violation,'violation_source':self.violation_source}
        if self.unsettled_output_bytes:
            value.update(schema='vibeflow.execution-output-usage.v3',unsettled_output_bytes=self.unsettled_output_bytes)
        return value

    def restore(self,usage):
        from .execution_output_checkpoint import validate_usage
        validate_usage(usage,self.max_log_bytes)
        self.log_bytes=usage['observed_output_bytes'];self.log_sources=dict(usage['by_source'])
        self.unsettled_output_bytes=usage.get('unsettled_output_bytes',0)
        if usage['violation'] is not None:self.trip(usage['violation'],usage['violation_source'])

    @contextmanager
    def activate(self):
        token=_current.set(self)
        try:yield self
        finally:_current.reset(token)


def current(owner=None):
    value=_current.get()
    return value if value is not None and (owner is None or value.attempt_id==getattr(owner,'attempt_id',None)) else None


def failure(owner,status,code,reason):
    budget=current(owner)
    if budget is not None and not budget.committed and status not in {'cancelled','interrupted'}:
        try:budget.check()
        except BudgetExceeded:return 'failed',-1,'resource_limit'
    return status,code,reason


async def load(owner,*,recovery=False):
    from sqlalchemy import select
    from ..db import SessionLocal
    from ..models import DurableEvent
    from .job_execution import _owned
    async with SessionLocal() as db:
        job,attempt=await _owned(db,owner)
        manifest=json.loads(attempt.input_manifest_json)
        spec=manifest['execution_spec']
        seconds=spec['timeout_seconds'];limit=spec['max_log_bytes']
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or not 0<seconds<=86400:
            raise ExecutionConflict('evidence_invalid','Execution budget is invalid')
        if type(limit) is not int or not 1024<=limit<=64*1024*1024:
            raise ExecutionConflict('evidence_invalid','Execution output budget is invalid')
        events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
            DurableEvent.case_id==job.case_id,DurableEvent.type=='attempt.state_changed')))
        claims=[event for event in events if json.loads(event.payload_json).get('status')=='claimed']
        if len(claims)!=1 or claims[0].created_at is None:
            raise ExecutionConflict('evidence_invalid','Execution has no unique durable claim time')
        at=claims[0].created_at
        if at.tzinfo is None:at=at.replace(tzinfo=timezone.utc)
        remaining=at.timestamp()+seconds-time.time()
        budget=Budget(attempt.id,time.monotonic()+remaining,limit)
        from .execution_storage import validate
        policy=manifest.get('storage_policy')
        if policy is not None:budget.storage_policy=validate(policy)
        if manifest.get('windows_compute_policy') is not None:
            budget.windows_compute_policy=validate_windows_compute_policy(manifest['windows_compute_policy'])
        if manifest.get('native_compute_policy') is not None:
            from .native_compute_v1 import validate_policy
            budget.native_compute_policy=validate_policy(manifest['native_compute_policy'])
        if manifest.get('linux_compute_policy') is not None:
            from .native_compute_v1 import validate_policy
            budget.linux_compute_policy=validate_policy(manifest['linux_compute_policy'])
        if recovery:
            from .execution_output_checkpoint import read
            usage=(await read(db,job,attempt))['payload']['usage']
            budget.restore(usage)
        return budget


async def record_usage(db,job,attempt,owner):
    """Persist measured usage in the terminal transaction without filesystem I/O."""
    budget=current(owner)
    if budget is None:return
    from sqlalchemy import select
    from ..models import DurableEvent
    from .job_execution import attempt_event
    existing=await db.scalar(select(DurableEvent.id).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type=='attempt.resource_usage'))
    if existing is None:await attempt_event(db,job,attempt,'attempt.resource_usage',budget.usage())
    if budget.storage_observation is not None:
        existing=await db.scalar(select(DurableEvent.id).where(DurableEvent.attempt_id==attempt.id,
            DurableEvent.type=='attempt.storage_usage'))
        if existing is None:await attempt_event(db,job,attempt,'attempt.storage_usage',dict(budget.storage_observation))
