"""Real app-server turns, restart/fork/resume, in-flight cancellation and reuse."""
import argparse,asyncio,hashlib,json,sys,time,uuid
from pathlib import Path
import httpx
from live_llm_acceptance import source
from live_llm_shared_index_acceptance import SharedIndexServer,configure,ROOT
from live_llm_codex_terminal_provider_acceptance import Forwarder,EXE
from cli_live_continuation_checks import verify as continuations
from app.services.process_identity_native import probe

OPTIONS='''
from app.services import codex_sessions
native_command=codex_sessions.command
def primary_command(credential=None):
    argv=native_command(credential)
    return [argv[0],'--disable','multi_agent','--disable','multi_agent_v2','-c','web_search="disabled"',*argv[1:]]
codex_sessions.command=primary_command
'''

def verify(config):
    api=SharedIndexServer('live-codex-native-responses',app='live_fixture:app')
    configure(api,{'VIBEFLOW_CODEX':str(EXE)},False)
    f=api.root/'live_fixture.py';f.write_text(f.read_text(encoding='utf-8')+OPTIONS,encoding='utf-8')
    report={'passed':False,'mock_llm_calls':0,'runtime':'windows','actual_CLI_turns_started':0,'checks':[],'sessions':[]}
    def save():
        raw=json.dumps(report,ensure_ascii=False,indent=2);assert config['key'] not in raw
        (api.root/'native-result.json').write_text(raw,encoding='utf-8')
    def call(method,path,payload=None):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=150,trust_env=False) as client:
            r=client.request(method,path,json=payload)
            if r.status_code!=200:raise RuntimeError(method+' '+path+' HTTP '+str(r.status_code)+' '+r.text.replace(config['key'],'[REDACTED]')[:600])
            return r.json()
    def settled(turn):
        deadline=time.monotonic()+150
        while turn['phase'] not in {'completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'}:
            if time.monotonic()>deadline:raise TimeoutError('Actual native turn observation timed out')
            assert turn['phase']!='awaiting_input','Unexpected approval in authorized full_access case'
            time.sleep(.2);turn=call('GET','/codex/turns/'+turn['id'])
        return turn
    def events(turn):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=20,trust_env=False) as client:
            r=client.get('/codex/turns/'+turn['id']+'/events');r.raise_for_status();assert config['key'] not in r.text
            (api.root/(turn['id']+'.sse')).write_text(r.text,encoding='utf-8')
            return [json.loads(x[5:]) for x in r.text.splitlines() if x.startswith('data:')]
    def start(cid,profile,prompt):
        session=call('POST','/codex/sessions',{'case_id':api.case_id,'conversation_id':cid,'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
        report['sessions'].append(session['id']);save()
        body={'content':prompt,'client_message_id':uuid.uuid4().hex,'timeout_seconds':120,'model_options':{'effort':'low'}}
        turn=call('POST','/codex/sessions/'+session['id']+'/turns',body)
        report['actual_CLI_turns_started']+=1;save()
        return session,body,turn
    def passed(label):report['checks'].append(label);save();print('NATIVE_PASS '+label,flush=True)
    print('NATIVE_EVIDENCE '+str(api.root),flush=True)
    try:
        with Forwarder(config,api.root) as upstream:
            try:
                with api:
                    api.create_case('Actual native Responses lifecycle')
                    nonce='NATIVE_REAL_'+uuid.uuid4().hex;api.put('proof.txt',nonce+'\n')
                    profile=call('POST','/provider-profiles',{'provider':'custom','name':'Actual Responses','base_url':upstream.base,'default_model':config['model'],'api_key':config['key']})
                    conv=call('POST','/conversations',{'case_id':api.case_id,'title':'Native continuity','permission_mode':'full_access','provider_profile_id':profile['id'],'model':config['model']})
                    session,body,turn=start(conv['id'],profile,'Read proof.txt using PowerShell Get-Content once. Reply only with its verification string. Do not edit files or delegate.')
                    turn=settled(turn);report['source_turn']=turn;save()
                    assert turn['phase']=='completed' and turn['cleanup_verified'],turn
                    observed=events(turn);answer=''.join(x.get('payload',{}).get('text','') for x in observed if x.get('type')=='text.delta')
                    assert answer==nonce
                    assert any(x.get('type')=='item.completed' and x.get('payload',{}).get('type')=='commandExecution' and x['payload'].get('exitCode')==0 and nonce in x['payload'].get('aggregatedOutput','') for x in observed)
                    passed('actual_app_server_tool_read_reply_and_durable_close')
                    old_api=dict(api.birth)
                    continuations(api,call,profile,conv,turn,nonce,report,save)
                    report['previous_api_exit']={'birth':old_api,'probe':probe(old_api)}
                    assert report['previous_api_exit']['probe']['state']=='exited'
                    passed('API_restart_native_fork_original_resume_and_idempotent_replay')
                    cancelled=call('POST','/conversations',{'case_id':api.case_id,'title':'Native cancellation','permission_mode':'full_access','provider_profile_id':profile['id'],'model':config['model']})
                    arrivals=api.root/'forwarder-arrivals.jsonl'
                    before=len(arrivals.read_text().splitlines())
                    session,body,turn=start(cancelled['id'],profile,'Without tools or delegation, list integers 1 through 800, each on its own line. Do not stop early.')
                    deadline=time.monotonic()+40
                    while len(arrivals.read_text().splitlines())==before:
                        if time.monotonic()>deadline:raise TimeoutError('Actual model request did not arrive')
                        time.sleep(.02)
                    report['cancel_response']=call('POST','/codex/turns/'+turn['id']+':cancel')
                    turn=settled(call('GET','/codex/turns/'+turn['id']));report['cancelled_turn']=turn;save()
                    assert turn['phase']=='cancelled' and turn['cleanup_verified'],turn
                    assert call('POST','/codex/sessions/'+session['id']+'/turns',body)['id']==turn['id']
                    assert call('GET',f'/codex/conversations/{cancelled["id"]}/activity')['phase']=='idle'
                    passed('in_flight_real_model_cancel_verified_close_and_replay')
                    marker='AFTER_CANCEL_'+uuid.uuid4().hex
                    session,body,turn=start(cancelled['id'],profile,'Do not use tools or delegate. Reply exactly '+marker)
                    turn=settled(turn);report['after_cancel_turn']=turn;save()
                    assert turn['phase']=='completed' and turn['cleanup_verified']
                    assert ''.join(x.get('payload',{}).get('text','') for x in events(turn) if x.get('type')=='text.delta')==marker
                    passed('fresh_actual_model_turn_after_cancellation')
                    # Provider work may finish after client cancellation; retain its
                    # real HTTP receipt instead of claiming cancellation saved tokens.
                    deadline=time.monotonic()+180
                    while len(upstream.receipts)<6:
                        if time.monotonic()>deadline:raise TimeoutError('Real upstream receipt still pending')
                        time.sleep(.2)
                    report['upstream']=upstream.receipts
                    assert all(x.get('status')==200 for x in upstream.receipts)
                    report['passed']=True
            finally:
                report['upstream']=upstream.receipts
                if hasattr(api,'birth'):report['api_exit']={'birth':api.birth,'probe':probe(api.birth)}
                save()
    except BaseException as error:
        report.update(passed=False,failure_type=type(error).__name__,failure=str(error).replace(config['key'],'[REDACTED]')[:1200]);save()
    print('NATIVE_RESULT '+str(report['passed']),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source-profile-id',type=int,required=True);p.add_argument('--review',type=Path,required=True);args=p.parse_args()
    gate=json.loads(args.review.read_text(encoding='utf-8'))
    assert gate['entry']=='platform/server/live_llm_codex_native_responses_acceptance.py' and gate['classification']=='real_llm'
    for path,digest in gate['sources'].items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    assert hashlib.sha256(EXE.read_bytes()).hexdigest()==gate['codex_exe_sha256']
    sys.exit(verify(asyncio.run(source(args.source_profile_id))))
