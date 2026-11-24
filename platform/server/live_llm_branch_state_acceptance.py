"""Actual pending proposals, goal ownership, lifecycle cleanup and cross-worker deletion."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import uuid

import httpx

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main
from live_llm_publication_restart_acceptance import worker_peer


FIXTURE = '''
import asyncio,time,uuid
from contextlib import asynccontextmanager
from fastapi import Depends,HTTPException
from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import Session
from app.db import get_db
from app.deps import get_current_user
from app.models import AgentEvent,Conversation,ToolCall,ConversationExecution
from app.routers import agent
from app.routers.workspaces import get_owned_case

@event.listens_for(Session,'after_flush')
def fail_child(session,ctx):
    if (root/'fail-new-fork').exists() and any(isinstance(c,Conversation) and c.forked_from_id for c in session.new):
        (root/'fork-flushed').touch()
        raise RuntimeError('test failure after inserting the branch')

async def hold(label):
    if not (root/label).exists():return
    (root/(label+'.observed')).touch()
    deadline=time.monotonic()+45
    while (root/label).exists():
        if time.monotonic()>=deadline:raise TimeoutError(label)
        await asyncio.sleep(.03)
capacity=agent.file_tool_capacity
@asynccontextmanager
async def observed_capacity(db,case,name):
    if name=='update_goal':await hold('queued')
    async with capacity(db,case,name) as result:yield result
agent.file_tool_capacity=observed_capacity
execute=agent.ToolExecutor.execute_case_tool
async def observed_execute(self,db,case,name,*args,**kwargs):
    if name=='update_goal':await hold('running')
    return await execute(self,db,case,name,*args,**kwargs)
agent.ToolExecutor.execute_case_tool=observed_execute

upstream=httpx.AsyncClient.send
async def observed_send(client,request,*args,**kwargs):
    spec=root/'child-request-check.json'
    if spec.exists() and request.url.host=='api.deepseek.com':
        expected=json.loads(spec.read_text())
        messages=json.loads(request.content)['messages']
        if expected['child'] in str(messages[-1].get('content')):
            ids={call['id'] for m in messages for call in (m.get('tool_calls') or [])}
            results={m['tool_call_id'] for m in messages if m.get('role')=='tool'}
            system=''.join(m['content'] for m in messages if m.get('role')=='system')
            sanitized={'parent_active_goal_in_system':expected['goal'] in system,
                       'pending_call_forwarded':expected['pending'] in ids,
                       'pending_result_forwarded':expected['pending'] in results,
                       'completed_call_and_result':expected['completed'] in ids & results}
            (root/'child-request-observed.json').write_text(json.dumps(sanitized))
    return await upstream(client,request,*args,**kwargs)
httpx.AsyncClient.send=observed_send

@app.get('/api/v1/testing/branch/{anchor}/{cid}')
async def counts(anchor:int,cid:int,user=Depends(get_current_user),db=Depends(get_db)):
    owned=await db.get(Conversation,anchor)
    if owned is None:raise HTTPException(404)
    await get_owned_case(owned.case_id,user,db)
    conv=await db.get(Conversation,cid)
    if conv is not None and conv.case_id!=owned.case_id:raise HTTPException(404)
    async def n(model):
        return await db.scalar(select(func.count()).select_from(model).where(model.conversation_id==cid))
    return {'events':await n(AgentEvent),'tools':await n(ToolCall),'owners':await n(ConversationExecution)}

@app.post('/api/v1/testing/branch/{cid}/cleanup/{other}')
async def replay_cleanup(cid:int,other:int,user=Depends(get_current_user),db=Depends(get_db)):
    conv=await db.get(Conversation,cid)
    foreign=await db.get(Conversation,other)
    if cid==other or conv is None or foreign is None or conv.case_id!=foreign.case_id:raise HTTPException(404)
    await get_owned_case(conv.case_id,user,db)
    rid=uuid.uuid4().hex
    def state(owner,rid,seq,kind,status):
        payload={'run_id':rid,'status':status,'started_at':time.time()-2}
        if kind=='request_state':payload.update(request_id='same-request',request_index=0)
        return dict(conversation_id=owner,event_id=uuid.uuid4().hex,seq=seq,type=kind,payload=json.dumps(payload))
    for owner,status in [(cid,'started'),(other,'completed')]:
        rows=[state(owner,uuid.uuid4().hex,n,'run_state','completed') for n in range(1,1201)]
        rows += [state(owner,rid,1201,'run_state',status),state(owner,rid,1202,'request_state',status)]
        for i in range(0,len(rows),128):await db.execute(insert(AgentEvent),rows[i:i+128])
    await db.commit()
    await agent._mark_run_interrupted(cid,rid)
    await agent._mark_run_interrupted(cid,rid)
    counts=[]
    for owner in (cid,other):
        query=select(AgentEvent.type,AgentEvent.payload).where(AgentEvent.conversation_id==owner,AgentEvent.seq>1202)
        appended={kind:json.loads(raw) for kind,raw in (await db.execute(query)).all()}
        total=await db.scalar(select(func.count(AgentEvent.id)).where(AgentEvent.conversation_id==owner))
        counts.append(total)
        if owner==cid:
            assert set(appended)=={'run_state','request_state'}
            assert all(p['status']=='interrupted' and p['run_id']==rid and p['duration_ms']>0 for p in appended.values())
        else:assert not appended
    assert counts==[1204,1202]
    return {'seeded_events':2404,'first_conversation_added':2,'other_conversation_added':0,'repeated_cleanup_added':0}
'''


def scenario(config, api, *, store=None, **_):
    profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual state and proposal',
                     'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
    def new():
        return api.call('POST','/conversations',{'title':'Branch ownership','case_id':api.case_id,
                        'permission_mode':'approve_writes','provider_profile_id':profile['id'],'model':config['model']})['id']
    def detail(cid):
        return api.call('GET',f'/conversations/{cid}')
    def read_file():
        return api.call('GET',api.prefix+'/file?path=Ref/shared.txt')['content_sha256']
    def turn(cid,prompt):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                          timeout=180,trust_env=False) as client:
            res=client.post(f'/conversations/{cid}/messages',json={'content':prompt,'model':config['model'],
                            'provider_profile_id':profile['id']})
            res.raise_for_status()
            return [json.loads(line[5:]) for line in res.text.splitlines() if line.startswith('data:')]

    source, anchor, sibling=new(), new(), new()
    marker='GOAL_'+uuid.uuid4().hex[:12]
    text='Shared file '+uuid.uuid4().hex
    digest=hashlib.sha256(text.encode()).hexdigest()
    api.put('Ref/shared.txt',text)
    cleanup=api.call('POST',f'/testing/branch/{anchor}/cleanup/{sibling}')
    with worker_peer(api,store) as peer, ThreadPoolExecutor(max_workers=1) as pool:
        gates=[api.root/k for k in ('queued','running')]
        for gate in gates:gate.touch()
        worker=pool.submit(turn,source,'仅调用一次 update_goal，objective 逐字填写 '+marker+
                           '，status 为 active；保持目标为 active，成功后只回复已登记，无需继续执行。')
        try:
            for gate in gates:
                wait_for(gate.with_suffix('.observed').exists,'Real tool never reached '+gate.name)
                snapshot=peer.call('GET',f'/conversations/{source}')
                assert any(t['tool']=='update_goal' and t['status']==gate.name for t in snapshot['tool_calls'])
                peer.call('DELETE',f'/conversations/{source}',expect=409)
                gate.unlink()
        finally:
            for gate in gates:gate.unlink(missing_ok=True)
        finished=worker.result()
        assert any(e['type']=='run.completed' for e in finished)
        assert detail(source)['current_goal']['objective']==marker and detail(source)['current_goal']['status']=='active'
        completed=next(e['tool_call_id'] for e in finished if e['type']=='tool.result' and e.get('tool')=='update_goal')
        proposed=turn(source,'仅调用一次 propose_file_update，将 Ref/shared.txt 的内容改为 proposal awaiting review，然后等待批准。')
        pending=next(e['tool_call']['id'] for e in proposed if e['type']=='approval.requested')
        peer.call('DELETE',f'/conversations/{source}',expect=409)
        parent=detail(source)
        cases_url=f'/conversations?caseId={api.case_id}'
        before_ids={c['id'] for c in api.call('GET',cases_url)}
        flag=api.root/'fail-new-fork'
        flag.touch()
        try:
            api.call('POST',f'/conversations/{source}/fork',{},expect=500,raw=True)
        finally:
            flag.unlink()
        assert (api.root/'fork-flushed').exists() and {c['id'] for c in api.call('GET',cases_url)}==before_ids
        child=api.call('POST',f'/conversations/{source}/fork')['id']
        child_history=detail(child)
        assert [e['id'] for e in child_history['events']]==[e['id'] for e in parent['events']]
        assert child_history['current_goal'] is None and not child_history['pending_tools'] and not child_history['tool_calls']
        assert api.call('GET',f'/testing/branch/{anchor}/{child}')=={'events':0,'owners':0,'tools':0}
        assert read_file()==digest
        for action in ('approve','reject'):
            api.call('POST',f'/conversations/{child}/tool-calls/{pending}:{action}',expect=404)
        api.call('POST',f'/conversations/{child}/goal:run',{},expect=404)
        before=detail(source)
        assert before['pending_tools'][0]['id']==pending and before['current_goal']==parent['current_goal']
        assert before['events']==parent['events']
        api.call('PATCH',f'/conversations/{child}/permission',{'permission_mode':'ask'})
        child_marker='CHILD_'+uuid.uuid4().hex[:12]
        (api.root/'child-request-check.json').write_text(json.dumps({'child':child_marker,'completed':completed,
            'pending':pending.split(':',1)[1],'goal':marker}))
        assert any(e['type']=='run.completed' for e in turn(child,'本轮只回复 '+child_marker))
        observed=json.loads((api.root/'child-request-observed.json').read_text())
        assert observed=={'parent_active_goal_in_system':False,'pending_call_forwarded':False,
                          'pending_result_forwarded':False,'completed_call_and_result':True}
        assert any(e.get('can_fork') and child_marker in e['payload']['content'] for e in detail(child)['events'])
        assert detail(source)['events']==before['events'] and read_file()==digest
        assert api.call('POST',f'/conversations/{source}/tool-calls/{pending}:reject')['status']=='rejected'
        peer.call('DELETE',f'/conversations/{source}',expect=409)
        for value in ('   ','x'*201):
            peer.call('PATCH',f'/conversations/{source}',{'title':value},expect=422)
        assert api.call('PATCH',f'/conversations/{source}',{'title':'   '+'x'*194+'   '})['title']=='x'*194
        assert api.call('PATCH',f'/conversations/{source}',{'title':'R'*200})['title']=='R'*200
        assert api.call('DELETE',f'/conversations/{child}')['ok']
        assert detail(source)['current_goal']['status']=='active'
        assert api.call('DELETE',f'/conversations/{source}')['ok']
        for removed in (child,source):
            peer.call('GET',f'/conversations/{removed}',expect=404)
            assert api.call('GET',f'/testing/branch/{anchor}/{removed}')=={'events':0,'tools':0,'owners':0}
        assert read_file()==digest
    result={'database':'postgresql' if api.database_url else 'sqlite','storage':'s3' if store else 'fs',
            'cross_worker_deletion_denied':['queued','running','pending','has_child'],'real_goal':True,
            'real_pending_proposal':True,'child_denied_approval_and_goal':True,'child_request':observed,
            'source_history_identical_after_child_turn':True,'shared_file_sha256':digest,
            'fork_flush_rollback':True,'post_delete_conversation_rows_zero':True,'run_cleanup':cleanup,'rename_constraints':True}
    (api.root/'branch-state.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    asyncio.run(main(selected_factory=lambda config:partial(scenario,config),fixture_override=FIXTURE))
