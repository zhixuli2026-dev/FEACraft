"""Actual Codex Ask, Plan and write permissions with the authorized DeepSeek profile."""
import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

import httpx

from cli_managed_session_checks import FIXTURE as MANAGED
from live_llm_acceptance import FIXTURE as LIVE, source
from live_llm_cli_inference_acceptance import FIXTURE as INFERENCE
from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer, SERVER


FIXTURE = r'''
from app import storage
from app.services.codex_stdio import CodexStdioClient
from app.services.codex_permissions import READ_ONLY

original_policy_request = CodexStdioClient.request
async def inspect_policy(self, method, params=None, *args, **kwargs):
    result = await original_policy_request(self, method, params, *args, **kwargs)
    if method in {'thread/start','thread/resume','turn/start'}:
        params = params or {}
        row = {'method':method,'sandbox':params.get('sandbox'),'approval':params.get('approvalPolicy'),
               'turn_sandbox':params.get('sandboxPolicy'),
               'collaboration':(params.get('collaborationMode') or {}).get('mode'),
               'effective_sandbox':result.get('sandbox'), 'effective_approval':result.get('approvalPolicy'),
               'thread_id':(result.get('thread') or {}).get('id') or params.get('threadId')}
        with (_base/'policy-rpc.jsonl').open('a',encoding='utf8') as log:
            log.write(json.dumps(row,ensure_ascii=False)+'\n')
    return result
CodexStdioClient.request = inspect_policy

original_fail = CodexStdioClient._fail
def inspect_protocol_failure(self,error):
    if isinstance(error,ValueError):
        import traceback
        row = {'type':type(error).__name__,'message':str(error)[:400],
               'trace':[(Path(frame.filename).name,frame.lineno,frame.name) for frame in traceback.extract_tb(error.__traceback__)[-8:]]}
        with (_base/'policy-transport-failures.jsonl').open('a',encoding='utf8') as log:
            log.write(json.dumps(row,ensure_ascii=False)+'\n')
    return original_fail(self,error)
CodexStdioClient._fail = inspect_protocol_failure

original_permission_event = codex_turns.Execution.event
async def inspect_event(self,event):
    try:
        return await original_permission_event(self,event)
    except Exception as error:
        import traceback
        row = {'method':event.get('method'),'type':type(error).__name__,
               'message':self.projection.secrets.clean(str(error)),
               'trace':[(Path(frame.filename).name,frame.lineno,frame.name) for frame in traceback.extract_tb(error.__traceback__)[-8:]]}
        with (_base/'policy-event-failures.jsonl').open('a',encoding='utf8') as log:
            log.write(json.dumps(row,ensure_ascii=False)+'\n')
        raise
codex_turns.Execution.event = inspect_event

original_start = sessions.Session.start
async def inspect_session_start(self):
    try:
        return await original_start(self)
    except Exception as error:
        import traceback
        row = {'type':type(error).__name__,'message':str(error).replace(self.secret or '\0','[REDACTED]')[:400],
               'trace':[(Path(frame.filename).name,frame.lineno,frame.name) for frame in traceback.extract_tb(error.__traceback__)[-8:]]}
        with (_base/'policy-session-failures.jsonl').open('a',encoding='utf8') as log:
            log.write(json.dumps(row,ensure_ascii=False)+'\n')
        raise
sessions.Session.start = inspect_session_start

@app.post('/api/v1/test/codex-permission-stage/{identifier}')
async def introduce_stage_difference(identifier:str,user=Depends(get_current_user)):
    row = await sessions.row(identifier,user.id)
    selected = json.loads(row.profile_json)
    assert selected['permission_mode'] in READ_ONLY and selected['staging_prefix']
    stage = storage._fs_path(selected['staging_prefix'],'.').resolve()
    assert str(stage)==selected['cwd'] and stage.is_relative_to(storage._fs_root().resolve()/'.staging')
    target = stage/'Ref'/'f28-stage-only.txt'
    target.parent.mkdir(exist_ok=True)
    target.write_text('test difference confined to the private stage',encoding='utf8')
    return {'present':target.is_file(),'stage':selected['staging_prefix']}

@app.post('/api/v1/test/codex-permission-exit')
async def exit_owned_api(user=Depends(get_current_user)):
    os._exit(79)
'''

TERMINAL = {'completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'}
MODES = ('ask','plan','escalation','full_access')
COLD_MODE = 'cold_readonly'


CASE_NAME = 'FF-F28 actual native permission modes'


def verify(configuration, runtime, modes, store=None, database_url=None, dist=None, case_tool=False):
    api=IsolatedServer('live-codex-permissions',app='permission_fixture:app',database_url=database_url)
    settings=store.settings() if store else {}
    static = ('\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='+repr(str(dist))+',html=True))\n') if dist else ''
    (api.root/'permission_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+LIVE+MANAGED+INFERENCE+FIXTURE+static,
                                                  encoding='utf8')
    report={'passed':False,'runtime':runtime,'storage':'s3' if store else 'fs','database':'postgresql' if database_url else 'sqlite',
            'provider':'api.deepseek.com','model':configuration['model'],'mock_model_calls':0,'scenarios':{},'failures':{}}
    print('LIVE_CODEX_PERMISSIONS_EVIDENCE',api.root,flush=True)

    def call(method,path,body=None,*,status=200,raw=False,retry_inventory=False):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=170,trust_env=False) as client:
            for attempt in range(3):
                response=client.request(method,path,json=body)
                if not retry_inventory or response.status_code!=409:
                    break
                detail=response.json().get('detail')
                if not isinstance(detail,dict) or detail.get('code')!='cli_user_storage_unavailable' or attempt==2:
                    break
                report.setdefault('inventory_admission_retries',[]).append({'path':path,'status':409})
                time.sleep(.4)
            assert response.status_code==status,(method,path,response.status_code,response.text[:1200])
            return response.text if raw else response.json()

    def events(turn_id,label):
        raw=call('GET',f'/codex/turns/{turn_id}/events',raw=True)
        assert configuration['key'] not in raw
        (api.root/(label+'-events.sse')).write_text(raw,encoding='utf8')
        return [json.loads(line[5:]) for line in raw.split('\n') if line.startswith('data:')]

    def stable_turn(cid,prompt,label,*,resume=False,deny_escalation=False,difference=False,exit_on_approval=False,
                    allowed_windows_command=None):
        selected=call('POST','/codex/sessions',{'case_id':api.case_id,'conversation_id':cid,'runtime':runtime,
                      'provider_profile_id':profile['id'],'provider_wire_api':'responses'},retry_inventory=True)
        session=selected['id']
        denied=None
        injected=None
        try:
            if store and difference:
                injected=call('POST',f'/test/codex-permission-stage/{session}')
                assert injected['present']
            turn=call('POST',f'/codex/sessions/{session}/turns',{'content':prompt,'client_message_id':uuid.uuid4().hex,
                      'timeout_seconds':145,'resume':resume})
            filename=api.root/(label+'-turn.json')
            filename.write_text(json.dumps(turn,ensure_ascii=False,indent=2),encoding='utf8')
            route=f'/codex/turns/{turn["id"]}'
            until=time.monotonic()+205
            while turn['phase'] not in TERMINAL:
                if turn['phase']=='awaiting_input':
                    for approval in call('GET',route+'/approvals'):
                        if approval['phase']!='pending':
                            continue
                        reply=f'{route}/approvals/{approval["id"]}:respond'
                        if approval['method']=='item/tool/requestUserInput':
                            answers={question['id']:[question['options'][0]['label'] if question.get('options') else '按现有文件继续']
                                     for question in approval['params']['questions']}
                            call('POST',reply,{'answers':answers})
                        elif allowed_windows_command is not None:
                            powershell=Path(os.environ['SYSTEMROOT'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
                            params=approval['params']
                            command=params.get('command','')
                            wrapped=re.fullmatch(r'"([^"]+)" -NoProfile -Command (.+)',command)
                            if wrapped:
                                assert Path(wrapped[1]).resolve()==powershell.resolve(),wrapped[1]
                                command=wrapped[2]
                                if command[0]==command[-1] and command[0] in "\"'":
                                    command=command[1:-1]
                                command=re.sub(r"\\+'", "'", command)
                            cwd=Path(params.get('cwd','')).resolve()
                            actions=params.get('commandActions',[])
                            assert (approval['method']=='item/commandExecution/requestApproval' and command==allowed_windows_command
                                    and len(actions)==1 and actions[0].get('command')==allowed_windows_command),params
                            assert cwd.is_relative_to((api.root/'files').resolve()),cwd
                            call('POST',reply,{'decision':'accept'})
                            allowed_windows_command=None
                        else:
                            assert deny_escalation and denied is None, ('Unexpected native approval',label,approval['method'],approval['params'])
                            assert approval['method']=='item/commandExecution/requestApproval',approval['method']
                            assert 'f28-escalation.txt' in approval['params'].get('command',''),approval['params']
                            call('POST',reply,{'decision':'accept'},status=403)
                            if exit_on_approval:
                                email=api.call('GET','/auth/me')['email']
                                try:
                                    call('POST','/test/codex-permission-exit')
                                except httpx.TransportError:
                                    pass
                                else:
                                    raise AssertionError('Actual isolated API exit did not interrupt the request')
                                assert api.proc.wait(timeout=15)==79
                                api.__exit__(RuntimeError,None,None)
                                api.__enter__()
                                api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
                                settled=call('POST',route+':reconcile-staging')
                                assert settled['staging_reconciled'] and settled['execution_lock_recovered'],settled
                                assert settled['turn']['phase']=='interrupted' and settled['turn']['cleanup_verified']
                                assert call('POST',route+':reconcile-staging')==settled
                                assert call('GET',route+'/approvals')[0]['phase']=='expired'
                                assert not (api.root/'files'/injected['stage']).exists()
                                api.call('GET',api.prefix+'/raw?path=Ref/f28-stage-only.txt',expect=404)
                                return settled['turn'],events(turn['id'],label),{
                                    'server_denied_grant':True,'actual_api_exit':79,'staged_difference_discarded':True}
                            offered=approval['params'].get('availableDecisions') or ['decline','cancel']
                            choice='decline' if 'decline' in offered else 'cancel'
                            call('POST',reply,{'decision':choice})
                            denied={'method':approval['method'],'server_denied_grant':True,'responded':choice}
                assert time.monotonic()<until, (label,turn['phase'])
                time.sleep(.4)
                turn=call('GET',route)
            filename.write_text(json.dumps(turn,ensure_ascii=False,indent=2),encoding='utf8')
            captured=events(turn['id'],label)
            assert turn['cleanup_verified'] is True,turn
            assert turn['phase']=='completed' or denied and turn['phase'] in {'cancelled','interrupted'},turn
            if store and difference:
                assert not (api.root/'files'/injected['stage']).exists()
                api.call('GET',api.prefix+'/raw?path=Ref/f28-stage-only.txt',expect=404)
            return turn,captured,denied
        finally:
            closed=call('DELETE',f'/codex/sessions/{session}')
            assert closed['cleanup_verified'],closed['phase']

    def conversation(mode):
        return api.call('POST','/conversations',{'case_id':api.case_id,'title':f'F28 {runtime} {mode}',
                        'permission_mode':mode,'provider_profile_id':profile['id'],'model':configuration['model']})['id']

    def command_events(captured):
        return [event['payload'] for event in captured if event['type']=='item.completed'
                and event.get('payload',{}).get('type') in {'commandExecution','fileChange'}]

    def read_command(path):
        return f'cat {path}' if runtime=='wsl' else f'Get-Content -LiteralPath {path}'

    def write_command(path, marker):
        return f"printf '%s' '{marker}' > {path}" if runtime=='wsl' else f"Set-Content -LiteralPath {path} -Value '{marker}' -NoNewline"

    def approve_plan(cid):
        if dist is None:
            return call('PATCH',f'/conversations/{cid}/permission',{'permission_mode':'approve_writes'})
        from playwright.sync_api import expect, sync_playwright
        from live_llm_file_proposal_history_acceptance import use_case

        user=api.call('GET','/auth/me')
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(channel='msedge')
            try:
                page=browser.new_page(viewport={'width':1450,'height':920},locale='zh-CN')
                active_key=f'vf-chat-backend:{user["id"]}:{cid}'
                page.add_init_script('localStorage.setItem("vf_token",'+json.dumps(api.token)+');'
                                     'localStorage.setItem('+json.dumps(active_key)+','+json.dumps(runtime)+');')
                use_case(page,api,CASE_NAME)
                page.get_by_test_id('agent-conversation').select_option(str(cid),timeout=25000)
                expect(page.get_by_test_id('native-chat')).to_have_count(1,timeout=15000)
                selector=page.get_by_test_id('agent-permission-mode')
                expect(selector).to_have_value('plan')
                page.screenshot(path=api.root/'native-plan-before-approval.png')
                with page.expect_response(lambda value: value.request.method=='PATCH' and
                                          value.url.endswith(f'/conversations/{cid}/permission')) as pending:
                    page.get_by_test_id('approve-plan').click(timeout=20000)
                response=pending.value
                assert response.status==200,response.status
                expect(selector).to_have_value('approve_writes')
                expect(selector.locator('option:checked')).to_have_text('工作区可写')
                expect(page.get_by_test_id('profile-status')).to_contain_text('超出沙箱的操作需审批')
                page.screenshot(path=api.root/'native-plan-approved.png')
                return response.json()
            finally:
                browser.close()

    def exercise(mode):
        cid=conversation('ask' if mode in {'escalation',COLD_MODE} else mode)
        marker='POLICY_'+uuid.uuid4().hex[:12]
        if mode in {'escalation',COLD_MODE}:
            path='Ref/f28-escalation.txt'
            prompt=f'隔离权限验收。只调用一次命令工具执行 `{write_command(path,marker)}`，必须设置 sandbox_permissions 为 require_escalated，并说明验证理由。收到拒绝就停止，请勿改用其它命令。'
            turn,_,denial=stable_turn(cid,prompt,mode,deny_escalation=True,difference=mode==COLD_MODE,
                                     exit_on_approval=mode==COLD_MODE)
            assert denial,turn
            api.call('GET',api.prefix+'/raw?path='+path,expect=404)
            return {'passed':True,'turn':turn,'denial':denial}
        if mode=='full_access':
            path='Ref/f28-full-access.txt'
            turn,captured,_=stable_turn(cid,f'实际在隔离算例中执行一次 `{write_command(path,marker)}`。完成后简短回复。',mode)
            assert api.call('GET',api.prefix+'/raw?path='+path,raw=True).decode()==marker
            assert command_events(captured),captured[-4:]
            assert not call('GET',f'/codex/turns/{turn["id"]}/approvals')
            return {'passed':True,'turn':turn,'actual_write':True}
        path=f'Ref/f28-{mode}-proof.txt'
        api.put(path,marker+'\n')
        instructions='读到验证字符串后列出后续写入两个步骤，现在保持只读。' if mode=='plan' else '只回复读到的验证字符串。'
        if runtime=='windows' or case_tool:
            prompt=(f'请只调用 vibeflow_read_case_file 一次，path 参数为 {path}，读取实际算例文件。'
                    '文件位置已经确定，无需列目录、搜索、调用终端或请求权限。'+instructions)
        else:
            prompt=f'隔离验收，请调用命令工具执行 `{read_command(path)}` 读取这个实际文件。保留工具的默认沙箱 sandbox_permissions=use_default，请勿请求权限升级。{instructions}'
        turn,captured,_=stable_turn(cid,prompt,mode,difference=True)
        if runtime=='windows' or case_tool:
            assert any(v['type']=='item.completed' and v['payload'].get('type')=='dynamicToolCall'
                       and v['payload'].get('tool')=='vibeflow_read_case_file' and v['payload'].get('success') is True
                       for v in captured),captured[-6:]
        else:
            commands=command_events(captured)
            assert any(v.get('exitCode')==0 and marker in v.get('aggregatedOutput','') for v in commands),commands
        output=''.join(v['payload']['text'] for v in captured if v['type']=='text.delta')
        output+='\n'.join(v['payload'].get('text','') for v in captured
                          if v['type']=='item.completed' and v['payload'].get('type')=='plan')
        assert marker in output, output
        assert api.call('GET',api.prefix+'/raw?path='+path,raw=True).decode()==marker+'\n'
        assert call('GET',f'/conversations/{cid}')['permission_mode']==mode
        if mode=='ask':
            return {'passed':True,'turn':turn,'actual_read':True,'staged_difference_discarded':bool(store)}
        target='Ref/f28-after-plan.txt'
        api.call('GET',api.prefix+'/raw?path='+target,expect=404)
        approved=approve_plan(cid)
        assert approved['permission_mode']=='approve_writes'
        command=write_command(target,marker)
        next_turn,after,_=stable_turn(cid,f'计划已经由用户批准。现在实际执行一次 `{command}`，完成后简短回复。',
                                      'after-plan',resume=True,allowed_windows_command=command if runtime=='windows' else None)
        assert turn['thread_id']==next_turn['thread_id']
        assert api.call('GET',api.prefix+'/raw?path='+target,raw=True).decode()==marker
        assert command_events(after),after[-4:]
        return {'passed':True,'turn':turn,'after_approval':next_turn,'actual_read':True,'same_thread':True,
                'actual_write_after_approval':True,'browser_approval':dist is not None,
                'staged_difference_discarded':bool(store)}

    try:
        with api:
            api.create_case(CASE_NAME)
            if runtime=='wsl':
                call('POST','/test/prepare-cli-inference')
            profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual Codex permission acceptance',
                'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
            for mode in modes:
                try:
                    report['scenarios'][mode]=exercise(mode)
                    print('LIVE_CODEX_PERMISSIONS_PASS',runtime,mode,flush=True)
                except Exception as error:
                    details=str(error).replace(configuration['key'],'[REDACTED]')[:1500]
                    report['failures'][mode]={'type':type(error).__name__,'detail':details}
                    print('LIVE_CODEX_PERMISSIONS_FAILED',runtime,mode,report['failures'][mode],flush=True)
        report['passed']=not report['failures']
    finally:
        trace=api.root/'policy-rpc.jsonl'
        report['policy']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        (api.root/'live-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if report['passed'] else 1


async def run(args):
    configuration=await source(args.source_profile_id)
    store=nullcontext()
    if args.s3:
        from s3testsupport import ObjectStore
        store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',authenticated=True)
    async with disposable_database(args.postgres) as database_url:
        with store as active:
            return await asyncio.to_thread(verify,configuration,args.runtime,args.modes.split(','),active,database_url,args.dist,args.case_tool)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--runtime',choices=('windows','wsl'),required=True)
    parser.add_argument('--modes',default=','.join(MODES))
    parser.add_argument('--s3',action='store_true')
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--dist',type=lambda path:Path(path).resolve(strict=True))
    parser.add_argument('--case-tool',action='store_true')
    options=parser.parse_args()
    modes=options.modes.split(',')
    if len(set(modes))!=len(modes) or not set(modes)<={*MODES,COLD_MODE} or COLD_MODE in modes and not options.s3:
        parser.error('Select unique native permission scenarios')
    sys.exit(asyncio.run(run(options)))
