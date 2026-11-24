"""Verify real caseDict block editing, source fidelity and version conditions."""

import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

import httpx

from app.services.casedict import parse_casedict, set_block
from app.services.process_identity_native import probe
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


CASE = 'Block source fidelity'
CASE_DICT = '0-caseDict/caseDict'
SOURCE = ('outer { notes { marker decoy; } }\r\n'
          '"notes" /* quoted top-level section */\r\n'
          '{\r\n'
          '\t// keep this header\r\n'
          '\tinner { value 1 /* keep its unit */; }\r\n'
          '\toptions (alpha /* keep list note */ beta);\r\n'
          '\tmarker old; // keep line comment\r\n'
          '}\r\n')
APP = '''
import os
from pathlib import Path
import json
from fastapi import Depends
from app.main import app
from app.deps import get_current_user
from app.services.process_identity_native import observe

root = Path(__file__).parent
@app.on_event('startup')
async def birth():
    (root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')

@app.post('/fixture/stop', status_code=204)
async def stop(body: dict, user=Depends(get_current_user)):
    assert body['pid'] == os.getpid()
    os._exit(80)
'''


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel='msedge')
        context = await edge.new_context(viewport={'width':1540, 'height':980},
            storage_state={'cookies': [], 'origins': [{'origin':settings['base'],
                'localStorage':[{'name':'vf_token', 'value':settings['token']}]}]})
        page = await context.new_page()
        page.set_default_timeout(25000)
        try:
            await page.goto(settings['base'])
            await page.locator('[data-case-name="' + CASE + '"]').click()
            editor = page.get_by_test_id('block-editor')
            if not await editor.is_visible():
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if await tab.is_visible():
                    await tab.click()
            await expect(editor).to_be_visible()
            await editor.get_by_label('编辑的字典块').select_option('notes')
            names = editor.get_by_placeholder('键', exact=True)
            await expect(names.nth(2)).to_have_value('marker')
            await editor.get_by_placeholder('值（[ 或 { 开头按 JSON 解析）', exact=True).nth(2).fill('new')
            path = '/api/v1/cases/' + str(settings['case_id']) + '/caseDict/block'
            async with page.expect_response(lambda resp: urlsplit(resp.url).path == path
                    and resp.request.method == 'PUT') as received:
                await editor.get_by_role('button', name='应用该块').click()
            response = await received.value
            assert response.status == 200, await response.text()
            entries = response.request.post_data_json['entries']
            assert entries['marker'] == 'new' and entries['inner'] == {'value':'1'}
            assert entries['options'] == ['(alpha', 'beta)']
            await expect(editor.get_by_role('status')).to_have_text('已保存（只替换了该块）')
            root = Path(settings['root'])
            await editor.screenshot(path=str(root/'edge-block-editor.png'))
            (root/'edge-result.json').write_text(json.dumps({
                'passed': True, 'browser':edge.version, 'revision':(await response.json())['dict_revision'],
                'submitted_complete_block':True}, indent=2), encoding='utf8')
        finally:
            await edge.close()


def process_exit(api):
    birth = json.loads((api.root/'api-birth.json').read_text(encoding='utf8'))
    with httpx.Client(timeout=10, trust_env=False) as client:
        try:
            client.post(api.base + '/fixture/stop', json={'pid':birth['pid']},
                headers={'Authorization':'Bearer '+api.token})
        except (httpx.RemoteProtocolError, httpx.ReadError):
            pass
    api.proc.wait(20)
    state = probe(birth)
    assert api.proc.returncode == 80 and state['state'] == 'exited'
    return state


def restart(api):
    assert api.proc.poll() is not None
    api.proc = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', api.app,
        '--app-dir', str(SERVER), '--host', '127.0.0.1', '--port', str(api.port), '--no-access-log'],
        cwd=api.root, env=api.env, stdout=api.log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        assert api.proc.poll() is None, api.root
        try:
            if api.call('GET','/api/health',api_prefix=False)['ok']:
                return
        except OSError:
            pass
        time.sleep(.1)
    raise TimeoutError('Restarted API did not become healthy')


def exercise(api, args, report, *, run_browser):
    case = api.create_case(CASE)
    path = api.prefix + '/caseDict'
    raw_path = api.prefix + '/raw?path=' + CASE_DICT

    def raw():
        return api.call('GET', raw_path, raw=True)

    initial = api.call('GET', path)
    authored = SOURCE + initial['content'].replace('workflowState A;', 'workflowState A /* keep stage note */;')
    spaced_list = b'tags ( alpha /* keep spaced list */ );'
    authored = re.sub(r'\bcaseMeta\s*\{', lambda match: match.group() + '\n    ' + spaced_list.decode('utf8'),
                      authored, count=1)
    authored = authored.replace('\r\n', '\n').replace('\n', '\r\n')
    created = api.call('PUT', path, {'content':authored, 'expected_revision':0})
    assert created['dict_revision'] == 1 and raw() == authored.encode('utf8')
    entries = api.call('GET', path)['parsed']['notes']
    if run_browser:
        payload = {'base':api.base, 'case_id':case['id'], 'token':api.token, 'root':str(api.root)}
        subprocess.run([str(args.browser_python), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                       input=json.dumps(payload), text=True, timeout=100, check=True)
        assert json.loads((api.root/'edge-result.json').read_text(encoding='utf8'))['revision'] == 2
        report['real_edge_form'] = True
    else:
        api.call('PUT', path+'/block', {'block':'notes', 'entries':{**entries,'marker':'new'},
                                       'expected_revision':1})
    exact = authored.replace('marker old;', 'marker new;').encode('utf8')
    assert raw() == exact and api.call('GET', path)['dict_revision'] == 2
    report['exact_first_edit_sha256'] = hashlib.sha256(exact).hexdigest()

    block = {'block':'notes', 'entries':{**entries, 'inner':{'value':2}, 'marker':'new'}}
    api.call('PUT', path+'/block', {**block,'expected_revision':1}, expect=409)
    api.call('PUT', path+'/block', {**block,'expected_revision':2,'expected_sha256':'0'*64}, expect=409)
    foreign = api.call('POST','/auth/register', {'email':uuid.uuid4().hex+'@example.com',
        'name':'Block outsider','password':'isolated-block-other-123'})
    api.call('PUT', path+'/block', block, expect=404,
             extra_headers={'Authorization':'Bearer '+foreign['token']})
    api.call('PUT', path+'/block', {'block':'workflowState','entries':{'invalid':'value'}},
             expect=422)
    assert raw() == exact and api.call('GET', path)['dict_revision'] == 2
    api.call('PUT', path+'/block', {**block,'expected_revision':2})
    assert raw() == exact.replace(b'value 1', b'value 2')
    report.update(revision_conflict=409, hash_conflict=409, outsider=404, invalid_status=422,
                  nested_edit_exact=True)

    cleared = api.call('PUT', path+'/block', {'block':'notes','entries':{},'expected_revision':3})
    final = raw()
    parsed = parse_casedict(final.decode('utf8'))
    assert cleared['dict_revision'] == 4 and parsed['notes'] == {}
    assert parsed['outer']['notes']['marker'] == 'decoy'
    assert all(marker in final for marker in (b'// keep this header', b'// keep line comment',
                                               b'/* keep list note */', b'workflowState A /* keep stage note */;'))
    assert b'\n' not in final.replace(b'\r\n', b'')
    report['api_initial_exit'] = process_exit(api)
    restart(api)
    assert raw() == final and api.call('GET', path)['dict_revision'] == 4
    advanced = api.call('POST', path+':advance', {'state':'V','expected_revision':4,'confirmed':True})
    assert advanced['workflow_state'] == 'V' and advanced['dict_revision'] == 5
    assert raw() == final.replace(b'workflowState A', b'workflowState V')
    metadata = api.call('GET', path)['parsed']['caseMeta']
    assert metadata['tags'] == ['alpha']
    api.call('PUT', path+'/block', {'block':'caseMeta',
              'entries':{**metadata,'caseName':'Block formatting witness'},'expected_revision':5})
    assert spaced_list in raw() and api.call('GET', path)['parsed']['caseMeta']['tags'] == ['alpha']
    report.update(clear_keeps_outer_comments=True, unchanged_decoy=True, cold_api_exact_readback=True,
                  checked_stage_advance_keeps_comment=True, untouched_spaced_list=True)


def verify(args, database, store):
    api = IsolatedServer('casedict-block-' + ('s3' if store else 'fs'),
                         app='block_fixture:app', database_url=database)
    settings = store.settings() if store else {}
    bundle = args.dist.resolve(strict=True)
    (api.root/'block_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n' + APP
        + '\nfrom fastapi.staticfiles import StaticFiles\n'
        + 'app.mount("/", StaticFiles(directory='+repr(str(bundle))+', html=True))\n', encoding='utf8')
    print('CASEDICT_BLOCK_EVIDENCE ' + str(api.root), flush=True)
    report = {'passed':False, 'database':'postgresql' if database else 'sqlite',
              'backend':'s3' if store else 'fs', 'real_model_requests':0, 'mock_llm_calls':0}
    try:
        with api:
            try:
                exercise(api, args, report, run_browser=not store)
            finally:
                if api.proc.poll() is None and api.token:
                    report['api_final_exit'] = process_exit(api)
        report['passed'] = True
    finally:
        (api.root/'block-fidelity.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print('CASEDICT_BLOCK_OK ' + str(api.root), flush=True)


def direct_checks():
    text = 'workflowState A;\r\na { x 1 /* units */; other keep; }\r\n'
    assert set_block(text, 'a', {}, replace_missing=False) == text
    assert set_block(text, 'a', {'x':2}, replace_missing=False) == text.replace('x 1', 'x 2')
    for block, fields in [('a', {'unknown':1}), ('missing', {'x':1})]:
        try:
            set_block(text, block, fields, replace_missing=False)
        except ValueError:
            continue
        raise AssertionError('Strict replacement accepted a missing entry')
    assert parse_casedict(set_block('a { same a; same b; other keep; }',
                                   'a', {'other':'keep'}))['a'] == {'other':'keep'}
    assert parse_casedict(set_block('a { x "a b"c; }', 'a', {'x':['new','c']}))['a']['x'] == ['(new','c)']
    result = set_block(text, 'a', {'x':1, 'shown label':2})
    assert parse_casedict(result)['a']['shown label'] == '2' and '\r\n' in result


async def main(args):
    direct_checks()
    use_s3 = args.backend == 's3'
    runtime = SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed'
    context = ObjectStore(executable=runtime, volume_max=12, authenticated=True) if use_s3 else nullcontext()
    async with disposable_database(use_s3) as database:
        with context as store:
            await asyncio.to_thread(verify, args, database, store)
    if use_s3:
        assert context.proc is not None and context.proc.poll() is not None


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        sys.path.insert(0, str(SERVER.parent / 'tools'))
        from regression_llm_policy import enforce

        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--backend', choices=('fs','s3'), required=True)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        args = parser.parse_args()
        enforce([Path(__file__)])
        asyncio.run(main(args))
