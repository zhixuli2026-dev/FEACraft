"""Independent Windows scope witnesses for engineering and sealed commands.

These witnesses prove host Job closure only. A native root exiting is retained
as an observation and never substituted for a Linux tree/drain certificate.
"""
import asyncio,hashlib,json,math,os
from pathlib import Path,PureWindowsPath
from sqlalchemy import select
from .. import storage
from ..db import SessionLocal
from ..models import DurableEvent,Job,JobAttempt
from . import job_execution,job_worker_identity,process_record,process_identity_native as identity
from . import node_storage_budget as capacity,windows_scope_guardian as protocol
from .async_ops import finish_before_cancel
from .durable_events import json_text

SCHEMA='vibeflow.job-scope-guardian.v1'
PREPARED='attempt.scope_guardian_prepared'
OBSERVED='attempt.scope_guardian_observed'
DIRECTORY='.vibeflow-node-guardians'


def _sealed_unregistered_command(job,attempt,runtime,*,job_type='shell',explicit_only=False):
    """An unregistered command must keep its sealed private workspace.

    Native roots additionally require their own kernel-scope protocol. This
    predicate never reconstructs missing inputs or accepts a legacy unsealed
    command, workflow step, argv-based handler, or publication reservation.
    """
    from .attempt_workspace import require_sealed
    try:
        workspace=require_sealed(job,attempt)
        if explicit_only and (workspace['source'].get('selection_mode')!='explicit'
                or not isinstance(workspace.get('selection'),list) or not workspace['selection']
                or workspace.get('publications') or workspace.get('generated_inputs')):
            return False
        document=json.loads(attempt.input_manifest_json)
        spec=document['execution_spec']
        return (job.type==job_type and attempt.step_id is None
                and document.get('handler') is None and json.loads(attempt.publication_json)=={}
                and spec.get('runtime')==runtime and spec.get('argv') is None
                and spec.get('evidence_run_dir') is None
                and isinstance(spec.get('command'),str) and bool(spec['command'].strip())
                and spec['command']==job.command)
    except (ValueError,KeyError,TypeError,AttributeError,job_execution.ExecutionConflict):
        return False


def sealed_windows_shell(job,attempt):
    return _sealed_unregistered_command(job,attempt,'windows')


def sealed_windows_command(job,attempt):
    if job.type=='convert':
        return _sealed_unregistered_command(job,attempt,'windows',job_type='convert',explicit_only=True)
    return sealed_windows_shell(job,attempt)


def _sealed_typed_command(job,attempt,runtime):
    """Typed commands and budgeted WSL handlers use the sealed native gate."""
    from .attempt_workspace import require_sealed
    from .case_command_handler import CaseCommandHandler
    from .step_handlers import validate_binding
    try:
        require_sealed(job,attempt)
        handler=validate_binding(job,attempt)
        manifest=json.loads(attempt.input_manifest_json)
        spec=manifest['execution_spec']
        policy=manifest.get('native_compute_policy')
        budgeted=runtime=='wsl' and isinstance(policy,dict) and any(policy.values())
        return (isinstance(handler,CaseCommandHandler) or (handler is not None and budgeted)) and (
            spec['runtime']==runtime and spec['argv'] is None)
    except (ValueError,KeyError,TypeError,AttributeError,job_execution.ExecutionConflict):
        return False


def sealed_sandbox_shell(job,attempt):
    """The fixed sandbox launcher confines its payload before running user code."""
    return _sealed_unregistered_command(job,attempt,'wsl-sandbox')


def sealed_wsl_shell(job,attempt):
    """Eligibility only; recovery also requires the actual unprivileged gate."""
    return _sealed_unregistered_command(job,attempt,'wsl') or _sealed_typed_command(job,attempt,'wsl')


def sealed_native_shell(job,attempt):
    return sealed_sandbox_shell(job,attempt) or sealed_wsl_shell(job,attempt)


async def prepare(owner,owned,*,seconds,child_id=None,parent_nonce=None):
    if os.name!='nt':return None
    from .step_handlers import require_binding
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        if (await require_binding(db,job,attempt) is None
                and not sealed_windows_command(job,attempt) and not sealed_native_shell(job,attempt)):return None
        worker=job_worker_identity.document(job,attempt)
        if worker['process']!=identity.observe(os.getpid()):raise ValueError('Guardian owner differs from attempt worker')
    root=storage._fs_root().resolve()/DIRECTORY/owned.nonce
    # Initial request/preparation files are bounded. The independent guardian
    # owns its separate future-receipt claim, which survives this worker dying.
    async with capacity.allocation(protocol.LIMIT*4):
        await finish_before_cancel(asyncio.to_thread(root.parent.mkdir,parents=True,exist_ok=True))
        claim_holder=[]
        def created(birth):
            # Bind future writes before the guardian receives its Job handle
            # and can create preparation/receipt files independently.
            claim_holder.append(capacity._admit(protocol.LIMIT*3,owner=birth))
        await owned.prepare_guardian(root,max_seconds=min(86430,max(1,math.ceil(seconds)+30)),on_created=created)
        guard=owned.guardian;prepared=protocol.validate_prepared(guard.request,guard.prepared)
        claim=claim_holder[0]
        bound={'schema':SCHEMA,'case_id':job.case_id,'job_id':job.id,'attempt_id':attempt.id,
            'child_id':child_id,'parent_nonce':parent_nonce,'request':guard.request,'prepared':prepared,
            'node_claim':{'root':str(claim[0]),'path':str(claim[1]),'sha256':hashlib.sha256(claim[2]).hexdigest(),
                'owner':prepared['guardian'],'bytes':protocol.LIMIT*3}}
        # A later inventory reclaims this claim only after the guardian birth
        # exits. The retained files remain charged in that same inventory.
        async with SessionLocal() as db:
            current_job,current_attempt=await job_execution._owned(db,owner)
            await job_execution.attempt_event(db,current_job,current_attempt,PREPARED,bound)
            await db.commit()
        return bound


def validate(job,attempt,value,*,check_directory=True,child_preparations=None,root_preparation=None):
    if (not isinstance(value,dict) or set(value)!={'schema','case_id','job_id','attempt_id','child_id','parent_nonce','request','prepared','node_claim'}
            or value['schema']!=SCHEMA or value['case_id']!=job.case_id or value['job_id']!=job.id or value['attempt_id']!=attempt.id):
        raise ValueError('Guardian witness crosses its attempt')
    request=value['request'];prepared=value['prepared']
    protocol.validate(request,check_code=False,check_directory=check_directory);protocol.validate_prepared(request,prepared)
    worker=job_worker_identity.document(job,attempt)
    if request['owner']!=worker['process']:raise ValueError('Guardian witness changed its worker')
    if root_preparation is not None:
        from .job_root_preparation import process
        parent=process(job,attempt,root_preparation)
    else:parent=process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
    if value['child_id'] is None:
        if value['parent_nonce'] is not None:raise ValueError('Root guardian has a parent')
        selected=parent
    else:
        from .native_child_records import document
        children=document(job,attempt)['children'];child=children[value['child_id']]
        if value['parent_nonce']!=parent['launch_nonce'] or child['parent_launch_nonce']!=parent['launch_nonce']:
            raise ValueError('Child guardian changed its parent launch')
        selected=child['identity']
        if selected is None and child_preparations is not None:
            from .child_launch_registration import prepared_identity
            selected=prepared_identity(job,attempt,child,child_preparations[child['id']])
    if selected is None or selected['launch_nonce']!=request['nonce'] or selected['scope']!=prepared['job']:
        raise ValueError('Guardian does not identify the registered scope')
    claim=value['node_claim']
    if (not isinstance(claim,dict) or set(claim)!={'root','path','sha256','owner','bytes'}
            or claim['owner']!=prepared['guardian'] or claim['bytes']!=protocol.LIMIT*3
            or PureWindowsPath(request['root']['path'])!=PureWindowsPath(claim['root'])/DIRECTORY/request['nonce']
            or not PureWindowsPath(claim['path']).is_relative_to(PureWindowsPath(claim['root']))):
        raise ValueError('Guardian capacity no longer identifies its node')
    return selected


def observe(job,attempt,bound,child_preparations=None,root_preparation=None):
    selected=validate(job,attempt,bound,child_preparations=child_preparations,root_preparation=root_preparation);request=bound['request'];prepared=bound['prepared']
    owner=identity.probe(request['owner'])
    if owner!={'state':'exited','reason':'process_exited'}:return None
    receipt=protocol.read(request,'receipt.json');protocol.validate_receipt(request,prepared,receipt)
    guardian=identity.probe(prepared['guardian'])
    host=identity.probe(selected['host']) if selected['host'] is not None else None
    if guardian!={'state':'exited','reason':'process_exited'} or selected['host'] is not None and host!={'state':'exited','reason':'process_exited'}:
        return None
    result={'schema':SCHEMA,'case_id':job.case_id,'job_id':job.id,'attempt_id':attempt.id,
        'prepared_sha256':hashlib.sha256(json_text(bound).encode()).hexdigest(),
        'child_id':bound['child_id'],'launch_nonce':request['nonce'],'worker_exit':owner,
        'guardian_exit':guardian,'host_exit':host,'host_identity':selected['host'],'guardian_receipt':receipt,
        'host_scope_verified':True,'native_tree_verified':False,'drain_certificate_created':False}
    if root_preparation is not None:
        from .job_root_preparation import history_digest
        result['root_preparation_sha256']=history_digest(root_preparation)
    if child_preparations is not None and bound['child_id'] in child_preparations:
        result['child_preparation_sha256']=hashlib.sha256(json_text(child_preparations[bound['child_id']]).encode()).hexdigest()
    return result


async def reconcile(job_id):
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        if job is None:return []
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
        if attempt is None:return []
        try:owner=job_worker_identity.document(job,attempt)
        except (ValueError,KeyError,TypeError):return []
        if identity.probe(owner['process'])!={'state':'exited','reason':'process_exited'}:return []
        events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==PREPARED).order_by(DurableEvent.seq)))
        before=(attempt.process_identity_json,attempt.child_processes_json,attempt.worker_identity_json)
        values=[json.loads(e.payload_json) for e in events]
        from .child_launch_registration import prepared_histories
        try:preparations=await prepared_histories(db,job,attempt)
        except (ValueError,KeyError,TypeError,AttributeError):preparations=None
        from .job_root_preparation import read as read_root
        try:root_preparation=await read_root(db,job,attempt)
        except (ValueError,KeyError,TypeError,AttributeError):root_preparation=None
    observations=[]
    for bound in values:
        try:value=await finish_before_cancel(asyncio.to_thread(observe,job,attempt,bound,preparations,root_preparation))
        except (OSError,ValueError,KeyError,TypeError):continue
        if value is not None:observations.append(value)
    if not observations:return []
    async with SessionLocal() as db:
        from .durable_events import lock_case_execution
        await lock_case_execution(db,job.case_id)
        current=await db.get(JobAttempt,attempt.id,populate_existing=True)
        if current is None or (current.process_identity_json,current.child_processes_json,current.worker_identity_json)!=before:return []
        if preparations is not None:
            if await prepared_histories(db,job,current)!=preparations:return []
        if root_preparation is not None and await read_root(db,job,current)!=root_preparation:return []
        prior=list(await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.attempt_id==current.id,DurableEvent.type==OBSERVED)))
        for value in observations:
            if json_text(value) not in prior:await job_execution.attempt_event(db,job,current,OBSERVED,value)
        await db.commit()
    return observations
