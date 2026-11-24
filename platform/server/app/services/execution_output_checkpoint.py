"""Immutable output accounting after native writers drain, before bundle copying.

Recovery can resume copying/verification, but never grants fresh native output
credit. Missing historical accounting requires attention instead of guessing zero.
"""
import hashlib
import json

from sqlalchemy import select

from ..models import DurableEvent
from .workflow_state import ExecutionConflict

EVENT='attempt.output_drained'
SOURCES={'root','native.worker','native.finalizer','diagnostic.worker','diagnostic.finalizer'}


def binding(attempt,usage):
    return {'schema':'vibeflow.execution-output-checkpoint.v1','usage':usage,
        **{name:hashlib.sha256(getattr(attempt,field).encode('utf8')).hexdigest() for name,field in (
            ('input_sha256','input_manifest_json'),('worker_sha256','worker_identity_json'),
            ('process_sha256','process_identity_json'),('children_sha256','child_processes_json'))}}


def validate_usage(value,limit):
    fields={'schema','max_log_bytes','observed_output_bytes','observed_pipe_bytes',
        'observed_diagnostic_bytes','by_source','violation','violation_source'}
    if not isinstance(value,dict):raise ValueError('Recovery requires complete output accounting')
    unsettled=value.get('unsettled_output_bytes',0)
    if value.get('schema')=='vibeflow.execution-output-usage.v3':
        fields=fields|{'unsettled_output_bytes'}
        if type(unsettled) is not int or not 0<unsettled<=64*1024*1024:
            raise ValueError('Invalid retained unknown output allowance')
    elif value.get('schema')!='vibeflow.execution-output-usage.v2':
        raise ValueError('Unsupported output accounting version')
    if set(value)!=fields:
        raise ValueError('Recovery requires complete output accounting')
    for key in ('max_log_bytes','observed_output_bytes','observed_pipe_bytes','observed_diagnostic_bytes'):
        if type(value[key]) is not int or value[key]<0:raise ValueError('Invalid output counter')
    sources=value['by_source']
    if not isinstance(sources,dict) or not set(sources)<=SOURCES:
        raise ValueError('Unknown output accounting source')
    if any(type(n) is not int or n<0 for n in sources.values()):raise ValueError('Invalid source counter')
    diagnostics=sum(n for key,n in sources.items() if key.startswith('diagnostic.'))
    if (value['max_log_bytes']!=limit or value['observed_output_bytes']!=sum(sources.values())
            or value['observed_diagnostic_bytes']!=diagnostics
            or value['observed_pipe_bytes']+diagnostics!=value['observed_output_bytes']):
        raise ValueError('Recovery output counters do not match their frozen allowance')
    violation=value['violation'];source=value['violation_source']
    if ((violation is None and source is not None)
            or (violation=='timeout' and source!='deadline')
            or (violation=='log_limit' and source not in SOURCES)
            or (violation in {'disk_limit','disk_unverified'} and source!='storage')
            or (violation=='storage_limit' and source not in {'publication','archive','log','staging'})
            or (violation in {'cpu_limit','memory_limit','compute_unverified'} and source not in {'windows_job','wsl_cgroup','linux_cgroup'})
            or violation not in {None,'timeout','log_limit','disk_limit','disk_unverified','storage_limit',
                                 'cpu_limit','memory_limit','compute_unverified'}
            or (value['observed_output_bytes']+unsettled>limit and violation is None)):
        raise ValueError('Recovery output violation is inconsistent')
    return value


async def read(db,job,attempt):
    rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==EVENT)))
    if len(rows)!=1 or rows[0].case_id!=job.case_id:
        raise ExecutionConflict('recovery_required','Execution has no unique drained output accounting')
    event=rows[0];payload=json.loads(event.payload_json)
    usage=validate_usage(payload['usage'],json.loads(attempt.input_manifest_json)['execution_spec']['max_log_bytes'])
    if payload!=binding(attempt,usage):raise ValueError('Drained output accounting belongs to changed execution')
    return {'event_id':event.id,'seq':event.seq,'payload':payload}


async def record(db,job,attempt,owner):
    from .execution_budget import current
    from .job_execution import attempt_event
    budget=current(owner)
    if budget is None:return  # Historical/custom callers do not gain recovery authority.
    usage=validate_usage(budget.usage(),json.loads(attempt.input_manifest_json)['execution_spec']['max_log_bytes'])
    existing=await db.scalar(select(DurableEvent.id).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==EVENT))
    if existing is not None:
        previous=(await read(db,job,attempt))['payload']['usage']
        # A deadline may expire during copying; drained output counters cannot grow.
        if any(previous[key]!=usage[key] for key in usage if key not in {'violation','violation_source'}):
            raise ValueError('Output was emitted after its drain checkpoint')
        return
    await attempt_event(db,job,attempt,EVENT,binding(attempt,usage))
