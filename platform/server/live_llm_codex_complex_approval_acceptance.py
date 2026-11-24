"""Validate installed Codex interaction cards against the authorized DeepSeek service."""
import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import httpx
from cli_managed_session_checks import FIXTURE as MANAGED
from live_llm_acceptance import FIXTURE as LIVE, source
from live_llm_cli_inference_acceptance import FIXTURE as INFERENCE
from testsupport import IsolatedServer


CASE_NAME = 'FF-F30 actual Codex interaction'
FEATURE = '''
original_command = sessions.command
original_native_command = sessions.native_command
def commands_with_permission_tool(*args, **kwargs):
    argv = original_command(*args, **kwargs)
    return [argv[0], '-c', 'features.request_permissions_tool=true', *argv[1:]]
def native_commands_with_permission_tool(*args, **kwargs):
    program, argv = original_native_command(*args, **kwargs)
    return program, [argv[0], '-c', 'features.request_permissions_tool=true', *argv[1:]]
sessions.command = commands_with_permission_tool
sessions.native_command = native_commands_with_permission_tool
'''


def browser(settings):
    from playwright.sync_api import expect, sync_playwright
    from live_llm_file_proposal_history_acceptance import use_case

    root=Path(settings['root'])
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        try:
            page = edge.new_page(viewport={'width':1480,'height':1000},locale='zh-CN')
            active_key = f'vf-chat-backend:{settings["user"]}:{settings["conversation"]}'
            values = {'vf_token':settings['token'],active_key:settings['runtime']}
            page.add_init_script('for(const [key,value] of Object.entries('+json.dumps(values)+'))localStorage.setItem(key,value);')
            use_case(page,SimpleNamespace(base=settings['base']),CASE_NAME)
            page.get_by_test_id('agent-conversation').select_option(str(settings['conversation']),timeout=25000)
            card = page.get_by_test_id('native-approval')
            expect(card).to_be_visible(timeout=30000)
            expect(page.get_by_test_id('native-input')).to_be_hidden()
            expect(page.get_by_test_id('native-stop')).to_be_enabled()
            interaction=settings['interaction']
            result={'passed':True,'edge':edge.version,'approval_visible_with_stop':True}
            if interaction=='questions':
                expect(card.get_by_text('需要你的回答',exact=True)).to_be_in_viewport()
                suggestion=card.get_by_role('button',name='校验',exact=True)
                suggestion.click()
                expect(suggestion).to_have_attribute('aria-pressed','true')
                expect(card.get_by_role('button',name='初稿',exact=True)).to_be_visible()
                card.locator('input[id$="-case_label"]').fill('F30-Custom-Label')
                password=card.locator('input[id$="-test_password"]')
                expect(password).to_have_attribute('type','password')
                expect(password).to_have_attribute('autocomplete','off')
                expect(card.get_by_role('button',name='输入测试值',exact=True)).to_have_count(0)
                page.get_by_test_id('native-chat').screenshot(path=str(root/'question-card.png'))
                password.scroll_into_view_if_needed()
                expect(password).to_be_in_viewport()
                page.get_by_test_id('native-chat').screenshot(path=str(root/'question-secret-field.png'))
                password.fill(settings['secret'])
                action='提交回答'
                result.update(clicked_native_suggestion=True,free_input=True,password_masked=True)
            elif interaction=='permissions':
                expect(card.get_by_text('请求额外权限',exact=True)).to_be_in_viewport()
                expect(card.get_by_role('checkbox',name='网络访问',exact=True)).to_be_disabled()
                expect(card.get_by_role('checkbox',name='文件系统访问',exact=True)).to_have_count(0)
                expect(card.get_by_role('button',name='允许所选权限',exact=True)).to_be_disabled()
                action='拒绝权限'
                result['read_only_grant_controls_disabled']=True
                page.get_by_test_id('native-chat').screenshot(path=str(root/'permissions-card.png'))
            else:
                expect(card.get_by_text('等待执行批准',exact=True)).to_be_in_viewport()
                accept=card.get_by_role('button',name='允许一次',exact=True)
                if accept.count():
                    expect(accept).to_be_disabled()
                action=settings['decision_label']
                result['file_approval_card_rejected']=True
                page.get_by_test_id('native-chat').screenshot(path=str(root/'file-approval-card.png'))
            with page.expect_response(lambda response: response.request.method=='POST' and response.url.endswith(':respond')) as observed:
                card.get_by_role('button',name=action,exact=True).click()
            assert observed.value.status == 200, observed.value.status
            expect(card).to_have_count(0,timeout=30000)
            expect(page.get_by_test_id('native-input')).to_be_visible()
            result['composer_visible_after_submission']=True
            if interaction=='questions':
                expect(page.get_by_test_id('native-chat')).to_contain_text('F30-Custom-Label',timeout=75000)
                assert settings['secret'] not in page.locator('body').inner_text()
                result['password_absent_from_page']=True
            (root/'browser-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
        finally:
            edge.close()


def verify(configuration, runtime, dist=None, browser_python=None, interaction='questions'):
    api = IsolatedServer('live-f30-codex-interaction', app='question_fixture:app')
    static = ('\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='+repr(str(dist))+',html=True))\n') if dist else ''
    fixture = FEATURE if interaction=='permissions' else ''
    (api.root/'question_fixture.py').write_text(LIVE+MANAGED+INFERENCE+fixture+static, encoding='utf8')
    report = {'runtime': runtime, 'interaction': interaction, 'provider': 'api.deepseek.com',
              'model': configuration['model'], 'mock_llm_calls': 0}
    print('LIVE_F30_CODEX_INTERACTION', api.root, flush=True)

    def call(method, path, body=None, *, status=200, raw=False):
        with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+api.token},
                          trust_env=False, timeout=110) as client:
            reply = client.request(method, path, json=body)
            assert reply.status_code == status, (method, path, reply.status_code, reply.text.replace(configuration['key'], '[REDACTED]')[:300])
            return reply.text if raw else reply.json()

    questions = [
        {'id':'run_mode','header':'运行模式','question':'选择本次运行模式。','options':[
            {'label':'校验','description':'只校验参数。'},{'label':'汇总','description':'生成汇总提纲。'}]},
        {'id':'case_label','header':'算例标签','question':'任选一个建议标签，或输入你自己的标签。','isOther':True,'options':[
            {'label':'初稿','description':'适用于初稿。'},{'label':'复核','description':'适用于复核。'}]},
        {'id':'test_password','header':'测试密码','question':'请输入这次隔离测试专用的临时值。','isSecret':True,'options':[
            {'label':'输入测试值','description':'请使用私有字段输入。'},{'label':'跳过测试值','description':'仅用于工具协议的备选项。'}]},
    ]
    prompt = ('这是原生交互工具的实际验收。请现在调用 request_user_input，一次提出下面三个明确的问题。'
              '请实际调用工具，不要仅用文字列问题。严格保留每个 id。第二题允许自填 isOther=true，第三题必须设置 isSecret=true。'
              '收到用户提交后，用普通文本回复模式与标签，并确认第三项已接收，切勿显示第三项的内容。'
              '工具参数为 '+json.dumps({'questions': questions}, ensure_ascii=False))
    if interaction=='file':
        prompt = ('使用原生 apply_patch 工具尝试对隔离算例文件提出下面的实际更改。'
                  '如果只提供命令工具，可以在当前 WSL 工作目录通过 apply_patch 单独执行下面的补丁。'
                  '如果工具需要审批就等我决定，如果拒绝就停止并说明没有写入。\n'
                  '*** Begin Patch\n*** Update File: Ref/f30-approval.txt\n@@\n-ORIGINAL\n+NEW_VALUE\n*** End Patch')
    elif interaction=='permissions':
        prompt = ('请现在实际调用 request_permissions 原生工具，请求当前回合临时网络权限。'
                  '只请求权限并等待用户审批。无论结果是什么，都不要访问网络或执行其他命令，只回复权限是否获准。')
    secret = 'F30_TEST_ONLY_'+uuid.uuid4().hex
    session_id = None
    try:
        with api:
            try:
                api.create_case(CASE_NAME)
                if interaction=='file':
                    api.put('Ref/f30-approval.txt','ORIGINAL\n')
                if runtime=='wsl':
                    call('POST','/test/prepare-cli-inference')
                profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'FF-F30 real acceptance',
                    'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
                conversation = api.call('POST','/conversations',{'case_id':api.case_id,'title':'Native input form',
                    'permission_mode':'plan' if interaction=='questions' else 'ask',
                    'provider_profile_id':profile['id'],'model':configuration['model']})
                session = call('POST','/codex/sessions',{'case_id':api.case_id,'conversation_id':conversation['id'],
                    'runtime':runtime,'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
                session_id = session['id']
                turn = call('POST',f'/codex/sessions/{session_id}/turns',{'content':prompt,
                    'client_message_id':uuid.uuid4().hex,'timeout_seconds':170})
                report['turn'] = turn['id']
                path = f'/codex/turns/{turn["id"]}'
                deadline = time.monotonic() + 190
                offered = None
                while time.monotonic() < deadline:
                    offers = [value for value in call('GET',path+'/approvals') if value['phase']=='pending']
                    if offers:
                        offered, = offers
                        report['offered'] = offered
                        break
                    if call('GET',path)['phase'] in ('completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'):
                        break
                    time.sleep(.35)
                method = {'questions':'item/tool/requestUserInput','file':'item/fileChange/requestApproval',
                          'permissions':'item/permissions/requestApproval'}[interaction]
                assert offered and offered['method']==method, 'Real CLI produced no matching interaction card'
                reply = path+f'/approvals/{offered["id"]}:respond'
                if interaction=='questions':
                    by_id = {x['id']:x for x in offered['params']['questions']}
                    assert set(by_id)=={'run_mode','case_label','test_password'}, 'Real question IDs differed'
                    assert by_id['case_label'].get('isOther') and by_id['test_password'].get('isSecret'), 'Real flags differed'
                    assert 'options' not in by_id['test_password'], 'Options exposed in secret field'
                    call('POST',reply,{'answers':{}},status=400)
                    answer = {'answers':{'run_mode':['校验'],'case_label':['F30-Custom-Label'],'test_password':[secret]}}
                elif interaction=='file':
                    decisions=offered['params'].get('availableDecisions', ['accept','decline','cancel'])
                    if 'accept' in decisions:
                        call('POST',reply,{'decision':'accept'},status=403)
                        report['read_only_grant_refused'] = True
                    answer={'decision':'decline' if 'decline' in decisions else 'cancel'}
                else:
                    params=offered['params'].get('permissions')
                    assert params and params.get('network'),'CLI did not request a real network grant'
                    call('POST',reply,{'permissions':params,'scope':'turn'},status=403)
                    report['read_only_grant_refused'] = True
                    answer={'permissions':{},'scope':'turn'}
                if dist:
                    user=api.call('GET','/auth/me')
                    temporary=api.root/'browser-tmp'
                    temporary.mkdir()
                    subprocess.run([str(browser_python),'-B','-X','utf8',str(Path(__file__).resolve()),'--browser'],
                        input=json.dumps({'root':str(api.root),'base':api.base,'token':api.token,'user':user['id'],
                            'conversation':conversation['id'],'runtime':runtime,'secret':secret,'interaction':interaction,
                            'decision_label':'取消请求' if answer.get('decision')=='cancel' else '拒绝'}),
                        cwd=api.root, env=dict(os.environ,PYTHONIOENCODING='utf8',TEMP=str(temporary),TMP=str(temporary)),
                        encoding='utf8',check=True,timeout=150)
                    report['browser']=json.loads((api.root/'browser-result.json').read_text(encoding='utf8'))
                else:
                    call('POST',reply,answer)
                while time.monotonic() < deadline:
                    terminal = call('GET',path)
                    if terminal['phase'] in ('completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'):
                        break
                    time.sleep(.35)
                report['final_phase'] = terminal['phase']
                assert terminal['phase']=='completed', terminal['phase']
                projections = [call('GET',path+'/events',raw=True), json.dumps(call('GET',path+'/approvals')),
                    json.dumps(call('GET',f'/conversations/{conversation["id"]}'),ensure_ascii=False)]
                assert all(secret not in body for body in projections), 'Test secret appeared in an exposed projection'
                if interaction=='questions':
                    assert 'F30-Custom-Label' in projections[0] or 'F30-Custom-Label' in projections[2], 'Actual CLI did not use free label'
                    database=(api.root/'test.sqlite').resolve().as_uri()+'?mode=ro'
                    with closing(sqlite3.connect(database,uri=True)) as db:
                        db.execute('PRAGMA query_only=ON')
                        stored, = db.execute('SELECT response_json FROM native_cli_approvals WHERE id=?', (offered['id'],)).fetchone()
                    assert secret not in stored
                    assert json.loads(stored)['answers']['test_password'] == {'answered':True,'secret':True}
                    report['persisted_secret_receipt_redacted'] = True
                elif interaction=='file':
                    assert api.call('GET',api.prefix+'/file?path=Ref/f30-approval.txt')['content']=='ORIGINAL\n'
                    report['file_content_unchanged'] = True
                report.update(passed=True, test_secret_absent=True, native_usage=terminal['usage']['status'])
            finally:
                if session_id:
                    api.call('DELETE',f'/codex/sessions/{session_id}')
    except Exception as error:
        report.update(passed=False, failure=str(error).replace(configuration['key'],'[REDACTED]').replace(secret,'[REDACTED]')[:700])
    finally:
        output = json.dumps(report,ensure_ascii=False,indent=2)
        assert configuration['key'] not in output and secret not in output
        (api.root/'live-result.json').write_text(output+'\n',encoding='utf8')
    print('LIVE_F30_CODEX_INTERACTION_RESULT',api.root/'live-result.json',flush=True)
    return 0 if report['passed'] else 1


if sys.argv[1:] == ['--browser']:
    browser(json.load(sys.stdin))
else:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--runtime', choices=['windows','wsl'], required=True)
    parser.add_argument('--interaction', choices=['questions','file','permissions'], default='questions')
    parser.add_argument('--dist',type=lambda path:Path(path).resolve(strict=True))
    parser.add_argument('--browser-python',type=lambda path:Path(path).resolve(strict=True))
    args = parser.parse_args()
    if bool(args.dist)!=bool(args.browser_python):
        parser.error('--dist and --browser-python must be supplied together')
    sys.exit(verify(asyncio.run(source(args.source_profile_id)),args.runtime,args.dist,args.browser_python,args.interaction))
