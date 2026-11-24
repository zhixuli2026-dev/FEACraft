"""Actual installed Claude protocol client: tool permission, resume, fork, interrupt."""
import argparse,asyncio,hashlib,json,os,sys,tempfile,time,uuid
from pathlib import Path
from types import SimpleNamespace
from live_llm_acceptance import source
from live_llm_claude_provider_acceptance import Forwarder,EXE,ROOT
from app.config import get_settings
from app.services.claude_stdio import ClaudeStdioClient,command
from app.services.terminal_credentials import descriptor,environment
from app.services.process_identity_native import probe
from app.services.process_stop import validate


async def verify(config):
    root=Path(tempfile.mkdtemp(prefix='live-claude-stdio-',dir=ROOT/'tmp'))
    os.environ.update(VIBEFLOW_STORAGE_FS_ROOT=str(root/'cases'),VIBEFLOW_CODEX_STATE_ROOT=str(root/'state'))
    get_settings.cache_clear()
    case=root/'cases'/'proof';case.mkdir(parents=True)
    nonce='CLAUDE_NATIVE_'+uuid.uuid4().hex;(case/'proof.txt').write_text(nonce,encoding='utf-8')
    report={'passed':False,'mock_llm_calls':0,'actual_model_turns':0,'checks':[],'clients':[]}
    def save():
        raw=json.dumps(report,ensure_ascii=False,indent=2);assert config['key'] not in raw
        (root/'stdio-result.json').write_text(raw,encoding='utf-8')
    def passed(label):report['checks'].append(label);save();print('CLAUDE_STDIO_PASS '+label,flush=True)
    clients=[]
    async def create(env,resume=None,fork=False):
        row={'resume':resume,'fork':fork};report['clients'].append(row);save()
        async def registered(identity):row['root']=identity;save()
        async def bridged(identity):row['bridge']=identity;save()
        argv=command(EXE,config['model'],'approve_writes',resume=resume,fork=fork,tools=['Read','Write'])
        argv+=['--disable-slash-commands','--system-prompt','Follow the requested file operation exactly. Use only Read or Write when requested. Do not delegate. Return concise exact answers.']
        client=ClaudeStdioClient(argv,env,cwd=case,before_release=registered,before_start=bridged)
        clients.append((client,row));await client.start();row['initialized']=True;save()
        return client
    async def consume(client,label,permission=None):
        events=[]
        try:
            async with asyncio.timeout(150):
                while True:
                    event=await client.next_event();events.append(event)
                    if event['type']=='control_request':
                        assert permission is not None,'Unexpected actual approval'
                        req=event['request'];assert req['tool_name']=='Write'
                        allowed=permission(req)
                        await client.respond_to_permission(event['request_id'],allow=allowed,message='Do not write this file; acknowledge denial without another tool.')
                        try:await client.respond_to_permission(event['request_id'],allow=allowed)
                        except ValueError:pass
                        else:raise AssertionError('Approval was reusable')
                    if event['type']=='result':return event,events
        finally:
            raw=json.dumps(events,ensure_ascii=False,indent=2);assert config['key'] not in raw
            (root/(label+'-events.json')).write_text(raw,encoding='utf-8')
    async def send(client,prompt):
        await client.send_message(prompt,str(uuid.uuid4()));report['actual_model_turns']+=1;save()
    async def close(client):
        row=next(r for c,r in clients if c is client)
        stop=await client.close();row['stop']=stop
        if 'root' in row:
            validate(stop,row['root']);assert stop['verified']
            row['root_probe']=probe(row['root']['host']);assert row['root_probe']['state']=='exited'
        if 'bridge' in row:
            row['bridge_probe']=probe(row['bridge']);assert row['bridge_probe']['state']=='exited'
        save()
    print('CLAUDE_STDIO_EVIDENCE '+str(root),flush=True)
    try:
        with Forwarder(config,root) as upstream:
            try:
                selected=descriptor(SimpleNamespace(id=26,user_id=1,provider='anthropic',base_url=upstream.base,
                    default_model=config['model'],key_ciphertext='isolated-revision'), 'claude')
                env=environment(selected,config['key'])
                client=await create(env)
                await send(client,'Use Read once to read proof.txt. Reply only with its exact content.')
                result,events=await consume(client,'read')
                assert not result['is_error'] and result['result']==nonce
                assert any(e['type']=='assistant' and any(c.get('type')=='tool_use' and c.get('name')=='Read' for c in e['message']['content']) for e in events)
                assert any(e['type']=='user' and nonce in json.dumps(e.get('message',{})) for e in events)
                assert any(e['type']=='stream_event' for e in events)
                original=client.session_id;report['original_session_id']=original
                passed('actual_Read_tool_stream_events_and_exact_answer')
                await send(client,'Use Write once to create allowed.txt with exactly '+nonce+'. Reply DONE after the write.')
                def allow(req):
                    assert Path(req['input']['file_path']).resolve()==(case/'allowed.txt').resolve()
                    assert req['input']['content']==nonce
                    return True
                result,events=await consume(client,'allow',allow)
                assert not result['is_error'] and (case/'allowed.txt').read_text(encoding='utf-8')==nonce
                assert any(e['type']=='control_request' for e in events)
                passed('actual_Write_permission_allowed_once_and_file_changed')
                await send(client,'Use Write to create denied.txt with content forbidden. If permission is denied, do not retry or use any other tool; reply DENIED.')
                def deny(req):
                    assert Path(req['input']['file_path']).resolve()==(case/'denied.txt').resolve()
                    return False
                result,events=await consume(client,'deny',deny)
                assert any(e['type']=='control_request' for e in events) and not (case/'denied.txt').exists()
                passed('actual_Write_permission_denied_and_no_file_created')
                before=upstream.count
                await send(client,'Use no tools. List every integer from 1 through 800, each on its own line.')
                deadline=time.monotonic()+40
                while upstream.count==before:
                    if time.monotonic()>deadline:raise TimeoutError('Actual model request did not start')
                    await asyncio.sleep(.02)
                report['interrupt_ack']=await client.interrupt();save()
                result,events=await consume(client,'interrupt');report['interrupt_result']=result;save()
                assert result.get('is_error') or any('interrupt' in json.dumps(e).lower() for e in events)
                passed('actual_in_flight_interrupt_and_terminal_result')
                marker='AFTER_INTERRUPT_'+uuid.uuid4().hex
                await send(client,'Use no tools. Reply exactly '+marker)
                result,events=await consume(client,'after-interrupt')
                assert not result['is_error'] and result['result']==marker and client.session_id==original
                passed('same_process_real_turn_after_interrupt')
                await close(client)
                for label,fork in [('resume',False),('fork',True)]:
                    client=await create(env,original,fork)
                    await send(client,'Without tools, repeat only the original verification string read from proof.txt at the start of this conversation.')
                    result,events=await consume(client,label)
                    assert not result['is_error'] and result['result']==nonce
                    assert (client.session_id!=original)==fork
                    assert not any(e['type']=='assistant' and any(c.get('type')=='tool_use' for c in e['message']['content']) for e in events)
                    report[label+'_session_id']=client.session_id;await close(client)
                    passed('actual_native_'+label+'_after_process_restart_preserves_history')
                deadline=time.monotonic()+180
                while len(upstream.receipts)<upstream.count:
                    if time.monotonic()>deadline:raise TimeoutError('Upstream receipt pending')
                    await asyncio.sleep(.2)
                assert all(r.get('status')==200 for r in upstream.receipts),upstream.receipts
                report['passed']=True
            finally:
                for client,row in clients:
                    if 'stop' not in row:await close(client)
                report['upstream']=upstream.receipts;save()
    except BaseException as error:
        report.update(passed=False,failure_type=type(error).__name__,failure=str(error).replace(config['key'],'[REDACTED]')[:1200]);save()
    print('CLAUDE_STDIO_RESULT '+str(report['passed']),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source-profile-id',type=int,required=True);p.add_argument('--review',type=Path,required=True);args=p.parse_args()
    gate=json.loads(args.review.read_text(encoding='utf-8'))
    assert gate['classification']=='real_llm' and gate['entry']=='platform/server/live_llm_claude_stdio_acceptance.py'
    for name,h in gate['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==h,name
    assert hashlib.sha256(EXE.read_bytes()).hexdigest()==gate['claude_exe_sha256']
    sys.exit(asyncio.run(verify(asyncio.run(source(args.source_profile_id)))))
