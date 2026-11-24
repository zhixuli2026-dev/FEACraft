"""Document preparation without retaining the case write transaction over a tool."""
import asyncio
import subprocess
from copy import deepcopy
from dataclasses import replace

from fastapi import HTTPException

from .async_ops import finish_before_cancel
from .workflow_state import ExecutionConflict


def pending(prepared,case,parameters,budget,*,dictionary=None,**extras):
    from .attempt_workspace import inventory
    from .case_read_view import inspect
    descriptor={key:value for key,value in prepared.descriptor.items()
                if key not in {'execution_id','generated_at','epoch'}}
    guard={'revision':case.dict_revision,'descriptor':descriptor,'paths':prepared.input_paths,
        'references':prepared.input_publications,'python':prepared.spec['argv'][0],
        'budget':budget,'prefix':case.s3_prefix,
        'dictionary':dictionary if dictionary is not None else
            inspect(case.s3_prefix,inventory,['0-caseDict/caseDict'],paths=['0-caseDict/caseDict'])}
    return replace(prepared,admission={'guard':deepcopy(guard),'parameters':deepcopy(parameters),
        'budget':dict(budget),'ready':False,**extras})


def ready(prepared,**changes):
    return replace(prepared,admission={**prepared.admission,'ready':True},**changes)


async def complete(prepared,case_id,user_id,*,request=None,invalidated=None):
    from .step_handlers import get_handler
    from .request_lifecycle import wait
    handler=get_handler(prepared.key,prepared.version)
    try:
        return await wait(handler.complete_preparation(prepared,case_id,user_id),request=request,invalidated=invalidated)
    except (OSError,subprocess.TimeoutExpired,TimeoutError) as error:
        raise ExecutionConflict('native_preparation_failed','Owned document preparation did not finish: '+type(error).__name__) from error


async def authorize(db,case_id,user_id,*,write=True):
    from ..models import User
    from ..routers.workspaces import get_owned_case
    db.expire_all()
    user=await db.get(User,user_id)
    if user is None or user.status!='active':raise HTTPException(401,'用户不可用')
    case=await get_owned_case(case_id,user,db,write=write)
    return case,user


async def check_access(db,case,user,*,write=True):
    from ..models import Workspace
    from ..routers.workspaces import get_owned_case
    await db.refresh(user)
    if user.status!='active':raise HTTPException(401,'用户不可用')
    await db.get(Workspace,case.workspace_id,populate_existing=True)
    await get_owned_case(case.id,user,db,write=write)


def validate_sealed(prepared,attempt):
    import json
    if prepared.admission is None:return
    manifest=json.loads(attempt.input_manifest_json).get('workspace')
    validate_workspace(prepared,manifest)


def validate_workspace(prepared,manifest):
    prepared.record()
    if not manifest:raise ExecutionConflict('evidence_invalid','Document has no sealed workspace')
    expected={**prepared.descriptor['source']['files'],**prepared.admission['guard']['dictionary']}
    for path,record in expected.items():
        sealed=manifest['files'].get(path)
        if sealed is None or any(sealed.get(key)!=record[key] for key in ('size','sha256')):
            raise ExecutionConflict('input_stale','Sealed document bytes differ from prepared inputs: '+path)


async def validate(db,case,user,prepared):
    from .step_handlers import get_handler
    if prepared.admission is None or not prepared.admission['ready']:
        raise ExecutionConflict('evidence_invalid','Document preparation is incomplete')
    handler=get_handler(prepared.key,prepared.version)
    try:
        fresh=await handler.prepare(db,case,user,parameters=prepared.admission['parameters'],budget=prepared.admission['budget'])
        if fresh.admission['guard']!=prepared.admission['guard']:
            raise ExecutionConflict('input_stale','Document inputs, runtime or engineering publications changed during preparation')
        observed=await finish_before_cancel(asyncio.to_thread(handler.source,case.s3_prefix,prepared.descriptor))
        if observed!=prepared.descriptor['source']:
            raise ExecutionConflict('input_stale','Document source files changed during preparation')
    except (OSError,ValueError,KeyError) as error:
        raise ExecutionConflict('input_stale','Document preparation inputs are no longer available') from error
    prepared.record()
