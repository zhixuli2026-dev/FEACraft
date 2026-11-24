"""A real model selects a real shell tool; preparation consumes its run budget."""
import argparse,asyncio,json,sqlite3,time
from pathlib import Path
import httpx
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
from testsupport import IsolatedServer
from app.services import process_identity_native as identity

FIXTURE='''
import asyncio,time
from app.services.native_request_execution import RequestExecutor
_shell_prepare=RequestExecutor._prepare
_shell_execute=RequestExecutor._execute
_shell_timing={}
def _save_shell_timing():
 pending=Path('shell-deadline-timing.pending')
 pending.write_text(json.dumps(_shell_timing))
 pending.replace('shell-deadline-timing.json')
async def _prepare_shell_deadline(self,entry,*args,**kwargs):
 result=await _shell_prepare(self,entry,*args,**kwargs)
 if self.kind=='agent.shell':
  _shell_timing['preparation_pause_at']=time.monotonic();_save_shell_timing()
  await asyncio.sleep(5)
  _shell_timing['preparation_return_at']=time.monotonic();_save_shell_timing()
 return result
RequestExecutor._prepare=_prepare_shell_deadline
async def _execute_shell_deadline(self,entry,*args,**kwargs):
 if self.kind!='agent.shell':return await _shell_execute(self,entry,*args,**kwargs)
 _shell_timing.update(request_id=self.owner.id,child_id=entry.id,deadline=entry.deadline,started_at=time.monotonic())
 release=entry.owned.release
 async def released():
  await release()
  _shell_timing['released_at']=time.monotonic();_save_shell_timing()
 entry.owned.release=released
 failure=None
 try:return await _shell_execute(self,entry,*args,**kwargs)
 except BaseException as error:failure=type(error).__name__;raise
 finally:
  _shell_timing.update(finished_at=time.monotonic(),reason=entry.reason,error=failure,stop=entry.owned.stop_result)
  _save_shell_timing()
RequestExecutor._execute=_execute_shell_deadline
'''

def verify(configuration,*,database_url=None):
    api=IsolatedServer('live-shell-deadline',app='live_fixture:app',database_url=database_url)
    (api.root/'live_fixture.py').write_text(LIVE_FIXTURE+FIXTURE,encoding='utf8')
    result={'passed':False,'simulated_llm':False,'mock_llm_calls':0,'source_database_modified':False,
        'provider':'deepseek','host':'api.deepseek.com','model':configuration['model']}
    events=[]
    print('LIVE_SHELL_DEADLINE_EVIDENCE '+str(api.root),flush=True)
    def save():
        trace=api.root/'upstream.jsonl'
        result['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        result['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions') for v in result['upstream'])
        for name,value in (('live-result.json',result),('live-model-events.json',events)):
            raw=json.dumps(value,ensure_ascii=False,indent=2);assert configuration['key'] not in raw
            (api.root/name).write_text(raw,encoding='utf8')
    try:
        with api:
            case=api.create_case('Actual model shell total deadline')
            profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Real deadline verification',
                'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
            conversation=api.call('POST','/conversations',{'case_id':case['id'],'title':'Actual shell total deadline',
                'provider_profile_id':profile['id'],'model':configuration['model'],'permission_mode':'full_access'})
            cid=conversation['id'];result['conversation_id']=cid
            prompt="请仅调用一次 run_shell_command 工具，runtime 设为 powershell，timeout_seconds 设为 12，command 原样使用：Start-Sleep -Seconds 25; Write-Output 'NATIVE_DEADLINE_SHOULD_NOT_COMPLETE'。这是隔离算例里的实际超时测试。无论成功或失败都不要重试、不要调用其他工具，最后用一句中文如实报告工具结果。"
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
                with client.stream('POST',f'/conversations/{cid}/messages',json={'content':prompt,
                        'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith('data:'):events.append(json.loads(line[5:]))
            save()
            assert not [e for e in events if e.get('type') in {'stream.error','request.failed','request.timed_out'}]
            tools=[e for e in events if e.get('type')=='tool.result']
            assert len(tools)==1 and tools[0]['tool']=='run_shell_command' and tools[0]['ok'] is False,tools
            assert any(e.get('type')=='run.completed' for e in events)
            timing=json.loads((api.root/'shell-deadline-timing.json').read_text());result['timing']=timing
            assert timing['reason']=='timeout' and timing['stop']['verified']
            assert timing['preparation_return_at']-timing['preparation_pause_at']>=4.8
            assert timing['released_at']<timing['deadline']<timing['finished_at']
            assert timing['finished_at']<timing['released_at']+11,'Runtime received a fresh 12-second budget after preparation'
            if database_url is None:
                with sqlite3.connect(api.root/'test.sqlite') as db:
                    row=db.execute('SELECT phase,error,children_json,profile_json FROM native_requests WHERE id=?',(timing['request_id'],)).fetchone()
            else:
                async def read_request():
                    from sqlalchemy import text
                    from sqlalchemy.ext.asyncio import create_async_engine
                    engine=create_async_engine(database_url)
                    try:
                        async with engine.connect() as db:
                            return (await db.execute(text('SELECT phase,error,children_json,profile_json FROM native_requests WHERE id=:id'),
                                {'id':timing['request_id']})).one()
                    finally:await engine.dispose()
                row=asyncio.run(read_request())
            assert row[0]=='failed' and row[1]=='TimeoutError'
            child=next(iter(json.loads(row[2])['children'].values()));selected=json.loads(row[3])
            assert selected['timeout_seconds']==12 and selected['runtime']=='powershell'
            assert child['stop']['verified'] and child['stop']['reason']=='timeout'
            result['actual_exit']=identity.probe(child['identity']['host']);assert result['actual_exit']['state']=='exited'
            result['answer']=''.join(e.get('text','') for e in events if e.get('type')=='text.delta')
            assert result['answer'];result['passed']=True
            result['checks']=['real_model_selected_shell','preparation_consumes_native_deadline','actual_shell_timeout_and_verified_stop','real_model_reports_tool_failure']
            save();print('LIVE_SHELL_DEADLINE_PASS '+str(api.root),flush=True)
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1000]}
        save();raise RuntimeError('Real shell deadline acceptance failed; see '+str(api.root/'live-result.json')) from None
    finally:
        result['api_exited']=api.proc is not None and api.proc.poll() is not None
        save()
    return api.root

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);args=parser.parse_args()
    configuration=await source(args.source_profile_id);await asyncio.to_thread(verify,configuration)
if __name__=='__main__':asyncio.run(main())
