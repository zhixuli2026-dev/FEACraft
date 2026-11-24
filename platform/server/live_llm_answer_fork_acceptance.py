"""Actual DeepSeek replies, branch ownership, browser selection, cancellation and concurrent deletion."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import uuid

import httpx

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main
from live_llm_outbox_restart_acceptance import restart


FIXTURE = '''
from contextlib import asynccontextmanager
from contextvars import ContextVar
import asyncio, time
from fastapi import Depends, HTTPException
from sqlalchemy import event, func, select
from app.db import get_db
from app.deps import get_current_user
from app.models import AgentEvent, Conversation, ToolCall
from app.routers import agent
from app.routers.workspaces import get_owned_case
from app.services import conversation_history

async def hold(label):
    flag=root/label
    if not flag.exists():
        return
    (root/(label+'.observed')).touch()
    deadline=time.monotonic()+30
    while flag.exists():
        if time.monotonic()>=deadline:
            raise TimeoutError(label)
        await asyncio.sleep(.03)

send_live=httpx.AsyncClient.send
async def hold_response(client, request, *args, **kwargs):
    response=await send_live(client,request,*args,**kwargs)
    if request.url.host=='api.deepseek.com' and response.status_code==200:
        try:
            await hold('cancel-after-provider')
        except BaseException:
            await response.aclose()
            raise
    return response
httpx.AsyncClient.send=hold_response

lock=agent.lock_case_execution
async def fork_lock(db,cid):
    await hold('before-fork-lock')
    await lock(db,cid)
    await hold('after-fork-lock')
agent.lock_case_execution=fork_lock

capacity=agent.storage_capacity
@asynccontextmanager
async def observed_capacity(*args,**kwargs):
    if (root/'after-fork-lock').exists():
        (root/'delete-started').touch()
    async with capacity(*args,**kwargs) as result:
        yield result
agent.storage_capacity=observed_capacity

reading=ContextVar('fork_read',default=None)
@event.listens_for(AgentEvent,'load')
def count_event(row,ctx):
    stats=reading.get()
    if stats is not None:stats['event_objects']+=1
check=conversation_history.visible_answer
async def measured(db,src,head):
    stats={'event_objects':0}
    token=reading.set(stats)
    try:
        return await check(db,src,head)
    finally:
        reading.reset(token)
        with (root/'fork-loads.jsonl').open('a') as out:
            out.write(json.dumps(stats)+'\\n')
conversation_history.visible_answer=measured

@app.get('/api/v1/testing/fork/{cid}/owned')
async def direct(cid:int,user=Depends(get_current_user),db=Depends(get_db)):
    conv=await db.get(Conversation,cid)
    if conv is None:raise HTTPException(404)
    await get_owned_case(conv.case_id,user,db)
    return {'events':await db.scalar(select(func.count(AgentEvent.id)).where(AgentEvent.conversation_id==cid)),
            'tools':await db.scalar(select(func.count(ToolCall.id)).where(ToolCall.conversation_id==cid))}
'''


def scenario(config, api, **_):
    name='Answer boundary '+uuid.uuid4().hex[:8]
    api.create_case(name)
    profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual branch run',
                     'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
    def new(title='Review',mode='ask'):
        return api.call('POST','/conversations',{'case_id':api.case_id,'title':title,'permission_mode':mode,
                        'provider_profile_id':profile['id'],'model':config['model']})['id']
    def fork(cid,head=None,status=200):
        return api.call('POST',f'/conversations/{cid}/fork',{} if head is None else {'head_event_id':head},expect=status)
    def own(cid):
        return api.call('GET',f'/testing/fork/{cid}/owned')
    def detail(cid):
        return api.call('GET',f'/conversations/{cid}')
    def evidence(cid):
        return api.call('GET',f'/conversations/{cid}/export',raw=True)
    def turn(cid,prompt):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                          timeout=180,trust_env=False) as client:
            response=client.post(f'/conversations/{cid}/messages',json={'content':prompt,
                                 'provider_profile_id':profile['id'],'model':config['model']})
            response.raise_for_status()
            return [json.loads(l[5:]) for l in response.text.splitlines() if l.startswith('data:')]
    def conversations():
        return {c['id'] for c in api.call('GET',f'/conversations?caseId={api.case_id}')}

    cid=new('F'*300,'plan')
    marker='PROOF_'+uuid.uuid4().hex[:12].upper()
    api.put('Ref/fork-proof.txt',marker)
    settings={'case':name,'base':api.base,'token':api.token,'cid':cid,'marker':marker,
              'prompts':['请仅调用一次 read_text_file 读取 Ref/fork-proof.txt。最终只逐字返回文件内容，不加解释。',
                         '这是第二次提问。请仅回复文本 '+marker],
              'output':str(api.root/'fork-browser.json')}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],str(Path(__file__).with_name('answer_fork_browser.py'))],
                   input=json.dumps(settings),text=True,check=True,timeout=210)
    ui=json.loads(Path(settings['output']).read_text())
    first,second=ui['same_text_distinct_ids']
    child=ui['first_child']
    assert own(child)=={'events':0,'tools':0} and len(detail(child)['title'])==300
    original=evidence(cid)
    later=fork(cid,second)['id']
    assert [e['id'] for e in detail(later)['events']]==[e['id'] for e in detail(cid)['events'] if e['id']<=second]
    assert own(later)=={'events':0,'tools':0}
    source_events=detail(cid)['events']
    boundary=next(e for e in source_events if e['id']==first)
    intermediate=next(e for e in source_events if e['type']=='message' and e['payload'].get('tool_calls'))
    assert not intermediate['can_fork'] and any(e['type']=='tool_result' and marker in e['payload'].get('result','') for e in source_events)
    user_message=next(e for e in source_events if e['type']=='message' and e['payload'].get('role')=='user')
    before=conversations()
    fork(cid,intermediate['id'],409)
    fork(cid,user_message['id'],409)
    fork(child,second,404)
    fork(cid,2**70,404)
    assert conversations()==before
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other','password':'isolated-test-123'})
    for method,path,body in [('POST',f'/conversations/{cid}/fork',{'head_event_id':first}),
                             ('PATCH',f'/conversations/{child}',{'title':'Private'}),('DELETE',f'/conversations/{child}',None)]:
        api.call(method,path,body,expect=404,extra_headers={'Authorization':'Bearer '+other['token']})
    assert any(e['type']=='run.completed' for e in turn(child,'请只逐字回复之前文件中读到的完整标识。'))
    own_reply=next(e for e in detail(child)['events'] if e['id']>second and e.get('can_fork'))
    assert own_reply['payload']['content'].strip()==marker and evidence(cid)==original
    fork(cid,own_reply['id'],404)
    nested=fork(child,first)['id']
    assert detail(nested)['events'][-1]['id']==first and own(nested)['events']==0

    # The text always comes from the preceding actual provider reply. Only
    # the imported lifecycle metadata is varied for these protocol checks.
    statuses={}
    rid=boundary['payload']['run_id']
    msg={'type':'message','payload':boundary['payload']}
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False) as client:
        for state in ('missing','failed','interrupted','timed_out','cancelled','legacy'):
            lines=[msg]
            if state=='legacy':
                lines=[{'type':'message','payload':{'role':'assistant','content':marker}}]
            elif state!='missing':
                lines.append({'type':'run_state','payload':{'run_id':rid,'status':state}})
            res=client.post(f'/conversations:import?case_id={api.case_id}',content=b'\n'.join(json.dumps(e).encode() for e in lines))
            res.raise_for_status()
            copied=res.json()['id']
            target=detail(copied)['events'][0]
            accepted=state=='legacy'
            assert target['can_fork'] is accepted
            fork(copied,target['id'],200 if accepted else 409)
            statuses[state]=200 if accepted else 409

    cancelled=new()
    gate=api.root/'cancel-after-provider'
    with ThreadPoolExecutor(max_workers=2) as pool:
        gate.touch()
        running=pool.submit(turn,cancelled,'请给我一个关于流体力学的简短定义。')
        try:
            wait_for((api.root/'cancel-after-provider.observed').exists,'No real upstream response arrived')
            api.call('DELETE',f'/conversations/{cancelled}',expect=409)
            assert api.call('POST',f'/conversations/{cancelled}/messages:cancel')['ok']
        finally:
            gate.unlink()
        assert any(e['type']=='run.cancelled' for e in running.result())
        cancelled_reply=next(e for e in detail(cancelled)['events'] if e['payload'].get('cancelled'))
        assert cancelled_reply['can_fork'] is False
        fork(cancelled,cancelled_reply['id'],409)
        assert api.call('DELETE',f'/conversations/{cancelled}')['ok']
        api.call('GET',f'/conversations/{cancelled}',expect=404)
        # Hold the real database lock in each ordering; verify no orphan or HTTP 500.
        for delete_first in (False,True):
            root=new()
            label='before-fork-lock' if delete_first else 'after-fork-lock'
            flag=api.root/label
            flag.touch()
            request=pool.submit(fork,root,None,404 if delete_first else 200)
            try:
                wait_for((api.root/(label+'.observed')).exists,'Fork did not reach transaction boundary')
                removal=pool.submit(api.call,'DELETE',f'/conversations/{root}',expect=200 if delete_first else 409)
                if delete_first:
                    assert removal.result()['ok']
                else:
                    wait_for((api.root/'delete-started').exists,'Removal did not enter the write path')
            finally:
                flag.unlink()
            result=request.result()
            removal.result()
            if not delete_first:
                assert detail(result['id'])['forked_from_id']==root
                assert api.call('DELETE',f'/conversations/{result["id"]}')['ok']
                assert api.call('DELETE',f'/conversations/{root}')['ok']
    api.proc.terminate();api.proc.wait(timeout=15);restart(api)
    assert detail(nested)['events'][-1]['can_fork'] and evidence(cid)==original
    counts=[json.loads(l)['event_objects'] for l in (api.root/'fork-loads.jsonl').read_text().splitlines()]
    assert max(counts)==1
    result={'database':'postgresql' if api.database_url else 'sqlite','browser':ui,'same_text':True,
            'zero_copy':True,'source_archive_unchanged':True,'child_read_original_value':True,
            'real_cancelled_boundary_http':409,'cancelled_conversation_removed':True,'real_intermediate_tool_http':409,'other_user_http':404,
            'terminal_metadata_checks':statuses,'maximum_event_objects_per_lookup':max(counts),
            'fork_wins_delete_http':409,'delete_wins_fork_http':404,'restart_preserves_boundary':True}
    (api.root/'answer-fork.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    bundle=Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (bundle/'index.html').is_file()
    mount="\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(bundle))+",html=True))\n"
    asyncio.run(main(selected_factory=lambda config:partial(scenario,config),fixture_override=FIXTURE+mount))
