"""Real DeepSeek file proposals and read-only history in the built Agent UI."""
import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from live_llm_agent_recovery_acceptance import stream
from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer, SERVER


OBSERVER = '''
live_send=httpx.AsyncClient.send
async def verify_snapshot_boundary(self, request, *args, **kwargs):
    if request.url.host == 'api.deepseek.com' and request.url.path.endswith(('/chat/completions','/v1/messages')):
        assert b'proposal_snapshot' not in request.content, 'Stored file preview reached the provider'
    return await live_send(self, request, *args, **kwargs)
httpx.AsyncClient.send=verify_snapshot_boundary
'''
CASE_NAME = 'FF-F26 file proposal history'
MODES = ('manual','rejected','automatic','dictionary')


def use_case(page, api, case_name=CASE_NAME):
    page.goto(api.base,wait_until='domcontentloaded')
    page.locator('[data-case-name='+json.dumps(case_name)+']').click(timeout=30000)
    page.locator('.dv-tab').filter(has_text=re.compile('^Agent$')).click(timeout=30000)


def select_history(page, cid, path, reason, label, old, new, *, screenshot=None, added=1, removed=None):
    from playwright.sync_api import expect

    page.get_by_test_id('agent-conversation').select_option(str(cid),timeout=25000)
    card = page.get_by_test_id('chat-tool-card').filter(has_text=path).filter(has=page.get_by_test_id('proposal-history'))
    expect(card).to_have_count(1,timeout=15000)
    expect(card).to_contain_text(label)
    expect(page.get_by_test_id('approval-card')).to_have_count(0)
    summary = card.get_by_text('查看提案',exact=True)
    summary.focus()
    summary.press('Enter')
    expect(card.get_by_test_id('proposal-history-reason')).to_have_text(reason)
    counts = card.get_by_test_id('approval-diff-counts')
    expect(counts).to_have_attribute('data-added',str(added))
    expect(counts).to_have_attribute('data-removed',str(int(bool(old)) if removed is None else removed))
    if old:
        expect(card.locator('[data-kind="del"]').filter(has_text=old)).to_have_count(removed or 1)
    if new:
        expect(card.locator('[data-kind="add"]').filter(has_text=new)).to_have_count(added)
    if screenshot is not None:
        card.screenshot(path=screenshot)


def browser_decision(page, cid, tool_id, approved, reason, *, added=1, removed=None, approval_evidence=None):
    from playwright.sync_api import TimeoutError as BrowserTimeout, expect

    page.get_by_test_id('agent-conversation').select_option(str(cid),timeout=25000)
    pending=page.get_by_test_id('approval-card')
    expect(pending).to_have_count(1,timeout=15000)
    expect(pending).to_contain_text(reason)
    counts=pending.get_by_test_id('approval-diff-counts')
    retry=pending.get_by_role('button',name='重读差异',exact=True)
    try:
        for _ in range(2):
            if not retry.or_(counts).first.is_visible():
                retry.or_(counts).first.wait_for(timeout=15000)
            if counts.is_visible():
                break
            retry.click()
        expect(counts).to_have_attribute('data-added',str(added),timeout=15000)
        if removed is not None:
            expect(counts).to_have_attribute('data-removed',str(removed))
    except (AssertionError, BrowserTimeout) as error:
        raise AssertionError(pending.inner_text(timeout=5000)) from error
    action='approve' if approved else 'reject'
    endpoint=f'/api/v1/conversations/{cid}/tool-calls/{tool_id}:{action}'
    with page.expect_response(lambda r: r.request.method == 'POST' and unquote(urlsplit(r.url).path) == endpoint,
                              timeout=90000) as response:
        pending.get_by_role('button',name='批准执行' if approved else '拒绝',exact=True).click()
    result=response.value
    assert result.status==200, (result.status,result.text()[:600])
    if approved:
        events=[json.loads(line[5:]) for line in result.text().split('\n') if line.startswith('data:')]
        if approval_evidence is not None:
            approval_evidence.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding='utf8')
        [tool]=[row for row in events if row['type']=='tool.result']
        failures=[row for row in events if row['type'] in {'request.failed','request.timed_out','stream.error'}]
        assert tool['ok'] and tool['approved'] and any(row['type']=='run.completed' for row in events), (tool,failures)
    expect(pending).to_have_count(0,timeout=15000)


def exercise(api, page, case, profile, config, mode):
    reason='核对提案'+uuid.uuid4().hex[:6]
    old='ORIGINAL_'+uuid.uuid4().hex
    new='UPDATED_'+uuid.uuid4().hex
    actual_path=f'Ref/{mode}.txt'
    dictionary=mode=='dictionary'
    if dictionary:
        actual_path='0-caseDict/caseDict'
        prior=api.call('GET',api.prefix+'/caseDict')
        before=prior['content']
        after=before.rstrip('\n')+'\n// '+new+'\n'
        # Normalize the final newline before binding the initial revision so
        # the resulting diff is one added comment.
        before=before.rstrip('\n')+'\n'
        prior=api.call('PUT',api.prefix+'/caseDict',{'content':before,'expected_revision':prior['dict_revision']})
    elif mode=='manual':
        before=''
        after=new+'\n'
    else:
        before=old+('\u2028上一版' if mode=='automatic' else '')+'\n'
        after=new+'\n'
        api.put(actual_path,before)
    cid=api.call('POST','/conversations',{'case_id':case['id'],'title':mode,
        'permission_mode':'full_access' if mode=='automatic' else 'approve_writes',
        'provider_profile_id':profile['id'],'model':config['model']})['id']
    route=f'/conversations/{cid}'
    target=api.prefix+'/raw?path='+actual_path
    message={'content':f'这是一次性隔离测试。{actual_path} 的旧内容已由我核对。只调用一次 propose_file_update。'
                       f'path 精确为 {actual_path}，reason 精确为 {reason}，'
                       f'content 必须是以下 JSON 字符串解码后的完整文本 {json.dumps(after,ensure_ascii=False)}。'
                       '直接提出写入，无需读取或调用其他工具。取得结果后只报告一次，不重试。',
             'provider_profile_id':profile['id'],'model':config['model']}
    events=stream(api,route+'/messages',message,mode+'-proposal',config)
    failures=[row for row in events if row['type'] in {'request.failed','stream.error','request.timed_out'}]
    assert not failures,failures
    state=api.call('GET',route)
    if mode=='automatic':
        assert not state['pending_tools']
        [tool]=[row for row in events if row['type']=='tool.result' and row['tool']=='propose_file_update']
        assert tool['ok'] and tool['auto'] and any(row['type']=='run.completed' for row in events)
        [proposal]=state['tool_calls']
        label='自动应用'
    else:
        [proposal]=state['pending_tools']
        if mode=='manual':
            api.call('GET',target,expect=404)
        else:
            assert api.call('GET',target,raw=True)==before.encode()
        use_case(page,api)
        browser_decision(page,cid,proposal['id'],mode!='rejected',reason)
        label='已拒绝' if mode=='rejected' else '人工批准'
    assert proposal['tool']=='propose_file_update'
    args=proposal['args']
    assert args['path']==actual_path and args['content']==after and args['reason']==reason
    assert args['baseline_sha256']==(None if mode=='manual' else hashlib.sha256(before.encode()).hexdigest())
    assert args['proposal_snapshot']=={'kind':'text','before':before},args['proposal_snapshot'].get('kind')
    expected=before if mode=='rejected' else after
    assert api.call('GET',target,raw=True)==expected.encode()
    state=api.call('GET',route)
    assert not state['pending_tools']
    [result]=[row['payload'] for row in state['events'] if row['type']=='tool_result' and row['payload']['tool']=='propose_file_update']
    assert result['ok']==(mode!='rejected')
    if dictionary:
        revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
        assert revision==prior['dict_revision']+1
    api.call('POST',route+'/tool-calls/'+proposal['id']+':approve',{},expect=409)
    initial={'passed':True,'conversation_id':cid,'path':actual_path,'reason':reason,'label':label,'after':new,
             'tool_id':proposal['id'],'history_added':1,'history_removed':int(mode in {'rejected','automatic'})}
    if mode in {'rejected','automatic'}:
        initial['before']=old
        api.put(actual_path,'UPDATED_LATER\n')
    use_case(page,api)
    select_history(page,cid,actual_path,reason,label,initial.get('before'),new,screenshot=api.root/(mode+'-history.png'))
    if dictionary:
        initial['dictionary_revision']=revision
    return initial


def test_archive(api, page, case, scenario, *, case_name=CASE_NAME):
    cid=scenario['conversation_id']
    data=api.call('GET',f'/conversations/{cid}/export',raw=True)
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=30) as client:
        response=client.post('/conversations:import',params={'case_id':case['id'],'title':'Imported read-only proposal'},
                             headers={'Content-Type':'application/x-ndjson'},content=data)
        response.raise_for_status()
        imported=response.json()['id']
    state=api.call('GET',f'/conversations/{imported}')
    assert not state['pending_tools'] and not state['tool_calls']
    api.call('POST',f'/conversations/{imported}/tool-calls/{scenario["tool_id"]}:approve',{},expect=404)
    use_case(page,api,case_name)
    select_history(page,imported,scenario['path'],scenario['reason'],scenario['label'],scenario.get('before'),
                   scenario['after'],screenshot=api.root/'imported-history.png',added=scenario['history_added'],
                   removed=scenario['history_removed'])
    return {'conversation_id':imported,'read_only':True,'snapshot_survived':True}


def verify(config, modes, dist, store, database_url, wire, *, case_name=CASE_NAME,
           scenario=exercise, evidence_label='live-file-proposal-history', continue_on_error=False):
    from playwright.sync_api import sync_playwright

    api=IsolatedServer(evidence_label,app='proposal_history_fixture:app',database_url=database_url,access_log=True)
    settings=store.settings() if store else {}
    static='\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='+repr(str(dist))+',html=True))\n'
    prerequisites = f'import os\nos.environ.update({settings!r})\nlive_request_limit = {len(modes)*2+2}\n'
    (api.root/'proposal_history_fixture.py').write_text(prerequisites+FIXTURE+OBSERVER+static,
                                                      encoding='utf8')
    report={'passed':False,'wire':wire,'backend':'s3' if store else 'fs','database':'postgresql' if database_url else 'sqlite',
            'model':config['model'],'simulated_model_calls':0,'scenarios':{}}
    label=evidence_label.upper().replace('-','_')
    print(label+'_EVIDENCE',api.root,flush=True)
    scenario_errors = {}

    def describe(error):
        return {'type':type(error).__name__,'detail':str(error).replace(config['key'],'[REDACTED]')[:1500]}

    try:
        with api, sync_playwright() as playwright:
            case=api.create_case(case_name)
            profile=api.call('POST','/provider-profiles',{'provider':'anthropic' if wire=='messages' else 'deepseek',
                'name':'Authorized DeepSeek file proposal','base_url':'https://api.deepseek.com/anthropic' if wire=='messages' else config['base_url'],
                'default_model':config['model'],'api_key':config['key']})
            browser=playwright.chromium.launch(channel='msedge')
            try:
                page=browser.new_page(viewport={'width':1450,'height':910},locale='zh-CN')
                page.add_init_script('localStorage.setItem("vf_token", '+json.dumps(api.token)+');')
                for mode in modes:
                    try:
                        report['scenarios'][mode]=scenario(api,page,case,profile,config,mode)
                    except Exception as error:
                        if not continue_on_error:
                            raise
                        scenario_errors[mode]=describe(error)
                        print(label+'_SCENARIO_FAILED',mode,scenario_errors[mode],flush=True)
                        continue
                    print(label+'_PASS',mode,flush=True)
                if 'automatic' in report['scenarios']:
                    report['imported_history']=test_archive(api,page,case,report['scenarios']['automatic'],case_name=case_name)
            finally:
                browser.close()
        report['passed']=not scenario_errors
    except Exception as error:
        report['failure']=describe(error)
        print(label+'_FAILED',report['failure'],flush=True)
    finally:
        if scenario_errors:
            report['scenario_errors']=scenario_errors
        trace=api.root/'upstream.jsonl'
        report['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.is_file() else []
        (api.root/'live-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if report['passed'] else 1


async def run(options, **verification):
    config=await source(options.source_profile_id)
    store=nullcontext()
    if options.s3:
        from s3testsupport import ObjectStore
        store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',authenticated=True)
    async with disposable_database(options.postgres) as database_url:
        with store as active:
            return await asyncio.to_thread(verify,config,options.modes.split(','),options.dist,active,database_url,options.wire,
                                           **verification)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--dist',type=lambda path:Path(path).resolve(strict=True),required=True)
    parser.add_argument('--modes',default=','.join(MODES))
    parser.add_argument('--wire',choices=('completions','messages'),default='completions')
    parser.add_argument('--s3',action='store_true')
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    selected=args.modes.split(',')
    if len(set(selected))!=len(selected) or not set(selected)<=set(MODES) or not (args.dist/'index.html').is_file():
        parser.error('Choose a built frontend and unique file proposal modes')
    sys.exit(asyncio.run(run(args)))
