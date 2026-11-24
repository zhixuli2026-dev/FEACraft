"""Close original, unreleased child preparations before guardian registration."""
import asyncio,hashlib,json,os,re
from pathlib import Path
from sqlalchemy import select
from ..db import SessionLocal
from ..models import DurableEvent,Job,JobAttempt
from . import job_allocation_recovery as allocation,job_native_scopes as native
from . import child_launch_registration as launches,native_child_records as children
from . import job_scope_guardians as guards,job_worker_identity,job_execution
from . import native_scope_control as control,process_identity_native as identity
from .durable_events import json_text,lock_case_execution

CONTRACT='vibeflow.child-preparation-contract.v1'
EVENT='attempt.child_handoff_recovered'
RESERVED='attempt.child_reserved'
FILES=allocation.LAUNCH_FILES|{'native_execution.py','native_child_records.py',
    'child_launch_registration.py','child_handoff_recovery.py'}
EXIT={'state':'exited','reason':'process_exited'}
KINDS=native.KINDS|launches.KINDS|{RESERVED,'attempt.child_running','attempt.child_stopped',guards.PREPARED,guards.OBSERVED}


def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()


def contract():
    return {'schema':CONTRACT,'implementation':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in sorted(FILES)},
        'business_release':'after_original_child_guardian_and_running_registration'}


def validate_contract(value):
    if (not isinstance(value,dict) or set(value)!={'schema','implementation','business_release'}
            or value['schema']!=CONTRACT or value['business_release']!='after_original_child_guardian_and_running_registration'
            or not isinstance(value['implementation'],dict) or set(value['implementation'])!=FILES
            or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in value['implementation'].values())):
        raise ValueError('Child preparation lacks its original release contract')


def reservation(child,value):
    expected={'child_id':child['id'],'purpose':child['purpose'],'caller':child['caller']}
    if not isinstance(value,dict) or set(value) not in (set(expected),set(expected)|{'preparation_contract'}) or any(value[k]!=v for k,v in expected.items()):
        raise ValueError('Child reservation changed its original owner')
    if 'preparation_contract' in value:validate_contract(value['preparation_contract'])
    return value


def state(job,attempt,child_id,history,*,current=False):
    child=children.document(job,attempt)['children'][child_id]
    if child['phase']!='preparing' or child['identity'] is not None or child['stop'] is not None:
        raise ValueError('Child handoff is no longer an unreleased preparation')
    if not history or history[0][0]!=RESERVED:raise ValueError('Child handoff lacks its original reservation')
    reserved=reservation(child,history[0][1]);selected=reserved['preparation_contract'];validate_contract(selected)
    if current and selected!=contract():raise ValueError('Child implementation changed since its reservation')
    native_history=[];intended=None
    for kind,value in history[1:]:
        if kind in native.KINDS:
            if intended is not None:raise ValueError('Child native allocation follows its launch intent')
            native_history.append([kind,value])
        elif kind==launches.INTENT and intended is None:
            intended,_,_,_=launches.evidence(job,attempt,child,[(kind,value)])
        else:raise ValueError('Child has crossed into guardian or execution registration')
    scope=allocation.allocation(job,attempt,native_history,child_id=child_id) if native_history else None
    is_native=child['purpose'] in children.WSL_PURPOSES
    if not is_native and scope is not None:raise ValueError('Host child has a foreign native allocation')
    if intended is not None:
        if (intended['gate_sha256']!=selected['implementation']['process_gate.py']
                or intended['identity_probe_sha256']!=selected['implementation']['process_identity_native.py']):
            raise ValueError('Child launch changed its original implementation')
        if is_native and (scope is None or native.ALLOCATED not in scope['stages']
                or intended['launch_nonce']!=scope['stages'][native.ALLOCATED]['result']['nonce']
                or intended['distribution']!=scope['binding']['distribution']):
            raise ValueError('Child launch changed its original allocated scope')
    return scope,native_history,intended


async def histories(db,job,attempt):
    if job_worker_identity.document(job,attempt)['process']['platform']!='windows':return {}
    pending={c['id']:[] for c in children.document(job,attempt)['children'].values() if c['phase']=='preparing'}
    if not pending:return {}
    rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type.in_(KINDS)).order_by(DurableEvent.seq)))
    for row in rows:
        value=json.loads(row.payload_json)
        if row.case_id!=job.case_id or not isinstance(value,dict):raise ValueError('Child handoff history crosses its attempt')
        child=value.get('binding',value).get('child_id')
        if child in pending:pending[child].append([row.type,value])
    result={}
    for child,history in pending.items():
        if any(kind==guards.PREPARED for kind,_ in history):continue
        if not history or 'preparation_contract' not in history[0][1]:continue
        state(job,attempt,child,history);result[child]=history
    return result


def validate(job,attempt,value,root_guard):
    if not isinstance(value,dict) or set(value)!={'child_id','history','history_sha256','closure','worker_exit','root_guardian_sha256'}:
        raise ValueError('Invalid child handoff closure')
    scope,native_history,intended=state(job,attempt,value['child_id'],value['history'])
    if value['history_sha256']!=digest(value['history']) or value['worker_exit']!=EXIT or value['root_guardian_sha256']!=digest(root_guard):
        raise ValueError('Child handoff closure changed its original history or parent witness')
    closure=value['closure'];stages=scope['stages'] if scope else {}
    unknown=(['allocation_host','allocation_creator'] if scope and native.PREPARED not in stages else [])+(['windows_guardian'] if intended else [])
    if (not isinstance(closure,dict) or set(closure)!={'retirement','known_birth_exits','unobserved_bootstraps','business_not_released','unknown_process_exit_inferred'}
            or closure['unobserved_bootstraps']!=unknown or closure['business_not_released'] is not True or closure['unknown_process_exit_inferred'] is not False):
        raise ValueError('Child handoff closure invented unknown process exits')
    if scope:
        allocation.validate_closure(job,attempt,native_history,{**closure,'history_sha256':digest(native_history),
            'unobserved_bootstraps':[] if native.PREPARED in stages else ['allocation_host','allocation_creator']},child_id=value['child_id'])
    elif closure['retirement'] is not None or closure['known_birth_exits']!=[]:
        raise ValueError('Unallocated child has fabricated scope retirement')
    return native_history


async def recover(job_id,guardians):
    if os.name!='nt':return []
    root=next((g for g in guardians if g['child_id'] is None and g['host_scope_verified'] and g['worker_exit']==EXIT),None)
    if root is None:return []
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id).order_by(JobAttempt.number.desc()).limit(1))
        if job is None or attempt is None or attempt.write_checkpoint_json!='{}':return []
        worker=job_worker_identity.document(job,attempt)
        if await asyncio.to_thread(identity.probe,worker['process'])!=EXIT:return []
        pending=await histories(db,job,attempt);before=native.snapshot(attempt)
        previous=list(await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==EVENT)))
    from .owned_process import probe_native
    result=[]
    for child,history in pending.items():
        try:scope,_,intended=state(job,attempt,child,history,current=True)
        except (ValueError,KeyError,TypeError):continue
        prior=[json.loads(v) for v in previous if json.loads(v)['child_id']==child]
        if prior:
            if len(prior)!=1 or prior[0]['history']!=history:raise ValueError('Child handoff closure was rebound')
            validate(job,attempt,prior[0],root);result.extend(prior);continue
        observations=[]
        for item in allocation.births(scope):
            exit_value=await probe_native(item['distribution'],item['birth']) if item['distribution'] else await asyncio.to_thread(identity.probe,item['birth'])
            if exit_value!=EXIT:break
            observations.append(dict(item,exit=exit_value))
        if len(observations)!=len(allocation.births(scope)):continue
        stages=scope['stages'] if scope else {};retirement=stages.get(native.RETIRED)
        if retirement is None and native.PREPARED in stages:
            operation='retire' if native.ALLOCATED in stages else 'abandon'
            arguments={'scope':stages[native.ALLOCATED]['result']} if native.ALLOCATED in stages else {
                'allocation_request':stages[native.PREPARED]['request'],'preparation':stages[native.PREPARED]['native']}
            retirement=await control.control(scope['binding']['distribution'],operation,arguments)
        closure={'retirement':retirement,'known_birth_exits':observations,
            'unobserved_bootstraps':(['allocation_host','allocation_creator'] if scope and native.PREPARED not in stages else [])+(['windows_guardian'] if intended else []),
            'business_not_released':True,'unknown_process_exit_inferred':False}
        value={'child_id':child,'history':history,'history_sha256':digest(history),'closure':closure,'worker_exit':EXIT,'root_guardian_sha256':digest(root)}
        validate(job,attempt,value,root)
        async with SessionLocal() as db:
            await lock_case_execution(db,job.case_id)
            fresh=await db.get(JobAttempt,attempt.id,populate_existing=True)
            if fresh is None or fresh.write_checkpoint_json!='{}' or native.snapshot(fresh)!=before or await histories(db,job,fresh)!=pending:continue
            prior=list(await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==EVENT)))
            same=[json.loads(v) for v in prior if json.loads(v)['child_id']==child]
            if same:
                if same!=[value]:raise ValueError('Concurrent child handoff closure differs')
            else:
                await job_execution.attempt_event(db,job,fresh,EVENT,value);await db.commit()
        result.append(value)
    return result
