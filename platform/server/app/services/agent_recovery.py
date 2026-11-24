"""Recover an OS-verified exited API owner without replaying tools or inference."""
import asyncio,hashlib,json,time,uuid
from types import SimpleNamespace
from sqlalchemy import case,func,select,update
from ..models import AgentEvent,Case,Conversation,ConversationExecution,NativeRequest,ToolCall
from . import conversation_execution as control,process_identity_native as identity
from .durable_events import json_text,lock_case_execution,now
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.agent-recovery.v1'
FIELDS=('phase','version','backend','resource_id','owner_id','owner_token_sha256','owner_identity_json','lease_expires_at')
SAFE_TOOLS={'read_text_file','list_files','search_text','web_search','web_fetch','propose_file_update',
 'propose_text_replacement','update_plan','update_goal','update_memory','get_skill'}
PAGE_SIZE=64

def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()

def binding(row):
 return digest({key:str(getattr(row,key)) for key in FIELDS})

def receipt_id(cid,version,observed):
 return uuid.uuid5(uuid.NAMESPACE_URL,f'vibeflow/agent-recovery/{cid}/{version}/{observed}').hex

def checked_receipt(event,cid,version,observed):
 try:
  value=json.loads(event.payload)
  snapshot=value['original_execution']
  if (event.conversation_id!=cid or event.type!='system' or value['schema']!=SCHEMA or
   value['kind']!='agent_recovery' or value['conversation_id']!=cid or value['original_version']!=version or
   value['evidence_sha256']!=observed or set(snapshot)!=set(FIELDS) or digest(snapshot)!=observed or
   snapshot['version']!=str(version) or value['owner_state']!={'state':'exited','reason':'process_exited'}):
   raise ValueError('Recovery receipt differs from original ownership')
  return value
 except (ValueError,TypeError,KeyError,AttributeError) as error:
  raise ExecutionConflict('evidence_invalid','会话恢复回执与原归属快照不一致，请保留历史人工核对') from error

async def owned(db,cid,user,*,write=False):
 conv=await db.get(Conversation,cid)
 if conv is None:
  from fastapi import HTTPException
  raise HTTPException(404,'会话不存在')
 if user is None:case=await db.get(Case,conv.case_id)
 else:
  from ..routers.workspaces import get_owned_case
  case=await get_owned_case(conv.case_id,user,db,write=write)
 return conv,case

async def preview(db,cid,user=None):
 await owned(db,cid,user)
 row=await db.get(ConversationExecution,cid,populate_existing=True)
 if row is None:return {'required':False,'conversation_id':cid,'phase':'idle','version':0}
 try:control.document(row)
 except (ValueError,TypeError,KeyError) as error:raise ExecutionConflict('evidence_invalid','会话执行归属损坏，请保留记录人工核对') from error
 if row.phase=='idle':return {'required':False,'conversation_id':cid,'phase':'idle','version':row.version}
 observed=await asyncio.to_thread(identity.probe,json.loads(row.owner_identity_json))
 return {'required':True,'conversation_id':cid,'phase':row.phase,'version':row.version,
  'evidence_sha256':binding(row),'backend':row.backend,'owner_state':observed,
  'can_recover':row.backend in {'api','approval','goal'} and observed=={'state':'exited','reason':'process_exited'}}

async def interrupt_history(db,cid,run_id=None):
 """Reconcile the owning execution's persisted run and request lifecycle."""
 from ..routers import agent
 from .conversation_history import run_id_field
 query=select(AgentEvent.type,AgentEvent.payload).where(AgentEvent.conversation_id==cid,
  AgentEvent.type.in_(['run_state','request_state']))
 if run_id is not None:
  query=query.where(run_id_field(db)==run_id)
 runs={};requests={}
 rows=await db.stream(query.order_by(AgentEvent.id).execution_options(yield_per=64))
 try:
  async for kind,raw in rows:
   p=json.loads(raw);rid=p.get('run_id')
   if not rid:continue
   if kind=='run_state':
    pending,key=runs,rid
   elif p.get('request_id'):
    pending,key=requests,(rid,p['request_id'])
   else:continue
   if p.get('status')=='started':pending[key]=p
   else:pending.pop(key,None)
 finally:
  await rows.close()
 count=0
 for rid,p in runs.items():
  result={'run_id':rid,'status':'interrupted'}
  if isinstance(p.get('started_at'),(int,float)):
   result.update(started_at=p['started_at'],ended_at=time.time(),duration_ms=max(0,round((time.time()-p['started_at'])*1000)))
  await agent._append_event(db,cid,'run_state',result);count+=1
 for (rid,request_id),p in requests.items():
  await agent._append_request_state(db,cid,rid,request_id,'interrupted',
   started_at=p.get('started_at'),request_index=p.get('request_index'))
 return count

async def conversation_pages(db,column,*conditions):
 """A startup pass has a fixed upper ID, independent of changes in earlier pages."""
 upper=await db.scalar(select(func.max(column)).where(*conditions))
 if upper is None:return
 query=select(column).distinct().where(*conditions,column<=upper).order_by(column).limit(PAGE_SIZE)
 cursor=None
 while True:
  ids=(await db.scalars(query if cursor is None else query.where(column>cursor))).all()
  if not ids:return
  yield ids
  cursor=ids[-1]
  if cursor==upper or len(ids)<PAGE_SIZE:return

async def recover_orphan_tools(db):
 """Serialize with owner acquisition and leave owned tools to their recovery transaction."""
 occupied=select(ConversationExecution.conversation_id).where(
  ConversationExecution.conversation_id==ToolCall.conversation_id,ConversationExecution.phase!='idle').exists()
 conditions=(ToolCall.status.in_(('queued','running')),~occupied)
 total=0
 async for ids in conversation_pages(db,ToolCall.conversation_id,*conditions):
  await db.rollback()
  for cid in ids:
   case_id=await db.scalar(select(Conversation.case_id).where(Conversation.id==cid))
   if case_id is None:continue
   await lock_case_execution(db,case_id)
   result=await db.execute(update(ToolCall).where(ToolCall.conversation_id==cid,*conditions)
    .values(status='interrupted').execution_options(synchronize_session=False))
   total+=result.rowcount
   await db.commit()
 return total

async def recover_histories(db,run_id=None):
 """Legacy history reconciliation serializes per case and rechecks live owners."""
 from .conversation_history import event_text_field
 rid=event_text_field(db,'run_id')
 request=case((AgentEvent.type=='request_state',event_text_field(db,'request_id')))
 owned=select(ConversationExecution.conversation_id).where(
  ConversationExecution.conversation_id==AgentEvent.conversation_id,ConversationExecution.phase!='idle',
  ConversationExecution.backend.in_(('api','approval','goal'))).exists()
 states=select(AgentEvent.conversation_id,event_text_field(db,'status').label('status'),func.row_number().over(
  partition_by=(AgentEvent.conversation_id,AgentEvent.type,rid,request),
  order_by=AgentEvent.id.desc()).label('position')).where(AgentEvent.type.in_(('run_state','request_state')),~owned)
 if run_id is not None:states=states.where(rid==run_id)
 latest=states.subquery()
 count=0
 async for ids in conversation_pages(db,latest.c.conversation_id,latest.c.position==1,latest.c.status=='started'):
  for cid in ids:
   conv=await db.get(Conversation,cid)
   if conv is None:continue
   await lock_case_execution(db,conv.case_id)
   row=await db.get(ConversationExecution,cid,populate_existing=True)
   if row is not None and row.phase!='idle':
    if row.backend in ('api','approval','goal'):
     # Their lifecycle, tools, owner and receipt must commit together.
     await db.rollback();continue
    try:
     control.document(row)
     observed=await asyncio.to_thread(identity.probe,json.loads(row.owner_identity_json))
    except (ValueError,TypeError,KeyError):observed={'state':'unverified'}
    if observed.get('state')!='exited':
     await db.rollback();continue
   count+=await interrupt_history(db,cid,run_id)
   await db.commit()
 return count

async def unmatched(db,cid):
 from .conversation_history import event_text_field
 scope=AgentEvent.conversation_id==cid
 assistant=(scope,AgentEvent.type=='message',event_text_field(db,'role')=='assistant')
 latest_message_id=await db.scalar(select(func.max(AgentEvent.id)).where(*assistant)) or 0
 last=await db.scalar(select(AgentEvent).where(scope,AgentEvent.type=='run_checkpoint',
  AgentEvent.id>latest_message_id).order_by(AgentEvent.id.desc()).limit(1))
 checkpoint=(last,json.loads(last.payload)) if last else None
 tool_calls=event_text_field(db,'tool_calls')
 result_id=event_text_field(db,'tool_call_id')
 cursor=0
 missing={}
 while cursor<latest_message_id:
  rows=(await db.execute(select(AgentEvent.id,tool_calls).where(*assistant,AgentEvent.id>cursor,
   AgentEvent.id<=latest_message_id,tool_calls.is_not(None)).order_by(AgentEvent.id).limit(PAGE_SIZE))).all()
  if not rows:break
  calls={}
  for _,raw in rows:
   if raw:
    for call in json.loads(raw) or []:
     calls[call['id']]=call
  ids=list(calls)
  for start in range(0,len(ids),400):
   batch=ids[start:start+400]
   matched=set(await db.scalars(select(result_id).distinct().where(scope,AgentEvent.type=='tool_result',result_id.in_(batch))))
   for key in batch:
    if key not in matched:missing[key]=calls[key]
  cursor=rows[-1].id
 return list(missing.values()),checkpoint

async def recover(db,cid,user,*,expected_version,expected_evidence_sha256):
 from .. import storage
 from ..db import SessionLocal
 from ..models import AcpThread,AcpTurn,User
 from ..config import get_settings
 from .case_write_capacity import committing
 from . import case_journal_recovery, storage_operations
 from ..routers import agent
 _,case=await owned(db,cid,user,write=True)
 identifier=receipt_id(cid,expected_version,expected_evidence_sha256)
 existing=await db.scalar(select(AgentEvent).where(AgentEvent.event_id==identifier,AgentEvent.conversation_id==cid))
 if existing is not None:
  value=checked_receipt(existing,cid,expected_version,expected_evidence_sha256)
  row=await db.get(ConversationExecution,cid,populate_existing=True)
  return {'recovered':True,'replayed':True,'recovery_event_id':identifier,
   'original_version':value['original_version'],'current_phase':row.phase,'current_version':row.version}
 plan=await preview(db,cid,user)
 if not plan['required'] or plan['version']!=expected_version or plan['evidence_sha256']!=expected_evidence_sha256:
  raise ExecutionConflict('state_conflict','会话执行归属已变化，请重新查看恢复方案')
 if not plan['can_recover']:
  raise ExecutionConflict('recovery_required','原执行者仍存活、身份未知或属于原生CLI，不能接管')
 if storage._use_s3():
  file_work=await storage_operations.status_locked(db,case.id)
 else:
  file_work=case_journal_recovery.pending_local(case)
 if file_work is not None:
  reference=SimpleNamespace(id=case.id,workspace_id=case.workspace_id,s3_prefix=case.s3_prefix)
  actor_id=user.id if user is not None else None
  await db.rollback()
  await case_journal_recovery.recover_bound_case(reference,SessionLocal)
  if actor_id is not None:
   user=await db.get(User,actor_id,populate_existing=True)
   if user is None or user.status!='active':
    from fastapi import HTTPException
    raise HTTPException(403,'用户已不可用')
  _,case=await owned(db,cid,user,write=True)
  if (case.id,case.workspace_id,case.s3_prefix)!=(reference.id,reference.workspace_id,reference.s3_prefix):
   raise ExecutionConflict('state_conflict','会话算例在文件恢复期间变化')
 async with committing(db,case,defer_journal=True):
  row=await db.get(ConversationExecution,cid,populate_existing=True)
  previous=await db.scalar(select(AgentEvent).where(AgentEvent.event_id==identifier,AgentEvent.conversation_id==cid))
  if previous is not None:
   checked_receipt(previous,cid,expected_version,expected_evidence_sha256)
   return {'recovered':True,'replayed':True,'recovery_event_id':identifier,'original_version':expected_version,
    'current_phase':row.phase,'current_version':row.version}
  if row is None or row.version!=expected_version or binding(row)!=expected_evidence_sha256:
   raise ExecutionConflict('state_conflict','会话执行归属在核验期间变化')
  calls,checkpoint=await unmatched(db,cid)
  for call in calls:
   if call['name'] not in SAFE_TOOLS:
    native=await db.scalar(select(NativeRequest).where(NativeRequest.conversation_id==cid,
     NativeRequest.tool_call_id==f"{cid}:{call['id']}"))
    if native is None:raise ExecutionConflict('recovery_required','未知原生工具缺少执行证明，请先人工核对')
  active=list(await db.scalars(select(ToolCall).where(ToolCall.conversation_id==cid,
   ToolCall.status.in_(['queued','running']))))
  for tool in active:
   if tool.tool not in SAFE_TOOLS:
    raise ExecutionConflict('recovery_required','原生工具仍需独立停止与执行对账')
   tool.status='interrupted';tool.result='原执行者已退出，工具回执未确认；未重放工具'
  acp=await db.get(AcpTurn,row.resource_id,populate_existing=True) if row.backend=='api' else None
  if acp is not None and acp.conversation_id==cid and acp.phase in {'admitted','running','recovery_required'}:
   thread=await db.get(AcpThread,acp.thread_id,populate_existing=True)
   if thread is not None and thread.host_id.startswith('docker:') and thread.conversation_id==cid:
    from . import docker_acp
    from .async_ops import finish_before_cancel
    # The case lock and the original execution snapshot still authorize only
    # this operation. Recheck the exact OS birth before container cleanup.
    observed=await asyncio.to_thread(identity.probe,json.loads(row.owner_identity_json))
    if observed!={'state':'exited','reason':'process_exited'}:
     raise ExecutionConflict('recovery_required','原执行者未核验退出')
    try:stop=await finish_before_cancel(docker_acp.recover(acp,thread,get_settings()))
    except (ValueError,OSError,RuntimeError,TimeoutError) as error:
     raise ExecutionConflict('recovery_required','Docker 回合仍需核验停止') from error
    acp.stop_json=json_text(stop) if stop is not None else None
    acp.phase,acp.error,acp.ended_at='cancelled','OwnerExited',now()
  await db.flush()
  if await control.unsafe_native(db,cid):
   raise ExecutionConflict('recovery_required','原生命令、CLI或暂存仍需独立恢复，不能释放会话执行锁')
  recovered_tools=[]
  for call in calls:
   tool=await db.get(ToolCall,f"{cid}:{call['id']}",populate_existing=True)
   if tool is not None and tool.status=='pending':continue
   await agent._append_event(db,cid,'tool_result',{'tool_call_id':call['id'],'tool':call['name'],
    'args':json.loads(tool.args) if tool else call['args'],'result':'原执行者已退出，工具执行回执未确认；未重放。继续修改前请核对当前文件并重新提案审批。',
    'ok':False,'status':'interrupted','recovery_event_id':identifier})
   recovered_tools.append(call['id'])
  await interrupt_history(db,cid)
  excerpt='';checkpoint_id=None
  if checkpoint:
   checkpoint_id=checkpoint[0].event_id
   excerpt=checkpoint[1].get('content','').encode('utf8')[-32768:].decode('utf8','ignore')
  original_execution={key:str(getattr(row,key)) for key in FIELDS}
  if digest(original_execution)!=expected_evidence_sha256:
   raise ExecutionConflict('state_conflict','会话原归属在恢复提交前变化')
  value={'schema':SCHEMA,'kind':'agent_recovery','conversation_id':cid,'original_version':expected_version,
   'original_execution':original_execution,
   'evidence_sha256':expected_evidence_sha256,'owner_state':plan['owner_state'],'source_backend':row.backend,
   'checkpoint_event_id':checkpoint_id,'partial_excerpt':excerpt,'recovered_tool_ids':recovered_tools,
   'actor_id':user.id if user else None}
  await agent._append_event(db,cid,'system',value)
  event=await db.scalar(select(AgentEvent).where(AgentEvent.conversation_id==cid).order_by(AgentEvent.id.desc()).limit(1))
  event.event_id=identifier
  row.phase='idle';row.backend=row.resource_id=row.owner_id=row.owner_token_sha256=row.owner_identity_json=row.lease_expires_at=None
  row.version+=1;control.document(row)
  version=row.version
 return {'recovered':True,'replayed':False,'recovery_event_id':identifier,'original_version':expected_version,
  'current_phase':'idle','current_version':version,'checkpoint_event_id':checkpoint_id,'recovered_tool_ids':recovered_tools}

def project_history(events):
 """Give the model a marked, bounded excerpt; never manufacture an assistant reply."""
 from .tools import PLAN_BLOCKED_TOOLS

 restricted = {}
 failed = set()
 for event in events:
  payload = event['payload']
  if event['type']=='run_state' and payload.get('run_id'):
   run = (event.get('conversation_id'), payload['run_id'])
   if payload.get('status')=='started' and payload.get('permission') in {'ask','plan'}:
    restricted[run] = payload['permission']
   if payload.get('status')=='failed':
    failed.add(run)
 rejected = {run: mode for run, mode in restricted.items() if run in failed}
 output=[]
 for event in events:
  p=event['payload']
  mode = rejected.get((event.get('conversation_id'), p.get('run_id')))
  if event['type']=='system' and p.get('schema')==SCHEMA and p.get('kind')=='agent_recovery':
   content='系统恢复记录：原API执行者已退出，先前回合中断。工具未重放；待审批操作仍需用户批准，工程阶段须单独确认。'
   if p.get('partial_excerpt'):
    content+='\n以下是已持久保存、尚未完成的模型正文片段（不是完整答复）：\n'+json.dumps(p['partial_excerpt'],ensure_ascii=False)
   output.append({**event,'type':'message','payload':{'role':'user','content':content}})
  elif (event['type']=='message' and p.get('role')=='assistant' and p.get('tool_calls') and mode
        and (mode=='ask' or any(isinstance(call, dict) and call.get('name') in PLAN_BLOCKED_TOOLS
                                for call in p['tool_calls']))):
   # Older permission failures recorded the entire rejected batch as a call.
   # Do not pair it with a later result even if the provider reused a call ID.
   if p.get('content'):
    output.append({**event,'payload':{**p,'tool_calls':None}})
  else:output.append(event)
 return output

async def recover_startup():
    from ..db import SessionLocal

    summary = {'checked': 0, 'recovered': 0, 'replayed': 0, 'skipped': 0, 'failed': 0, 'first_errors': []}
    async with SessionLocal() as scan:
        pages = conversation_pages(scan, ConversationExecution.conversation_id,
                                   ConversationExecution.phase != 'idle',
                                   ConversationExecution.backend.in_(('api', 'approval', 'goal')))
        async for ids in pages:
            await scan.rollback()
            for cid in ids:
                summary['checked'] += 1
                try:
                    async with asyncio.timeout(120), SessionLocal() as db:
                        plan = await preview(db, cid)
                        if not plan.get('can_recover'):
                            summary['skipped'] += 1
                            continue
                        result = await recover(db, cid, None, expected_version=plan['version'],
                                               expected_evidence_sha256=plan['evidence_sha256'])
                        summary['replayed' if result['replayed'] else 'recovered'] += 1
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    summary['failed'] += 1
                    if len(summary['first_errors']) < PAGE_SIZE:
                        summary['first_errors'].append({'conversation_id': cid,
                                                        'code': getattr(error, 'code', type(error).__name__)})
    return summary
