"""Close an unreleased root allocation using its original controller receipts."""
import asyncio,hashlib,json,os,re
from datetime import datetime,timedelta
from pathlib import Path
from sqlalchemy import select
from ..db import SessionLocal
from ..models import Case,DurableEvent,Job,JobAttempt
from . import job_native_scopes as native,job_launch_registration as launches,job_scope_guardians as guards
from . import job_execution,job_worker_identity,process_identity_native as identity,native_scope_control as control
from .durable_events import json_text,lock_case_execution,now

CONTRACT='vibeflow.job-allocation-launch-contract.v1'
CERTIFICATE='vibeflow.job-allocation-recovery.v1'
LAUNCH_CONTRACT='vibeflow.job-launch-preparation-contract.v1'
LAUNCH_CERTIFICATE='vibeflow.job-launch-preparation-recovery.v1'
FILES=set(native.protocol.FILES)|{'jobrunner.py','job_native_scopes.py','job_allocation_recovery.py',
    'native_scope_control.py','owned_process.py','process_gate.py','job_launch_registration.py',
    'async_ops.py','windows_process_scope.py'}
LAUNCH_FILES=FILES|set(guards.protocol.FILES)|{'job_scope_guardians.py','windows_scope_guard.py'}
EXIT={'state':'exited','reason':'process_exited'}
LAUNCH_KINDS={launches.INTENT,launches.OBSERVED,launches.PREPARED,launches.ABORTED,guards.PREPARED,guards.OBSERVED}


def contract():
    return {'schema':CONTRACT,'implementation':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in sorted(FILES)},
        'business_release':'after_original_launch_registration'}


def validate_contract(value):
    if (not isinstance(value,dict) or set(value)!={'schema','implementation','business_release'}
            or value['schema']!=CONTRACT or value['business_release']!='after_original_launch_registration'
            or not isinstance(value['implementation'],dict)
            or set(value['implementation']) not in (FILES,FILES-{'native_compute_v1.py'})
            or any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None for v in value['implementation'].values())):
        raise ValueError('Root allocation lacks its original pre-release contract')


def launch_contract():
    return {'schema':LAUNCH_CONTRACT,'implementation':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in sorted(LAUNCH_FILES)},
        'business_release':'after_original_guardian_and_launch_registration'}


def validate_launch_contract(value):
    if (not isinstance(value,dict) or set(value)!={'schema','implementation','business_release'}
            or value['schema']!=LAUNCH_CONTRACT or value['business_release']!='after_original_guardian_and_launch_registration'
            or not isinstance(value['implementation'],dict)
            or set(value['implementation']) not in (LAUNCH_FILES,LAUNCH_FILES-{'native_compute_v1.py'})
            or any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None for v in value['implementation'].values())):
        raise ValueError('Root launch lacks its original guardian handoff contract')


def launch_state(job,attempt,native_history,launch_history):
    if len(launch_history)!=1 or launch_history[0][0]!=launches.INTENT:
        raise ValueError('Only an original intent before guardian registration may use preparation recovery')
    intended=launches.validate_intent(launch_history[0][1],job,attempt)
    selected=intended['preparation_contract'];validate_launch_contract(selected)
    if (intended['gate_sha256']!=selected['implementation']['process_gate.py']
            or intended['identity_probe_sha256']!=selected['implementation']['process_identity_native.py']):
        raise ValueError('Launch helpers differ from the original preparation contract')
    if intended['runtime']=='windows':
        from .step_handlers import validate_binding
        if native_history or validate_binding(job,attempt) is None and not guards.sealed_windows_shell(job,attempt):
            raise ValueError('Windows preparation lacks a sealed private execution')
        return None
    state=allocation(job,attempt,native_history)
    if native.ALLOCATED not in state['stages'] or intended['launch_nonce']!=state['stages'][native.ALLOCATED]['result']['nonce']:
        raise ValueError('Root launch changed its original allocated scope')
    return state


def allocation(job,attempt,history,*,child_id=None):
    scopes=native.validate_events(job,attempt,history)
    if set(scopes)!={child_id}:raise ValueError('Allocation recovery requires exactly one original scope')
    state=scopes[child_id];stages=state['stages']
    if child_id is None:validate_contract(stages[native.INTENT])
    if native.ADMITTED in stages:raise ValueError('Admitted root needs full execution scope recovery')
    return state


def births(state):
    if state is None:return []
    prepared=state['stages'].get(native.PREPARED)
    if prepared is None:return []
    return [{'role':'allocation_host','distribution':None,'birth':prepared['host']['host']},
        {'role':'allocation_creator','distribution':state['binding']['distribution'],'birth':prepared['native']['controller']}]


def validate_closure(job,attempt,history,value,*,child_id=None):
    from .job_scope_recovery import digest
    state=allocation(job,attempt,history,child_id=child_id);stages=state['stages']
    if (not isinstance(value,dict) or set(value)!={'history_sha256','retirement','known_birth_exits',
            'unobserved_bootstraps','business_not_released','unknown_process_exit_inferred'}
            or value['history_sha256']!=digest(history) or value['business_not_released'] is not True
            or value['unknown_process_exit_inferred'] is not False
            or value['known_birth_exits']!=[dict(item,exit=EXIT) for item in births(state)]
            or value['unobserved_bootstraps']!=([] if native.PREPARED in stages else ['allocation_host','allocation_creator'])):
        raise ValueError('Allocation closure changed its original known or unknown births')
    receipt=value['retirement']
    if native.PREPARED not in stages:
        if receipt is not None:raise ValueError('Unpermitted allocation has a fabricated retirement')
        return
    control.validate_receipt(receipt)
    operation='retire' if native.ALLOCATED in stages else 'abandon'
    arguments={'scope':stages[native.ALLOCATED]['result']} if native.ALLOCATED in stages else {
        'allocation_request':stages[native.PREPARED]['request'],'preparation':stages[native.PREPARED]['native']}
    if (receipt['request']['operation']!=operation or receipt['request']['distribution']!=state['binding']['distribution']
            or receipt['request']['arguments']!=arguments or native.RETIRED in stages and receipt!=stages[native.RETIRED]):
        raise ValueError('Allocation closure changed its original retirement selection')


def validate_launch_closure(job,attempt,native_history,launch_history,value):
    from .job_scope_recovery import digest
    state=launch_state(job,attempt,native_history,launch_history)
    if (not isinstance(value,dict) or set(value)!={'history_sha256','retirement','known_birth_exits',
            'unobserved_bootstraps','business_not_released','unknown_process_exit_inferred'}
            or value['history_sha256']!=digest([native_history,launch_history])
            or value['unobserved_bootstraps']!=['windows_guardian'] or value['business_not_released'] is not True
            or value['unknown_process_exit_inferred'] is not False):
        raise ValueError('Launch preparation closure changed its original unknown guardian')
    if state is None:
        if value['retirement'] is not None or value['known_birth_exits']!=[]:
            raise ValueError('Windows preparation invents a native allocation or exit')
    else:
        validate_closure(job,attempt,native_history,{**value,'history_sha256':digest(native_history),'unobserved_bootstraps':[]})


async def history(db,job,attempt):
    rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type.in_(native.KINDS|LAUNCH_KINDS)).order_by(DurableEvent.seq)))
    if any(row.case_id!=job.case_id or row.type in LAUNCH_KINDS-{launches.INTENT} for row in rows):
        raise ValueError('Root has crossed preparation into a registered guardian or execution')
    result=[[row.type,json.loads(row.payload_json)] for row in rows if row.type in native.KINDS]
    launch_history=[[row.type,json.loads(row.payload_json)] for row in rows if row.type==launches.INTENT]
    if launch_history:launch_state(job,attempt,result,launch_history)
    else:allocation(job,attempt,result)
    return result,launch_history


def validate_certificate(case,job,attempt,value):
    from .job_scope_recovery import FIELDS,SCHEMA,digest,snapshot
    from .job_write_checkpoint import claim_digest
    from .attempt_workspace import require_sealed
    require_sealed(job,attempt)
    if (not isinstance(value,dict) or set(value)!=FIELDS or value['schema']!=SCHEMA
            or type(value['case_id']) is not int or type(value['job_id']) is not int
            or value['case_id']!=case.id or value['job_id']!=job.id or value['attempt_id']!=attempt.id
            or job.case_id!=case.id or attempt.job_id!=job.id or value['case_prefix']!=case.s3_prefix
            or value['claim_sha256']!=claim_digest(attempt) or value['state']!='recovered_stopped' or value['stop'] is not None
            or value['process_identity_json']!=attempt.process_identity_json or value['child_processes_json']!=attempt.child_processes_json
            or json.loads(attempt.process_identity_json)!={} or json.loads(attempt.child_processes_json)!={}
            or json.loads(attempt.output_manifest_json).get('process_stop') is not None):
        raise ValueError('Allocation recovery changed original missing execution facts')
    if datetime.fromisoformat(value['recorded_at']).utcoffset()!=timedelta(0):raise ValueError('Recovery requires UTC')
    proof=value['recovery'];fields={'schema','snapshot_sha256','worker','worker_exit','observer','native_history',
        'closure','original_phase','cancel_requested_at','launch_history','original_output_manifest_json'}
    if not isinstance(proof,dict) or set(proof)!=fields or proof['schema'] not in {CERTIFICATE,LAUNCH_CERTIFICATE}:
        raise ValueError('Invalid allocation recovery certificate')
    launching=proof['schema']==LAUNCH_CERTIFICATE
    if not launching:native.root_policy(job,attempt)
    cancelled=proof['original_phase']=='cancelling'
    stamp=attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None
    if (proof['original_phase'] not in {'starting','cancelling'} or cancelled!=(stamp is not None)
            or proof['cancel_requested_at']!=stamp or not launching and proof['launch_history']!=[] or json.loads(proof['original_output_manifest_json'])!={}
            or proof['snapshot_sha256']!=digest(snapshot(case,job,attempt))
            or proof['worker']!=job_worker_identity.document(job,attempt) or proof['worker_exit']!=EXIT
            or job.status!=attempt.phase or attempt.phase!=('cancelled' if cancelled else 'interrupted')
            or attempt.reason_code!=('cancel_requested' if cancelled else 'recovery_required')):
        raise ValueError('Allocation recovery changed its original owner or cancellation')
    observer=identity.validate(proof['observer']);worker=proof['worker']['process']
    if observer['platform']!='windows' or observer['host_id']!=worker['host_id'] or observer==worker:
        raise ValueError('Allocation recovery lacks an independent same-host observer')
    if launching:validate_launch_closure(job,attempt,proof['native_history'],proof['launch_history'],proof['closure'])
    else:validate_closure(job,attempt,proof['native_history'],proof['closure'])
    return value


async def reconcile(job_id):
    if os.name!='nt':return False
    from .job_scope_recovery import digest,snapshot,SCHEMA,EVENT
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        if job is None:return False
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
        if attempt is None:return False
        if attempt.write_checkpoint_json!='{}':return False
        if attempt.phase not in {'starting','cancelling'}:return False
        if any(json.loads(v)!={} for v in (attempt.process_identity_json,attempt.child_processes_json,attempt.output_manifest_json)):return False
        try:
            from .attempt_workspace import require_sealed
            require_sealed(job,attempt)
            worker=job_worker_identity.document(job,attempt)
            original,launch_history=await history(db,job,attempt);launching=bool(launch_history)
            state=launch_state(job,attempt,original,launch_history) if launching else allocation(job,attempt,original)
            if state is not None and state['stages'][native.INTENT]!=contract():return False
            if launching and launch_history[0][1]['preparation_contract']!=launch_contract():return False
        except (ValueError,KeyError,TypeError,job_execution.ExecutionConflict):return False
        case=await db.get(Case,job.case_id);before=snapshot(case,job,attempt)
        phase=attempt.phase;stamp=attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None
        if (phase=='cancelling')!=(stamp is not None) or job.status!=('cancelled' if stamp else 'queued'):return False
    if await asyncio.to_thread(identity.probe,worker['process'])!=EXIT:return False
    from .owned_process import probe_native
    observations=[]
    for item in births(state):
        observed=await probe_native(item['distribution'],item['birth']) if item['distribution'] else await asyncio.to_thread(identity.probe,item['birth'])
        if observed!=EXIT:return False
        observations.append(dict(item,exit=observed))
    stages=state['stages'] if state is not None else {};retirement=stages.get(native.RETIRED)
    if retirement is None and native.PREPARED in stages:
        operation='retire' if native.ALLOCATED in stages else 'abandon'
        arguments={'scope':stages[native.ALLOCATED]['result']} if native.ALLOCATED in stages else {
            'allocation_request':stages[native.PREPARED]['request'],'preparation':stages[native.PREPARED]['native']}
        retirement=await control.control(state['binding']['distribution'],operation,arguments)
    closure={'history_sha256':digest([original,launch_history] if launching else original),'retirement':retirement,'known_birth_exits':observations,
        'unobserved_bootstraps':['windows_guardian'] if launching else [] if native.PREPARED in stages else ['allocation_host','allocation_creator'],
        'business_not_released':True,'unknown_process_exit_inferred':False}
    if launching:validate_launch_closure(job,attempt,original,launch_history,closure)
    else:validate_closure(job,attempt,original,closure)
    observer=await asyncio.to_thread(identity.observe,os.getpid())
    async with SessionLocal() as db:
        await lock_case_execution(db,job.case_id)
        job=await db.get(Job,job_id,populate_existing=True);attempt=await db.get(JobAttempt,attempt.id,populate_existing=True)
        case=await db.get(Case,job.case_id,populate_existing=True)
        if (attempt is None or snapshot(case,job,attempt)!=before or attempt.phase!=phase or attempt.write_checkpoint_json!='{}'
                or (attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None)!=stamp
                or await history(db,job,attempt)!=(original,launch_history)):return False
        from .workflow_state import transition
        target,reason=('cancelled','cancel_requested') if stamp else ('interrupted','recovery_required')
        await transition(db,'attempt',attempt.id,target,expected_version=attempt.version,claim_token=attempt.claim_token,
            reason_code=reason,detail='Original allocation owner exited before business release; original preparation closed without replay')
        await job_execution._job_status(db,job,target,None,attempt)
        from .artifact_publication import finalize
        await finalize(db,job,attempt,target,reason)
        from .job_write_checkpoint import claim_digest
        certificate=LAUNCH_CERTIFICATE if launching else CERTIFICATE
        proof={'schema':certificate,'snapshot_sha256':digest(snapshot(case,job,attempt)),'worker':worker,'worker_exit':EXIT,
            'observer':observer,'native_history':original,'closure':closure,'original_phase':phase,'cancel_requested_at':stamp,'launch_history':launch_history,
            'original_output_manifest_json':'{}'}
        value={'schema':SCHEMA,'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,'case_prefix':case.s3_prefix,
            'claim_sha256':claim_digest(attempt),'state':'recovered_stopped','process_identity_json':attempt.process_identity_json,
            'child_processes_json':attempt.child_processes_json,'stop':None,'recorded_at':now().isoformat(),'recovery':proof}
        validate_certificate(case,job,attempt,value)
        attempt.write_checkpoint_json=json_text(value)
        await job_execution.attempt_event(db,job,attempt,EVENT,{'schema':certificate,'checkpoint_sha256':digest(value),
            'original_root_stop_missing':True,'business_not_released':True,'replayed':False})
        await db.commit();return True
