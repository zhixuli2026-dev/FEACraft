"""Job child startup evidence, with explicit preparation and aborted registration."""
import hashlib,json
from sqlalchemy import select

from ..db import SessionLocal
from ..models import DurableEvent
from . import job_launch_registration as launches,process_preparation,process_record,owned_process
from .durable_events import json_text
from .workflow_state import ExecutionConflict

INTENT='attempt.child_launch_intended'
OBSERVED='attempt.child_launch_observed'
PREPARED='attempt.child_launch_prepared'
ABORTED='attempt.child_launch_aborted'
KINDS={INTENT,OBSERVED,PREPARED,ABORTED}
SCHEMA='vibeflow.child-launch-intent.v1'


async def events(db,job,attempt,identifier):
    rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type.in_(KINDS)).order_by(DurableEvent.seq)))
    result=[]
    for row in rows:
        value=json.loads(row.payload_json)
        if row.case_id!=job.case_id or not isinstance(value,dict) or not isinstance(value.get('child_id'),str):
            raise ValueError('Invalid child launch history ownership')
        if value['child_id']==identifier:result.append((row.type,value))
    return result


def evidence(job,attempt,child,entries):
    if not entries:return None,[],None,None
    values={kind:[] for kind in KINDS}
    for kind,payload in entries:values[kind].append(payload)
    if len(values[INTENT])!=1 or len(values[PREPARED])>1 or len(values[ABORTED])>1:
        raise ValueError('Missing or duplicated child launch evidence')
    intent=values[INTENT][0]
    expected={**launches.binding(job,attempt),'schema':SCHEMA,'child_id':child['id'],
        'parent_launch_nonce':child['parent_launch_nonce'],'request_sha256':child['request_sha256'],
        'purpose':child['purpose'],'caller':child['caller']}
    if set(intent)!=set(expected)|{'launch_nonce','runtime','distribution','gate_sha256','identity_probe_sha256'} or any(intent[k]!=v for k,v in expected.items()):
        raise ValueError('Child launch intent differs from its reservation')
    import re
    for key in ('launch_nonce','gate_sha256','identity_probe_sha256'):
        if not isinstance(intent[key],str) or not re.fullmatch('[0-9a-f]{64}',intent[key]):raise ValueError('Invalid child launch binding')
    from .native_child_records import WSL_PURPOSES
    native=child['purpose'] in WSL_PURPOSES
    if intent['runtime']!=('wsl' if native else 'windows') or (not native and intent['distribution'] is not None):
        raise ValueError('Child launch profile changed')
    if native and (not isinstance(intent['distribution'],str) or not intent['distribution'] or len(intent['distribution'])>128):
        raise ValueError('Missing child native distribution')
    observations=[]
    for item in values[OBSERVED]:
        if set(item)!={'child_id','preparation'} or item['child_id']!=child['id']:raise ValueError('Invalid child preparation link')
        observations.append(item['preparation'])
    last=process_preparation.chain(observations,intent)
    prepared=values[PREPARED][0] if values[PREPARED] else None
    aborted=values[ABORTED][0] if values[ABORTED] else None
    expected_order=[INTENT]+[OBSERVED]*len(observations)+([PREPARED] if prepared else [])+([ABORTED] if aborted else [])
    if [kind for kind,_ in entries]!=expected_order:raise ValueError('Child launch evidence is reordered')
    for item,fields in ((prepared,{'child_id','identity'}),(aborted,{'child_id','identity','stop'})):
        if item is None:continue
        if set(item)!=fields or item['child_id']!=child['id']:raise ValueError('Invalid child launch completion evidence')
        match(intent,item['identity'],job,attempt)
        if observations and item['identity']['host']!=observations[0]['host']:raise ValueError('Child cleanup changed the created host')
    if prepared is not None:
        if last is None:raise ValueError('Prepared child lacks observed startup evidence')
        process_preparation.matches_record(last,prepared['identity'])
    return intent,observations,prepared,aborted


def match(intent,record,job,attempt):
    process_record.for_attempt(record,job,attempt)
    if any(intent[key]!=record[key] for key in ('launch_nonce','runtime','gate_sha256','identity_probe_sha256')):
        raise ValueError('Child identity differs from its intended launch')
    if (record['native']['distribution'] if record['native'] else None)!=intent['distribution']:
        raise ValueError('Child native distribution differs from its intended launch')


async def reserve(owner,parent_nonce,identifier,process,runtime,distribution):
    from .native_child_records import _current,document
    from .job_execution import attempt_event
    async with SessionLocal() as db:
        job,attempt=await _current(db,owner,parent_nonce,launch=True)
        child=document(job,attempt)['children'][identifier]
        if child['phase']!='preparing' or await events(db,job,attempt,identifier):
            raise ExecutionConflict('recovery_required','Child already has an intended native launch')
        value={**launches.binding(job,attempt),'schema':SCHEMA,'child_id':identifier,'parent_launch_nonce':parent_nonce,
            'request_sha256':child['request_sha256'],'purpose':child['purpose'],'caller':child['caller'],
            'launch_nonce':process.nonce,'runtime':runtime,'distribution':distribution,
            'gate_sha256':hashlib.sha256(owned_process.GATE.read_bytes()).hexdigest(),
            'identity_probe_sha256':hashlib.sha256(owned_process.PROBE.read_bytes()).hexdigest()}
        evidence(job,attempt,child,[(INTENT,value)])
        await attempt_event(db,job,attempt,INTENT,value);await db.commit()


async def observed(owner,parent_nonce,identifier,value):
    from .native_child_records import _current,document
    from .job_execution import attempt_event
    async with SessionLocal() as db:
        job,attempt=await _current(db,owner,parent_nonce)
        child=document(job,attempt)['children'][identifier]
        if child['phase']!='preparing':raise ExecutionConflict('state_conflict','Child no longer awaits preparation')
        previous=await events(db,job,attempt,identifier);payload={'child_id':identifier,'preparation':value}
        if previous and previous[-1]==(OBSERVED,payload):return
        evidence(job,attempt,child,previous+[(OBSERVED,payload)])
        await attempt_event(db,job,attempt,OBSERVED,payload);await db.commit()


async def prepared(owner,parent_nonce,identifier,process):
    from .native_child_records import _current,document
    from .job_execution import attempt_event,WORKER_ID
    await process.verify_registration()
    async with SessionLocal() as db:
        job,attempt=await _current(db,owner,parent_nonce,launch=True)
        child=document(job,attempt)['children'][identifier]
        if child['phase']!='preparing':raise ExecutionConflict('state_conflict','Child no longer awaits registration')
        payload={'child_id':identifier,'identity':process_record.bind(process.record,job,attempt,WORKER_ID)}
        previous=await events(db,job,attempt,identifier)
        if previous and previous[-1]==(PREPARED,payload):return
        evidence(job,attempt,child,previous+[(PREPARED,payload)])
        await attempt_event(db,job,attempt,PREPARED,payload);await db.commit()


async def require_prepared(db,job,attempt,child,record):
    intent,observations,prepared,aborted=evidence(job,attempt,child,await events(db,job,attempt,child['id']))
    if intent is None:return
    if aborted is not None or prepared!={'child_id':child['id'],'identity':record}:
        raise ValueError('Child registration differs from its observed preparation')


async def aborted_locked(db,job,attempt,child,record,stop):
    from .job_execution import attempt_event
    entries=await events(db,job,attempt,child['id'])
    if not entries or child['phase']!='preparing' or record is None:return
    payload={'child_id':child['id'],'identity':record,'stop':stop}
    evidence(job,attempt,child,entries+[(ABORTED,payload)])
    await attempt_event(db,job,attempt,ABORTED,payload)


def verify_history(job,attempt,child,entries):
    """Require the complete original event chain, including failed registration."""
    extended=[item for item in entries if item[0] in KINDS]
    intent,observations,prepared,aborted=evidence(job,attempt,child,extended)
    from .child_handoff_recovery import reservation
    if not entries or entries[0][0]!='attempt.child_reserved':raise ValueError('Missing child reservation')
    expected=[('attempt.child_reserved',reservation(child,entries[0][1]))]
    if intent is None:
        expected.append(('attempt.child_running',{'child_id':child['id'],'identity':child['identity']}))
    else:
        expected.append((INTENT,intent))
        expected.extend((OBSERVED,{'child_id':child['id'],'preparation':value}) for value in observations)
        if prepared is not None:expected.append((PREPARED,prepared))
        if aborted is None:
            if prepared is None:raise ValueError('Child running history lacks prepared identity')
            expected.append(('attempt.child_running',{'child_id':child['id'],'identity':child['identity']}))
        else:
            if child['phase']!='failed' or aborted['identity']!=child['identity'] or aborted['stop']!=child['stop']:
                raise ValueError('Aborted child does not match its actual failed cleanup')
            expected.append((ABORTED,aborted))
    expected.append(('attempt.child_stopped',{'child_id':child['id'],'phase':child['phase'],'stop':child['stop'],'error':child['error']}))
    if entries!=expected:raise ValueError('Child cleanup history is incomplete or changed')


def incomplete_children(job,attempt):
    from .native_child_records import document
    children=document(job,attempt)['children']
    # An already registered child with uncertain cleanup belongs to drain
    # recovery. Do not relabel that history as missing startup registration.
    return [child for child in children.values() if child['phase']=='preparing' or (
        child['phase']=='failed' and child['identity'] is None)]


def prepared_identity(job,attempt,child,entries):
    """Read complete original child preparation without inventing registration.

    A WSL identity alone proves no Linux tree closure. The separate native
    admission and retirement chain remains mandatory in the scope certificate.
    """
    if child['phase']!='preparing' or child['identity'] is not None or child['stop'] is not None:
        raise ValueError('Child is not awaiting its original running registration')
    from . import job_native_scopes as native
    admission=None
    if entries and entries[-1][0]==native.ADMITTED:
        admission=entries[-1][1];entries=entries[:-1]
    if not entries or entries[0][0]!='attempt.child_reserved' or any(item[0] not in KINDS for item in entries[1:]):
        raise ValueError('Prepared child has missing reservation or later running history')
    from .child_handoff_recovery import reservation
    reservation(child,entries[0][1])
    intent,observations,prepared,aborted=evidence(job,attempt,child,entries[1:])
    if intent is None or aborted is not None:
        raise ValueError('Prepared child lacks a complete original launch')
    if admission is not None:
        if prepared is not None or not isinstance(admission,dict) or set(admission)!={'binding','value'}:
            raise ValueError('Native admission cannot replace an existing child preparation')
        bound=admission['binding'];value=admission['value']
        if (bound!=native.binding(job,attempt,child['id'],child['parent_launch_nonce'],intent['distribution'],bound['seed'])
                or not isinstance(value,dict) or set(value)!={'identity','observation'}):
            raise ValueError('Native admission changed its child reservation')
        record=value['identity'];match(intent,record,job,attempt)
        if not observations:raise ValueError('Native admission lacks original preparation observations')
        process_preparation.matches_record(observations[-1],record)
        if record['runtime']!='wsl' or record['native'] is None:
            raise ValueError('Only a native child can use its original kernel admission identity')
    elif prepared is not None:record=prepared['identity']
    else:
        latest=observations[-1] if observations else None
        from .job_root_preparation import SCOPE_VIEW
        if latest is None or latest['stage']=='created' or intent['runtime']=='wsl' and latest['stage']=='host_ready':
            return {'schema':SCOPE_VIEW,'launch_nonce':intent['launch_nonce'],'runtime':intent['runtime'],
                'host':latest['host'] if latest else None,'native':None,
                'scope':'Local\\VibeFlow-'+intent['launch_nonce'],
                'original_stage':latest['stage'] if latest else 'unobserved'}
        if latest['stage']!=('host_ready' if intent['runtime']=='windows' else 'native_ready'):
            raise ValueError('Child lacks its original waiting gate observation')
        record=process_record.bind({'schema':process_record.SCHEMA,'pid':latest['host']['pid'],
            **{key:latest[key] for key in process_record.BASE-{'schema','pid'}}},job,attempt,attempt.worker_id)
        match(intent,record,job,attempt)
        process_preparation.matches_record(latest,record)
    if record['runtime'] not in {'windows','wsl'} or record['host']['platform']!='windows':
        raise ValueError('Prepared child requires its original Windows host scope')
    return record


def validate_preparations(job,attempt,histories,*,unlaunched=()):
    children={c['id']:c for c in incomplete_children(job,attempt)}
    if not set(unlaunched).issubset(children) or not isinstance(histories,dict) or set(histories)!=set(children)-set(unlaunched):
        raise ValueError('Prepared child inventory is incomplete')
    return {key:prepared_identity(job,attempt,children[key],entries) for key,entries in histories.items()}


async def prepared_histories(db,job,attempt):
    children=incomplete_children(job,attempt)
    from .child_handoff_recovery import histories as unlaunched_histories
    unlaunched=await unlaunched_histories(db,job,attempt)
    result={c['id']:[] for c in children if c['id'] not in unlaunched}
    if not result:return result
    from .job_native_scopes import ADMITTED
    kinds=KINDS|{ADMITTED,'attempt.child_reserved','attempt.child_running','attempt.child_stopped'}
    rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type.in_(kinds)).order_by(DurableEvent.seq)))
    for row in rows:
        value=json.loads(row.payload_json)
        if row.case_id!=job.case_id or not isinstance(value,dict):
            raise ValueError('Prepared child history crosses its attempt')
        identifier=value.get('binding',{}).get('child_id') if row.type==ADMITTED else value.get('child_id')
        if row.type==ADMITTED and identifier is None:continue
        if not isinstance(identifier,str):raise ValueError('Prepared child history lacks its child identity')
        if identifier in result:result[identifier].append([row.type,value])
    for key,entries in result.items():
        # Preserve the earlier complete-prepared certificate format. Admission
        # is retained here only when it is the last committed identity source.
        if any(kind==PREPARED for kind,_ in entries):result[key]=[item for item in entries if item[0]!=ADMITTED]
    validate_preparations(job,attempt,result,unlaunched=unlaunched)
    return result


async def pending(db,job,attempt):
    result=[]
    for child in incomplete_children(job,attempt):
        try:
            intent,observations,prepared,aborted=evidence(job,attempt,child,await events(db,job,attempt,child['id']))
            result.append({'child_id':child['id'],'phase':child['phase'],'reason':'child_registration_incomplete',
                'preparation':observations[-1] if observations else None,
                'identity':prepared['identity'] if prepared else aborted['identity'] if aborted else child['identity']})
        except (ValueError,TypeError,KeyError,AttributeError):
            result.append({'child_id':child['id'],'phase':child['phase'],'reason':'child_launch_history_invalid',
                'preparation':None,'identity':None})
    return result
