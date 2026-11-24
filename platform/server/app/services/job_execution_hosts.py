"""Explicit deployment routing; durable intents retain their selected host."""
import os
import re

from ..config import get_settings
from . import process_identity_native as identity
from .workflow_state import ExecutionConflict


def platform_name():
    return 'windows' if os.name=='nt' else 'linux'


def require_remote(plan):
    if plan.get('parameters',{}).get('placement')!='remote':
        return
    steps=[step for step in plan['steps'] if step['key'] in {'solve','udf-build'} and not step.get('reuse')]
    if not steps:
        return
    from .distributed_queue import distributed_enabled
    from . import openfoam_runtime
    if not distributed_enabled():
        raise ExecutionConflict('runtime_unavailable', '远程求解需要部署已配置的 Redis worker 路由')
    for step in steps:
        handler=step['handler']
        runtime=(openfoam_runtime.transport('',openfoam_runtime.distribution())['runtime']
                 if handler in {'solve.openfoam','solve.openfoam.resume'} else 'windows')
        if selected(runtime,'redis',handler=handler) is None:
            raise ExecutionConflict('runtime_unavailable','当前求解处理器没有部署可用的远程 worker 路由')


async def requires_remote(db, job, attempt):
    if attempt.step_id is None:
        return False
    from ..models import WorkflowRun,WorkflowStep
    from .workflow_queries import plan_record
    step=await db.get(WorkflowStep,attempt.step_id)
    if step is None:
        raise ExecutionConflict('evidence_invalid','Delivery lost its registered workflow step')
    if step.step_key not in {'solve','udf-build'}:
        return False
    run=await db.get(WorkflowRun,step.run_id)
    if run is None or run.case_id!=job.case_id:
        raise ExecutionConflict('evidence_invalid','Delivery lost its original workflow placement')
    return plan_record(run).get('parameters',{}).get('placement')=='remote'


def selected(runtime, mode, *, handler=None):
    settings = get_settings()
    mapping = settings.job_execution_hosts
    handlers = settings.job_handler_execution_hosts
    if not isinstance(mapping, dict) or set(mapping)-{'windows','wsl','wsl-sandbox'}:
        raise ExecutionConflict('runtime_unavailable', 'Invalid execution host mapping')
    if not isinstance(handlers,dict):
        raise ExecutionConflict('runtime_unavailable','Invalid handler execution host mapping')
    if handlers:
        from .step_handlers import registered
        if set(handlers)-{key for key,version in registered()}:
            raise ExecutionConflict('runtime_unavailable','Execution host override names an unregistered handler')
    for target in [*mapping.values(),*handlers.values()]:
        if (not isinstance(target, dict) or set(target)!={'host_id','platform'}
                or not isinstance(target.get('host_id'),str)
                or not re.fullmatch('[0-9a-f]{64}', target.get('host_id',''))
                or target.get('platform') not in {'windows','linux'}):
            raise ExecutionConflict('runtime_unavailable', 'Invalid execution host mapping')
    if any(name in {'wsl','wsl-sandbox'} and target['platform']!='windows' for name,target in mapping.items()):
        raise ExecutionConflict('runtime_unavailable','WSL execution requires a Windows host')
    fallback = handlers.get('solve.openfoam',mapping.get(runtime)) if handler=='solve.openfoam.resume' else mapping.get(runtime)
    target = handlers.get(handler,fallback)
    if target is None:
        return None
    if runtime in {'wsl','wsl-sandbox'} and target['platform']!='windows':
        raise ExecutionConflict('runtime_unavailable','WSL handler execution requires a Windows host')
    if mode!='redis':
        raise ExecutionConflict('runtime_unavailable', 'Targeted execution requires Redis delivery')
    if target['host_id']!=identity.host_id():
        from sqlalchemy.engine import make_url
        if settings.storage_backend!='s3' or make_url(settings.database_url).get_backend_name()!='postgresql':
            raise ExecutionConflict('runtime_unavailable', 'Remote execution requires shared PostgreSQL and S3')
    return dict(target)


def queue_name(host_id):
    if not isinstance(host_id,str) or not re.fullmatch('[0-9a-f]{64}',host_id):
        raise ValueError('Invalid execution queue host')
    return 'vibeflow:jobs:host:'+host_id


def worker_queue():
    return queue_name(identity.host_id()) if get_settings().worker_host_queue else 'vibeflow:jobs'
