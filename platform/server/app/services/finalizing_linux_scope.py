"""Durable Linux namespace ownership for independent finalization requests.

Use the existing gated PID namespace. Recovery observes or stops its recorded
births; it never replaces the original worker's or request's missing stop.
"""
import asyncio,copy,hashlib,json,os,re,signal,time,uuid
from datetime import datetime,timedelta,timezone

from ..db import SessionLocal
from ..models import DurableEvent,NativeRequest
from . import native_request_records as records,process_identity_native as identity,process_preparation,process_record
from .async_ops import finish_before_cancel
from .durable_events import append_event,json_text,lock_case_execution,now

SCHEMA='vibeflow.finalizer-linux-scope.v1'
CLOSURE='vibeflow.finalizer-linux-closure.v1'
STAGES=('intent','created','host_ready','namespace','closed')
EXIT={'state':'exited','reason':'process_exited'}
FILES={'finalizing_linux_scope.py','finalizing_native_scope.py','finalizing_native_preparation.py',
 'finalizing_native_usage.py','finalizing_request_recovery.py','finalizing_request_cancellation.py',
 'native_request_execution.py','native_request_records.py','native_execution.py','owned_process.py',
 'process_gate.py','process_identity_native.py','process_preparation.py','process_record.py'}

def applies(row):return row.kind=='job.finalize' and json.loads(row.owner_identity_json).get('platform')=='linux'
def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()
def event_id(child,stage):return str(uuid.uuid5(uuid.UUID(child),SCHEMA+':'+stage))
def code():
 from pathlib import Path
 return {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in sorted(FILES)}
def binding(row,child):
 from .finalizing_native_scope import binding as original
 return dict(original(row,child),schema=SCHEMA)
def unreleased(proof):return proof.get('schema')==CLOSURE and proof.get('business_not_released') is True
def births(value):
 result={}
 if value['created'] is not None:result['host']=value['created']['host']
 if value['namespace'] is not None:result['init']=value['namespace']['init']
 return result

async def read(db,row,child):
 if not applies(row) or records.is_native(row):raise ValueError('Linux finalizer cannot own a foreign runtime')
 owner=identity.validate(json.loads(row.owner_identity_json));bound=binding(row,child);value={}
 for stage in STAGES:
  event=await db.get(DurableEvent,event_id(child['id'],stage),populate_existing=True)
  if event is None:value[stage]=None;continue
  payload=json.loads(event.payload_json)
  if (event.case_id!=row.case_id or event.type!='job.finalizer_linux_'+stage or len(event.payload_json.encode())>128*1024
    or set(payload)!={'binding','value'} or payload['binding']!=bound):raise ValueError('Linux scope history changed its request')
  value[stage]=payload['value']
 intended=value['intent']
 if intended is None:
  if any(v is not None for v in value.values()):raise ValueError('Linux finalizer lacks its original reservation')
  return None
 if (set(intended)!={'seed','implementation','output_usage'} or not isinstance(intended['seed'],str)
   or re.fullmatch('[0-9a-f]{64}',intended['seed']) is None or set(intended['implementation'])!=FILES
   or any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None for v in intended['implementation'].values())):
  raise ValueError('Invalid Linux finalizer launch contract')
 from .execution_output_checkpoint import validate_usage
 validate_usage(intended['output_usage'],json.loads(row.profile_json)['max_log_bytes'])
 intent={'launch_nonce':intended['seed'],'runtime':'windows','gate_sha256':intended['implementation']['process_gate.py'],
  'identity_probe_sha256':intended['implementation']['process_identity_native.py']}
 observed=[]
 for stage in ('created','host_ready'):
  item=value[stage]
  if item is None:continue
  if item['stage']!=stage or len(observed)!=('created','host_ready').index(stage):raise ValueError('Linux preparation stages are missing')
  process_preparation.validate(item)
  if item['native'] is not None or item['host']['platform']!='linux' or any(item['host'][k]!=owner[k] for k in ('host_id','boot_id','uid','pid_namespace')):
   raise ValueError('Linux preparation changed its owner host')
  observed.append(item)
 process_preparation.chain(observed,intent)
 namespace=value['namespace']
 if namespace is not None:
  if set(namespace)!={'process','init'} or value['host_ready'] is None:raise ValueError('Linux namespace has no observed host')
  record=process_record.validate(namespace['process'],bound=False);init=identity.validate(namespace['init'])
  process_preparation.matches_record(value['host_ready'],record)
  if (record.get('namespace_init')!=init or init['platform']!='linux' or init['pid_namespace']==owner['pid_namespace']
    or any(init[k]!=owner[k] for k in ('host_id','boot_id','uid'))):raise ValueError('Linux namespace birth differs from its owner')
  if child['identity'] is not None and child['identity']!=record:raise ValueError('Linux namespace differs from registered request child')
 closed=value['closed']
 if closed is not None:
  fields={'schema','history_sha256','authority','authority_sha256','observer','observed_at','birth_exits','stop','stop_identity','business_not_released','unknown_bootstrap_exit_inferred'}
  if (set(closed)!=fields or closed['schema']!=CLOSURE or closed['history_sha256']!=digest({k:v for k,v in value.items() if k!='closed'})
    or closed['authority'] not in {'normal','owner_exit','cancelled','terminal'} or closed['unknown_bootstrap_exit_inferred'] is not False
    or type(closed['business_not_released']) is not bool):raise ValueError('Invalid Linux finalizer closure')
  if closed['birth_exits']!={k:{'birth':v,'exit':EXIT} for k,v in births(value).items()}:
   raise ValueError('Linux finalizer lacks exact recorded birth exits')
  observer=identity.validate(closed['observer']);stamp=datetime.fromisoformat(closed['observed_at'])
  if observer['platform']!='linux' or any(observer[k]!=owner[k] for k in ('host_id','boot_id','pid_namespace')) or stamp.utcoffset()!=timedelta(0):
   raise ValueError('Linux closure witness is on another host or namespace')
  if stamp>(event.created_at.replace(tzinfo=timezone.utc) if event.created_at.tzinfo is None else event.created_at):raise ValueError('Linux closure timestamp follows its event')
  if closed['authority']=='owner_exit' and (closed['authority_sha256']!=digest({'owner':owner,'exit':EXIT}) or observer==owner):
   raise ValueError('Linux owner exit authority changed')
  if closed['authority']=='cancelled':
   from .finalizing_request_cancellation import require_fence
   fence,_=await require_fence(db,row)
   if closed['authority_sha256']!=digest(fence):raise ValueError('Linux closure lost its cancellation fence')
  if closed['authority']=='terminal':
   from .finalizing_native_preparation import terminal_digest
   if closed['authority_sha256']!=terminal_digest(row):raise ValueError('Linux closure changed its terminal request')
  if closed['authority']=='normal' and closed['authority_sha256'] is not None:raise ValueError('Normal Linux closure invents owner authority')
  if closed['business_not_released']:
   # Only the original gated implementation can register/release business.
   # Missing bootstrap identity is retained as unknown, never called exited.
   if child['identity'] is not None and closed['authority']!='normal':raise ValueError('Linux non-release contradicts business registration')
  elif namespace is None:raise ValueError('Released Linux finalizer has no original namespace')
  if closed['stop'] is not None:
   from .process_stop import validate
   stopped=process_record.validate(closed['stop_identity'],bound=False)
   if namespace is not None and stopped!=namespace['process']:raise ValueError('Linux stop changed its namespace identity')
   if stopped['host']!=births(value).get('host') or stopped['launch_nonce']!=intended['seed']:raise ValueError('Linux stop changed its observed host')
   stop=validate(closed['stop'],stopped)
   if not stop['verified'] or not stop['scope_empty'] or stop['launch_nonce']!=intended['seed']:raise ValueError('Linux normal closure lacks its verified stop')
  elif closed['stop_identity'] is not None or closed['authority']=='normal' and not closed['business_not_released']:raise ValueError('Normal Linux finalizer lost its stop')
 return value

async def reserve(db,row,child,nonce):
 if not isinstance(nonce,str) or re.fullmatch('[0-9a-f]{64}',nonce) is None:raise ValueError('Linux reservation lacks a launch nonce')
 if await read(db,row,child) is not None:raise ValueError('Linux finalizer reservation already exists')
 value={'seed':nonce,'implementation':code(),'output_usage':json.loads(row.result_json)['usage']}
 await append_event(db,row.case_id,'job.finalizer_linux_intent',{'binding':binding(row,child),'value':value},event_id=event_id(child['id'],'intent'))

async def write(owner,child_id,stage,value,*,opening=False):
 from .case_validation import admit_session_locked
 async with SessionLocal() as db:
  if opening:await admit_session_locked(db,NativeRequest,owner.id)
  else:
   selected=await db.get(NativeRequest,owner.id)
   if selected is None:raise ValueError('Linux finalizer owner disappeared')
   await lock_case_execution(db,selected.case_id)
  row=await records._current(db,owner,owner.nonce,launch=opening);child=records.document(row)['children'][child_id]
  history=await read(db,row,child)
  if history is None or history['closed'] is not None:raise ValueError('Linux launch is missing or already closed')
  if opening:
   from .job_output_finalizing import require_native_release
   await require_native_release(db,row)
   if history['intent']['implementation']!=code():raise ValueError('Linux launch implementation changed after reservation')
  if await db.get(DurableEvent,event_id(child_id,stage)) is not None:raise ValueError('Linux scope stage already exists')
  await append_event(db,row.case_id,'job.finalizer_linux_'+stage,{'binding':binding(row,child),'value':value},event_id=event_id(child_id,stage))
  await db.flush();await read(db,row,child);await finish_before_cancel(db.commit())

async def prepare(executor,entry):
 if os.name=='nt' or executor.runtime!='windows':raise ValueError('Linux finalization requires native Linux execution')
 def active():
  executor.execution_budget.check();entry.check()
  if entry.event.is_set() or not executor._allowed(entry.caller):raise ValueError('Linux finalizer stopped during preparation')
 active()
 async def observed(value):
  await write(executor.owner,entry.id,value['stage'],value,opening=True);active()
 async def namespace(record,init):
  await write(executor.owner,entry.id,'namespace',{'process':record,'init':init},opening=True);active()
 entry.owned.on_preparation=observed;entry.owned.on_namespace=namespace

async def require_ready(db,row,child,record):
 value=await read(db,row,child)
 if value is None or value['closed'] is not None or value['namespace'] is None or value['namespace']['process']!=record:
  raise ValueError('Linux finalizer lacks its open, recorded namespace')

def retire_birth(birth,stop):
 observed=identity.probe(birth)
 if observed==EXIT:return EXIT
 if observed['state']!='match':raise ValueError('Linux birth exit is not verifiable')
 if stop:
  descriptor=os.pidfd_open(birth['pid'])
  try:
   if identity.observe(birth['pid'])!=birth:raise ValueError('Linux birth changed before scoped stop')
   signal.pidfd_send_signal(descriptor,signal.SIGKILL)
  finally:os.close(descriptor)
 deadline=time.monotonic()+10
 while time.monotonic()<deadline:
  observed=identity.probe(birth)
  if observed==EXIT:return EXIT
  if observed['state']!='match':raise ValueError('Linux birth changed during scoped stop')
  time.sleep(.05)
 raise ValueError('Linux finalizer birth has not exited')

async def proof(row,child,value,authority,authority_sha256,stop=None,stop_identity=None):
 observed={}
 # Terminating the recorded PID-namespace init terminates all of its children.
 for role in ('init','host'):
  birth=births(value).get(role)
  if birth is not None:observed[role]={'birth':birth,'exit':await finish_before_cancel(asyncio.to_thread(retire_birth,birth,authority!='normal'))}
 return {'schema':CLOSURE,'history_sha256':digest({k:v for k,v in value.items() if k!='closed'}),
  'authority':authority,'authority_sha256':authority_sha256,'observer':await asyncio.to_thread(identity.observe,os.getpid()),
  'observed_at':now().isoformat(),'birth_exits':observed,'stop':copy.deepcopy(stop),'stop_identity':copy.deepcopy(stop_identity) if stop is not None else None,
  'business_not_released':child['identity'] is None,'unknown_bootstrap_exit_inferred':False}

async def close(owner,child_id,*,process):
 async with SessionLocal() as db:
  row=await records._current(db,owner,owner.nonce);child=records.document(row)['children'][child_id];value=await read(db,row,child)
 if value is None:raise ValueError('Linux finalizer has no original launch contract')
 if value['closed'] is not None:return
 await write(owner,child_id,'closed',await proof(row,child,value,'normal',None,process.stop_result,process.record))

async def recover(row,*,cancelled=False,terminal=False):
 from .finalizing_native_scope import fingerprint
 from .case_validation import assert_writable_locked
 if cancelled and terminal:raise ValueError('Linux finalizer has conflicting retirement authorities')
 before=fingerprint(row);owner=json.loads(row.owner_identity_json)
 if cancelled:
  from .finalizing_request_cancellation import require_fence
  async with SessionLocal() as db:fence,_=await require_fence(db,row)
  authority,stamp='cancelled',digest(fence)
 elif terminal:
  from .finalizing_native_preparation import terminal_digest
  authority,stamp='terminal',terminal_digest(row)
 else:
  if await asyncio.to_thread(identity.probe,owner)!=EXIT:raise ValueError('Original Linux validator owner has not exited')
  authority,stamp='owner_exit',digest({'owner':owner,'exit':EXIT})
 result={}
 for key,child in records.document(row)['children'].items():
  async with SessionLocal() as db:value=await read(db,row,child)
  if value is None:raise ValueError('Historical Linux validator has no namespace launch contract')
  if value['closed'] is not None:result[key]=value['closed'];continue
  closed=await proof(row,child,value,authority,stamp)
  async with SessionLocal() as db:
   await lock_case_execution(db,row.case_id);await assert_writable_locked(db,row.case_id)
   current=await db.get(NativeRequest,row.id,populate_existing=True)
   if current is None or fingerprint(current)!=before:raise ValueError('Linux request changed during retirement')
   fresh=await read(db,current,child)
   if fresh['closed'] is not None:closed=fresh['closed']
   else:
    if fresh!=value:raise ValueError('Linux namespace changed during retirement')
    await append_event(db,row.case_id,'job.finalizer_linux_closed',{'binding':binding(current,child),'value':closed},event_id=event_id(key,'closed'))
    await db.flush();await read(db,current,child);await finish_before_cancel(db.commit())
  result[key]=closed
 return result

async def preparation_read(db,row,child,value):
 # read() validates the actual event and its complete launch-history binding.
 value=await read(db,row,child)
 if value is None or value['closed'] is None:return None
 return value['closed'] if unreleased(value['closed']) else None
