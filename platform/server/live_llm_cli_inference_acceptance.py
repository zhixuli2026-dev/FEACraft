"""Actual installed CLI inference against the authorized provider; no model substitute."""
import argparse,asyncio,json,os,re,sys,time,uuid,tempfile
from pathlib import Path
import httpx
from testsupport import IsolatedServer
from live_llm_acceptance import source,FIXTURE as LIVE
from cli_managed_session_checks import FIXTURE as MANAGED

FIXTURE=r'''
@app.post('/api/v1/test/prepare-cli-inference')
async def prepare_inference(user=Depends(get_current_user)):
 await native_root()
 return {'ready':True}

from app.services import codex_turns
original_inference_event=codex_turns.Execution.event
async def inference_event(self,event):
 if event.get('method') in {'error','turn/completed','turn/started','thread/tokenUsage/updated','item/agentMessage/delta'}:
  path=_base/'actual-cli-events.jsonl'
  if not path.exists() or path.stat().st_size<1024*1024:
   with path.open('a',encoding='utf8') as stream:
    stream.write(json.dumps(self.projection.secrets.clean(event),ensure_ascii=False)+'\n')
 if event.get('method')=='error':
  diagnostic=bytes(self.session.client.transport.stderr_tail).decode('utf8','replace')
  (_base/'actual-cli-stderr.txt').write_text(self.projection.secrets.clean(diagnostic),encoding='utf8')
 return await original_inference_event(self,event)
codex_turns.Execution.event=inference_event

from app.services import node_storage_budget,cli_runtime_storage
original_node_observe=node_storage_budget.observe
original_user_observe=cli_runtime_storage.observe
def inventory_failure(error):
 chain=[]
 while error is not None and len(chain)<8:
  chain.append({'type':type(error).__name__,'code':getattr(error,'code',None),
   'message':str(error)[:500],'native_entry_metadata':getattr(error,'native_entry_metadata',None)})
  error=error.__cause__
 with (_base/'actual-inventory-failures.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(chain,ensure_ascii=False)+'\n')
def node_observe(*args,**kwargs):
 try:return original_node_observe(*args,**kwargs)
 except Exception as error:inventory_failure(error);raise
async def user_observe(*args,**kwargs):
 try:return await original_user_observe(*args,**kwargs)
 except Exception as error:inventory_failure(error);raise
node_storage_budget.observe=node_observe
cli_runtime_storage.observe=user_observe

from app.services import cli_initialization_capacity,conversation_execution,codex_turn_records
import time
def timed_operation(obj,name,label):
 original=getattr(obj,name)
 async def timed(*args,**kwargs):
  started=time.monotonic()
  with (_base/'actual-lifecycle-timing.jsonl').open('a',encoding='utf8') as stream:
   stream.write(json.dumps({'operation':label,'phase':'start','at':started})+'\n')
  try:return await original(*args,**kwargs)
  finally:
   with (_base/'actual-lifecycle-timing.jsonl').open('a',encoding='utf8') as stream:
    stream.write(json.dumps({'operation':label,'phase':'end','at':time.monotonic(),'seconds':time.monotonic()-started})+'\n')
 setattr(obj,name,timed)
for obj,name,label in [(sessions.Session,'retire_work','session.retire'),(sessions.Session,'close_work','session.close'),
 (cli_initialization_capacity,'closed','initialization.closed'),(codex_turn_records,'finish','turn.finish'),
 (conversation_execution,'release','execution.release'),(cli_runtime_storage,'observe','runtime.inventory')]:
 timed_operation(obj,name,label)
'''


def verify(configuration,*,read_file=False,runtime='wsl',continuations=False):
    api=IsolatedServer('live-cli-inference',app='live_fixture:app')
    (api.root/'live_fixture.py').write_text(LIVE+MANAGED+FIXTURE,encoding='utf8')
    report={'passed':False,'mock_llm_calls':0,'simulated_llm':False,'cli_model_inference_verified':False,
        'provider':'deepseek','host':'api.deepseek.com','model':configuration['model'],'configured_protocol':'responses',
        'developer_database_modified':False,'upstream_success_count_verified':False,'read_file_scenario':read_file,
        'runtime':runtime,'actual_CLI_turns_started':0}
    print('LIVE_CLI_INFERENCE '+str(api.root),flush=True)
    def save():
        encoded=json.dumps(report,ensure_ascii=False,indent=2)
        assert configuration['key'] not in encoded
        (api.root/'live-result.json').write_text(encoded+'\n',encoding='utf8')
    def call(method,path,payload=None):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=150,trust_env=False) as client:
            response=client.request(method,path,json=payload)
            if response.status_code!=200:
                raise RuntimeError(method+' '+path+' HTTP '+str(response.status_code)+' '+response.text.replace(configuration['key'],'[REDACTED]')[:800])
            return response.json()
    session=None;turn=None
    try:
        with api:
            try:
                api.create_case('Actual installed CLI model inference')
                if runtime=='wsl':call('POST','/test/prepare-cli-inference')
                profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual CLI inference',
                    'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
                conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual CLI inference',
                    'permission_mode':'approve_writes' if read_file else 'ask','provider_profile_id':profile['id'],'model':configuration['model']})
                report['conversation_id']=conversation['id']
                nonce='ACTUAL_CLI_'+uuid.uuid4().hex
                if read_file:api.put('Ref/cli-inference-proof.txt',nonce+'\n')
                session=call('POST','/codex/sessions',{'case_id':api.case_id,'conversation_id':conversation['id'],
                    'provider_profile_id':profile['id'],'provider_wire_api':'responses','runtime':runtime})
                report['session_id']=session['id'];save()
                prompt=('请实际调用命令工具执行 cat Ref/cli-inference-proof.txt，读取后只回复文件里的验证字符串。不要猜测，不要修改文件，不要添加解释。' if read_file else
                    '只回复以下字符串，不调用工具：'+nonce)
                if read_file and runtime=='windows':
                    prompt='请实际调用命令工具执行 PowerShell 命令 Get-Content -LiteralPath Ref/cli-inference-proof.txt，读取后只回复文件里的验证字符串。不要猜测，不要修改文件，不要添加解释。'
                turn=call('POST','/codex/sessions/'+session['id']+'/turns',{'content':prompt,
                    'client_message_id':uuid.uuid4().hex,'timeout_seconds':120})
                report['turn_id']=turn['id'];report['actual_CLI_turns_started']+=1;save()
                deadline=time.monotonic()+180
                while turn['phase'] not in {'completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'}:
                    if time.monotonic()>=deadline:raise TimeoutError('Actual CLI turn exceeded its observation deadline')
                    if read_file and turn['phase']=='awaiting_input':
                        approvals=call('GET','/codex/turns/'+turn['id']+'/approvals')
                        user=api.call('GET','/auth/me')
                        proofs=list((api.root/'files').rglob('cli-inference-proof.txt'))
                        assert len(proofs)==1,'Read proof must identify one actual case file'
                        host=proofs[0].parent.parent.as_posix()
                        cwd='/mnt/'+host[0].lower()+host[2:] if runtime=='wsl' else str(proofs[0].parent.parent)
                        for approval in approvals:
                            if approval['phase']!='pending':continue
                            params=approval['params'];actions=params.get('commandActions',[])
                            assert approval['method']=='item/commandExecution/requestApproval'
                            if runtime=='wsl':
                                assert params.get('command') in {"/usr/bin/bash -c 'cat Ref/cli-inference-proof.txt'",'cat Ref/cli-inference-proof.txt'}
                                assert params.get('cwd')==cwd and len(actions)==1 and actions[0]['type']=='read'
                                assert actions[0]['path']==cwd+'/Ref/cli-inference-proof.txt'
                            else:
                                # Any unexpected approval remains pending; never
                                # broaden approval to arbitrary model commands.
                                command=params.get('command','')
                                allowed='Get-Content -LiteralPath Ref/cli-inference-proof.txt'
                                if command!=allowed:
                                    wrapped=re.fullmatch(r'"([^"]+)" -NoProfile -Command \'([^\']+)\'',command)
                                    shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
                                    assert wrapped and Path(wrapped[1]).resolve()==shell.resolve() and wrapped[2]==allowed,{'unexpected_windows_approval':params}
                                assert Path(params['cwd']).resolve()==proofs[0].parent.parent.resolve()
                                assert len(actions)==1 and actions[0]['type']=='read'
                                assert Path(actions[0]['path']).resolve()==proofs[0].resolve()
                            assert len(report.setdefault('approved_reads',[]))<2,'Unexpected repeated command approval'
                            result=call('POST','/codex/turns/'+turn['id']+'/approvals/'+approval['id']+':respond',{'decision':'accept'})
                            report['approved_reads'].append({'approval':approval,'response':result});save()
                    time.sleep(.5);turn=api.call('GET','/codex/turns/'+turn['id'])
                report['turn']=turn;save()
                with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=20,trust_env=False) as client:
                    response=client.get('/codex/turns/'+turn['id']+'/events');response.raise_for_status()
                    raw=response.text;assert configuration['key'] not in raw
                    (api.root/'turn-events.sse').write_text(raw,encoding='utf8')
                    events=[json.loads(line[5:]) for line in raw.splitlines() if line.startswith('data:')]
                answer=''.join(v.get('payload',{}).get('text','') for v in events if v.get('type')=='text.delta')
                report['answer']=answer;report['expected']=nonce;report['strict_answer']=answer==nonce
                commands=[v['payload'] for v in events if v.get('type')=='item.completed' and v.get('payload',{}).get('type')=='commandExecution']
                report['actual_commands']=commands
                history=api.call('GET','/conversations/'+str(conversation['id']))
                report['persisted_answer']=any(e['type']=='message' and e['payload'].get('role')=='assistant' and e['payload'].get('content')==nonce for e in history['events'])
                assert turn['phase']=='completed' and turn['native_status']=='completed',('Actual CLI turn did not complete',turn['phase'],turn.get('error'))
                assert report['strict_answer'] and report['persisted_answer'] and turn['cleanup_verified']
                if read_file:
                    assert any(v.get('exitCode')==0 and nonce in v.get('aggregatedOutput','') for v in commands),'Actual CLI file command did not deliver the proof'
                if continuations:
                    from cli_live_continuation_checks import verify as verify_continuations
                    verify_continuations(api,call,profile,conversation,turn,nonce,report,save)
                report['passed']=True;report['cli_model_inference_verified']=True
            finally:
                original_error=sys.exc_info()[1]
                if api.proc is not None and api.proc.poll() is None:
                    try:
                        if turn is not None and turn['phase'] not in {'completed','failed','cancelled','interrupted'}:
                            report['cancel_result']=call('POST','/codex/turns/'+turn['id']+':cancel')
                        if session is not None:report['session_final']=call('DELETE','/codex/sessions/'+session['id'])
                    except BaseException as cleanup_error:
                        report['cleanup_error']={'type':type(cleanup_error).__name__,'detail':str(cleanup_error).replace(configuration['key'],'[REDACTED]')[:800]}
                        if original_error is None:raise
    except BaseException as error:
        report['passed']=False
        report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1200]}
    finally:
        report['api_exited']=api.proc is not None and api.proc.poll() is not None
        report['passed']=report['passed'] and report['api_exited'];save()
    print('LIVE_CLI_INFERENCE_RESULT '+str(api.root/'live-result.json'),flush=True)
    return 0 if report['passed'] else 1


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--runtime',choices=('windows','wsl'),default='wsl')
    parser.add_argument('--continuations',action='store_true')
    modes=parser.add_mutually_exclusive_group();modes.add_argument('--protocol-probe',action='store_true')
    modes.add_argument('--read-file',action='store_true');args=parser.parse_args()
    if args.continuations and not args.read_file:parser.error('--continuations requires --read-file')
    configuration=await source(args.source_profile_id)
    if args.protocol_probe:return await asyncio.to_thread(probe,configuration)
    return await asyncio.to_thread(verify,configuration,read_file=args.read_file,runtime=args.runtime,continuations=args.continuations)


def probe(configuration):
    """Two real provider requests; failure is evidence, never a generated reply."""
    from testsupport import SERVER
    root=Path(tempfile.mkdtemp(prefix='live-cli-protocol-',dir=SERVER.parents[1]/'tmp'))
    result={'passed':False,'mock_llm_calls':0,'cli_model_inference_verified':False,'checks':[]}
    nonce='PROTOCOL_'+uuid.uuid4().hex
    with httpx.Client(timeout=45,trust_env=False,headers={'Authorization':'Bearer '+configuration['key']}) as client:
        for endpoint,payload in [('responses',{'model':configuration['model'],'input':'Reply only '+nonce,'stream':False}),
                ('chat/completions',{'model':configuration['model'],'messages':[{'role':'user','content':'Reply only '+nonce}],'stream':False,'max_tokens':100})]:
            item={'endpoint':endpoint,'model':configuration['model'],'expected':nonce}
            try:
                response=client.post(configuration['base_url'].rstrip('/')+'/'+endpoint,json=payload)
                item.update(status=response.status_code,date=response.headers.get('date'))
                try:data=response.json()
                except ValueError:data={'body':response.text[:1000]}
                if response.status_code==200:
                    item.update({k:data[k] for k in ('id','model','usage','output','output_text','choices') if k in data})
                    answer=(data['choices'][0]['message']['content'] if endpoint=='chat/completions' else
                        ''.join(part.get('text','') for entry in data.get('output',[]) if entry.get('type')=='message'
                            for part in entry.get('content',[]) if part.get('type')=='output_text'))
                    item['strict_answer']=answer==nonce
                else:item['failure']=data
            except httpx.HTTPError as error:item['failure']={'type':type(error).__name__,'detail':str(error)}
            result['checks'].append(item)
            encoded=json.dumps(result,ensure_ascii=False,indent=2);assert configuration['key'] not in encoded
            (root/'live-result.json').write_text(encoded+'\n',encoding='utf8')
            print('ACTUAL_PROTOCOL '+endpoint+' '+str(item.get('status',item.get('failure',{}).get('type'))),flush=True)
    result['passed']=all(v.get('status')==200 and v.get('strict_answer') for v in result['checks'])
    encoded=json.dumps(result,ensure_ascii=False,indent=2);assert configuration['key'] not in encoded
    (root/'live-result.json').write_text(encoded+'\n',encoding='utf8')
    print('ACTUAL_PROTOCOL_RESULT '+str(root/'live-result.json'),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
