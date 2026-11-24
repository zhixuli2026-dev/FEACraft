"""Observe cold process recovery of a locally published run derivation."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

import httpx

from file_transaction_diagnostics_acceptance import restart
from live_llm_terminal_agent_acceptance import disposable_database
from run_derive_source_lock_smoke import BASE, CONTROL, SIZE
from testsupport import IsolatedServer, SERVER


FIXTURE = r'''
from contextvars import ContextVar
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app

root = Path(__file__).parent
original_commit = AsyncSession.commit
mode = ContextVar('derive_exit_mode',default='')

async def commit(db):
    marker = root/'arm-derive-exit'
    if mode.get() and marker.exists() and Path(marker.read_text(encoding='utf8')).is_dir():
        marker.unlink()
        if mode.get()=='before':
            os._exit(86)
        result = await original_commit(db)
        os._exit(87)
    return await original_commit(db)

class Observe:
    def __init__(self,app):
        self.app=app

    async def __call__(self,scope,receive,send):
        header=dict(scope.get('headers',())).get(b'x-derive-exit',b'')
        value=header.decode('ascii') if scope.get('path','').endswith('/runs:derive') else ''
        token=mode.set(value)
        try:
            await self.app(scope,receive,send)
        finally:
            mode.reset(token)

AsyncSession.commit=commit
app=Observe(app)
'''


def crash(api, client, name, target, phase):
    (api.root/'arm-derive-exit').write_text(str(target),encoding='utf8')
    try:
        client.post(api.prefix+'/runs:derive',json={'base':BASE,'name':name},headers={'X-Derive-Exit':phase})
    except httpx.TransportError:
        pass
    else:
        raise AssertionError('The API did not exit at the selected boundary')
    evidence = restart(api,86 if phase=='before' else 87)
    assert target.is_dir(),target
    return evidence


def entries(api):
    return {row['path'] for row in api.call('GET',api.prefix+'/tree')['entries']}


def changes(api, name):
    expected = f'C-run/{name}'
    events = api.call('GET',api.prefix+'/workflow-events')['events']
    return [event['payload'] for event in events if event['type']=='files.changed'
            and expected in event['payload']['paths']]


def recovery_receipts(root):
    history = root/'.vibeflow/tree-transactions/history'
    return [json.loads(path.read_text(encoding='utf8')) for path in history.glob('*/resolution-*.json')]


async def derive_in_browser(api, case_name, channel):
    from playwright.async_api import async_playwright, expect

    target = 'C-run/browser-copy'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=channel)
        try:
            context = await browser.new_context(viewport={'width':1536,'height':1100}, locale='zh-CN')
            await context.add_init_script('localStorage.setItem("vf_token",'+json.dumps(api.token)+');')
            editor, observer = await context.new_page(), await context.new_page()
            forms = []
            for page in (editor, observer):
                await page.goto(api.base,wait_until='domcontentloaded')
                await page.locator('[data-case-name='+json.dumps(case_name)+']').click(timeout=30000)
                await page.locator('.dv-tab').get_by_text('caseDict',exact=True).click(timeout=25000)
                base = page.get_by_role('combobox').filter(has=page.get_by_role('option',name='基准目录…',exact=True))
                await expect(base).to_contain_text(BASE,timeout=20000)
                forms.append(base)
            name = editor.get_by_placeholder('新工况名（如 case-Re1000）')
            button = editor.get_by_role('button',name='派生',exact=True)
            await expect(button).to_be_disabled()
            await forms[0].select_option(BASE)
            await name.fill('browser-copy')
            matches = lambda response: response.url.endswith('/runs:derive') and response.request.method=='POST'
            async with editor.expect_response(matches,timeout=60000) as submitted:
                await button.click()
            assert (await submitted.value).status==200
            await expect(editor.get_by_text('✓ 已创建 '+target+'（3 个文件）',exact=True)).to_be_visible(timeout=15000)
            await expect(forms[1]).to_contain_text(target,timeout=30000)
            await name.fill('browser-copy')
            async with editor.expect_response(matches,timeout=30000) as duplicated:
                await button.click()
            assert (await duplicated.value).status==409
            await expect(editor.get_by_text('✕ 409:',exact=False)).to_be_visible(timeout=10000)
        finally:
            await browser.close()
    return {'channel':channel,'created':target,'second_tab_observed':True,'duplicate_rejected':True}


def run(baseline, database_url, size, browser):
    api = IsolatedServer('derive-crash-fs',app='derive_crash_fixture:app',database_url=database_url)
    fixture = FIXTURE
    if browser:
        dist = SERVER.parent/'web/dist'
        assert (dist/'index.html').is_file(), dist
        fixture += '\nfrom fastapi.staticfiles import StaticFiles\napp.app.mount("/", StaticFiles(directory='+repr(str(dist))+', html=True))\n'
    (api.root/'derive_crash_fixture.py').write_text(fixture,encoding='utf8')
    result = {'passed':False,'baseline':baseline,'database':'postgresql' if database_url else 'sqlite',
              'actual_model_calls':0,'simulated_llm_calls':0}
    print('DERIVE_CRASH_EVIDENCE',api.root,flush=True)
    try:
        with api:
            case_name = 'Cold derived directory'
            api.create_case(case_name)
            prefix = api.call('GET',api.prefix)['storage_prefix']
            root = api.root/'files'/prefix
            api.put(BASE+'/system/controlDict',CONTROL)
            api.put(BASE+'/0/U','initial data')
            binary = root/BASE/'0/field.bin'
            with binary.open('wb') as stream:
                stream.write(b'vibeflow-cold-derive')
                stream.truncate(size)
            with binary.open('rb') as stream:
                expected = hashlib.file_digest(stream,'sha256').hexdigest()
            endpoint = api.prefix+'/runs:derive'
            name = 'cold-before'
            target = root/'C-run'/name
            with httpx.Client(base_url=api.base+'/api/v1',timeout=65,trust_env=False,
                              headers={'Authorization':'Bearer '+api.token}) as client:
                before = crash(api,client,name,target,'before')
                relative = f'C-run/{name}/0/field.bin'
                before['uncommitted_target_visible_after_cold_read'] = relative in entries(api)
                result['before_commit_exit'] = before
                if baseline:
                    conflict = api.call('POST',endpoint,{'base':BASE,'name':name},expect=409)
                    assert before['uncommitted_target_visible_after_cold_read'] and '目标工况已存在' in conflict['detail']
                    before['same_name_retry_http']=409
                else:
                    assert not before['uncommitted_target_visible_after_cold_read']
                    assert not changes(api,name)
                    assert [item['decision'] for item in recovery_receipts(root)]==['rolled_back']
                    retry = api.call('POST',endpoint,{'base':BASE,'name':name})
                    assert retry['created']==f'C-run/{name}'
                    with (target/'0/field.bin').open('rb') as stream:
                        assert hashlib.file_digest(stream,'sha256').hexdigest()==expected
                    assert changes(api,name)==[{'paths':[f'C-run/{name}'],'changes':[],'rescan':True}]
                    before['same_name_retry_http']=200
                    before['aborted_file_change_events']=0
                    name = 'cold-after'
                    target = root/'C-run'/name
                    after = crash(api,client,name,target,'after')
                    assert f'C-run/{name}/0/field.bin' in entries(api)
                    with (target/'0/field.bin').open('rb') as stream:
                        assert hashlib.file_digest(stream,'sha256').hexdigest()==expected
                    api.call('POST',endpoint,{'base':BASE,'name':name},expect=409)
                    assert changes(api,name)==[{'paths':[f'C-run/{name}'],'changes':[],'rescan':True}]
                    assert sorted(item['decision'] for item in recovery_receipts(root))==[
                        'committed','committed','rolled_back']
                    assert not (root/'.vibeflow/tree-transactions/pending').exists()
                    after['committed_target_preserved']=True
                    after['committed_file_change_events']=1
                    after['same_name_retry_http']=409
                    result['after_commit_exit']=after
            with binary.open('rb') as stream:
                assert hashlib.file_digest(stream,'sha256').hexdigest()==expected
            if browser:
                result['browser'] = asyncio.run(derive_in_browser(api,case_name,browser))
            result.update(passed=True,source_bytes=size,source_sha256=expected)
    finally:
        (api.root/'derive-crash.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')


async def main(args):
    async with disposable_database(args.postgres) as url:
        await asyncio.to_thread(run,args.observe_uncommitted_target,url,args.size_mib*1024**2,args.browser)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-uncommitted-target',action='store_true')
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--size-mib',type=int,default=SIZE//1024**2,choices=(40,140))
    parser.add_argument('--browser',choices=('msedge','chrome'))
    args=parser.parse_args()
    enforce([Path(__file__)])
    asyncio.run(main(args))
