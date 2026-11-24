"""Exercise real Setup and capability probes in an isolated API and browser."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit
import uuid

from live_llm_outbox_restart_acceptance import restart
from testsupport import IsolatedServer, SERVER


FORM = ('python', 'pandoc', 'wsl_distro', 'openfoam_bashrc')
FILE = 'Ref/path-format-proof.txt'


def route(resp, path, method='GET'):
    return urlsplit(resp.url).path == '/api/v1' + path and resp.request.method == method


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    base = settings['base']
    root = Path(settings['root'])
    checks = {}
    phase = settings['phase']
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1800, 'height': 1080}, locale='zh-CN')
        await context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=base)
        page = await context.new_page()
        active_probes = set()
        def begin_probe(req):
            if urlsplit(req.url).path == '/api/v1/runtime/capabilities':
                active_probes.add(req)
        def end_probe(req):
            active_probes.discard(req)
        page.on('request', begin_probe)
        page.on('requestfinished', end_probe)
        page.on('requestfailed', end_probe)
        await page.add_init_script('localStorage.setItem("vf_token", ' + json.dumps(settings['token']) + ');')
        try:
            await page.goto(base, wait_until='domcontentloaded')
            await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click(timeout=30000)
            setup_tab = page.locator('.dv-tab').filter(has_text=re.compile('^设置与账户$'))
            if await setup_tab.is_visible() and not await page.get_by_test_id('setup-save').is_visible():
                await setup_tab.click()
            await expect(page.get_by_test_id('setup-save')).to_be_visible(timeout=30000)

            async def config():
                value = await context.request.get(base + '/api/v1/runtime/config', headers={'Authorization': 'Bearer ' + settings['token']})
                assert value.ok
                return await value.json()

            async def idle():
                async with asyncio.timeout(70):
                    while active_probes:
                        await asyncio.sleep(.06)

            async def refresh():
                await idle()
                async with page.expect_response(lambda resp: route(resp, '/runtime/capabilities'), timeout=70000) as probe:
                    await page.get_by_role('button', name='重新探测', exact=True).click()
                response = await probe.value
                assert response.ok
                return await response.json()

            async def save(fields, status=200):
                await idle()
                for key, value in fields.items():
                    await page.get_by_test_id('setup-' + key).fill(value)
                if status == 200:
                    async with page.expect_response(lambda resp: route(resp, '/runtime/capabilities'), timeout=70000) as probe:
                        async with page.expect_response(lambda resp: route(resp, '/runtime/config', 'PATCH')) as update:
                            await page.get_by_test_id('setup-save').click()
                        response = await update.value
                        assert response.status == status, await response.text()
                    evidence = await probe.value
                    assert evidence.ok
                    return await response.json(), await evidence.json()
                async with page.expect_response(lambda resp: route(resp, '/runtime/config', 'PATCH')) as update:
                    await page.get_by_test_id('setup-save').click()
                response = await update.value
                assert response.status == status, await response.text()
                return (await response.json())['detail']

            initial = settings['initial']
            current = await config()
            await expect(page.get_by_test_id('setup-python')).to_have_value(current['python'], timeout=5000)
            if phase == 'edit':
                assert current == initial
                actual = await refresh()
                assert actual['wsl']['reachable'] and actual['wsl']['openfoam_bashrc_loaded']
                assert actual['wsl']['openfoam'] and actual['wsl']['distro'] == initial['wsl_distro']
                checks['initial_real_probe'] = True
                registry = {item['tool']: item for item in actual['environment_registry']}
                assert registry['node']['source']=='explicit_error' and not registry['node']['available']
                assert actual['stage_checks']['V']['missing']==['dev/node']
                await expect(page.get_by_test_id('stage-checks').get_by_text('✕ V 缺 dev/node',exact=True)).to_be_visible()
                checks['explicit_missing_node_visible_in_stage_check'] = True
                change = dict(initial, python=settings['alternate_python'])
                committed, actual = await save(change)
                assert committed == change and actual['python']['version'] == settings['alternate_version']
                await expect(page.get_by_text('Python 沙箱（server venv ' + settings['alternate_version'] + '）')).to_be_visible()
                checks['python_save_triggers_real_probe'] = True
                checks['alternate_python_request_id'] = actual['python']['request_id']

                message = await save({'python': initial['python'], 'wsl_distro': 'not-a-supported-distro'}, 400)
                await expect(page.get_by_role('alert').filter(has_text='WSL 发行版')).to_be_visible()
                assert '授权集合' in message and await config() == change
                await save({'wsl_distro': change['wsl_distro'], 'python': settings['missing_python']}, 400)
                assert await config() == change
                await save({'python': change['python'], 'openfoam_bashrc': '/tmp/vf-invalid/etc/bashrc'}, 400)
                assert await config() == change
                await save({'openfoam_bashrc': '/usr/lib/openfoam/vf\tinvalid/etc/bashrc'}, 400)
                assert await config() == change
                checks['invalid_draft_atomic_rejection'] = True

                _, actual = await save({'openfoam_bashrc': settings['missing_foam']})
                assert actual['wsl']['reachable'] and actual['wsl']['openfoam_bashrc_loaded'] is False, actual['wsl']
                assert actual['wsl']['openfoam'] is False, actual['wsl']
                assert not next(x for x in actual['environment_registry'] if x['tool']=='openfoam_bashrc')['available']
                await expect(page.get_by_text(re.compile(r'✕\s*OpenFOAM（blockMesh）'))).to_be_visible()
                checks['selected_openfoam_missing_observable'] = True
                checks['missing_foam_request_id'] = actual['wsl']['request_id']
                committed, actual = await save({'openfoam_bashrc': initial['openfoam_bashrc']})
                assert committed == change and actual['wsl']['openfoam_bashrc_loaded'] is True

                if settings.get('absent_distro'):
                    _, actual = await save({'wsl_distro': settings['absent_distro']})
                    assert actual['wsl']['distro'] == settings['absent_distro'] and actual['wsl']['reachable'] is False
                    assert not next(x for x in actual['environment_registry'] if x['tool']=='wsl_distro')['available']
                    await expect(page.get_by_text('不可达：', exact=False)).to_be_visible()
                    committed, actual = await save({'wsl_distro': initial['wsl_distro']})
                    assert committed == change and actual['wsl']['reachable'] is True
                    checks['available_distro_restored_after_missing'] = True

                style = page.get_by_title('路径文本格式', exact=True)
                await style.select_option('windows')
                await page.locator('[data-path=' + json.dumps(FILE) + ']').click()
                copy = page.get_by_role('button', name='复制相对路径', exact=True)
                await copy.click()
                assert await page.evaluate('navigator.clipboard.readText()') == FILE.replace('/', '\\')
                await style.select_option('linux')
                await copy.click()
                assert await page.evaluate('navigator.clipboard.readText()') == FILE
                checks['clipboard_path_format'] = True
                await page.screenshot(path=root / 'runtime-setup-browser.png')
            else:
                assert current['python'] == settings['alternate_python']
                await expect(page.get_by_test_id('setup-wsl_distro')).to_have_value(initial['wsl_distro'])
                checks['cold_api_preserved_setup'] = True
                member = await chromium.new_context(viewport={'width': 1800, 'height': 1080}, locale='zh-CN')
                try:
                    member_page = await member.new_page()
                    await member_page.add_init_script('localStorage.setItem("vf_token", ' + json.dumps(settings['member_token']) + ');')
                    await member_page.goto(base, wait_until='domcontentloaded')
                    marker = member_page.get_by_test_id('setup-save')
                    tab = member_page.locator('.dv-tab').filter(has_text=re.compile('^设置与账户$'))
                    if await tab.is_visible() and not await marker.is_visible():
                        await tab.click()
                    await expect(marker).to_be_visible(timeout=30000)
                    await expect(member_page.get_by_test_id('setup-python')).to_have_value(current['python'])
                    await member_page.get_by_test_id('setup-python').fill(initial['python'])
                    async with member_page.expect_response(lambda resp: route(resp, '/runtime/config', 'PATCH')) as update:
                        await marker.click()
                    denied = await update.value
                    assert denied.status == 403 and await config() == current
                    await expect(member_page.get_by_role('alert').filter(has_text='仅组织 admin')).to_be_visible()
                    checks['member_cannot_edit'] = True
                finally:
                    await member.close()
                committed, actual = await save({key: '' for key in FORM})
                assert committed == initial and actual['wsl']['openfoam_bashrc_loaded'] and actual['wsl']['reachable']
                for key in FORM:
                    await expect(page.get_by_test_id('setup-' + key)).to_have_value(initial[key])
                checks['clear_restores_real_defaults'] = True
                assert actual['stage_checks']['V']['ok']
                await expect(page.get_by_test_id('stage-checks').get_by_text('✓ V 就绪',exact=True)).to_be_visible()
                registry = {item['tool']: item for item in actual['environment_registry']}
                assert registry['node']['source']=='explicit' and registry['node']['value']==settings['node']
                assert registry['npm']['available'] and registry['wsl_distro']['available']
                checks['valid_explicit_node_restores_stage_check'] = True
                checks['default_python_request_id'] = actual['python']['request_id']
        finally:
            (root / ('runtime-setup-' + phase + '.json')).write_text(json.dumps(checks, indent=2), 'utf8')
            await chromium.close()


def run(args):
    api = IsolatedServer('runtime-setup-browser', app='runtime_setup_fixture:app')
    bundle = args.dist.resolve(strict=True)
    assert (bundle / 'index.html').is_file()
    node = Path(shutil.which('node')).resolve(strict=True)
    npm = node.parent/'node_modules/npm/bin/npm-cli.js'
    versions = {}
    for tool, command in (('node',[node,'--version']), ('npm',[node,npm,'--version'])):
        versions[tool]=subprocess.run(command,check=True,capture_output=True,text=True,timeout=15).stdout.strip()
    check_path = '''
import json,sys
from app.services.runtime_registry import environment_registry
empty={r['tool']:r for r in environment_registry({'PATH':''})}
selected={r['tool']:r for r in environment_registry({'PATH':sys.argv[1]})}
assert all(not empty[name]['available'] for name in ('node','npm'))
assert all(selected[name]['available'] for name in ('node','npm'))
print(json.dumps({name:selected[name]['value'] for name in ('node','npm')}))
'''
    selected_path = subprocess.run([sys.executable,'-B','-c',check_path,str(node.parent)],
        cwd=SERVER,env=dict(os.environ,VIBEFLOW_RUNTIME_CONFIG=str(api.root/'unconfigured.json')),
        check=True,capture_output=True,text=True,timeout=15)
    (api.root/'missing-node').touch()
    (api.root / 'runtime_setup_fixture.py').write_text(
        'import os\nfor name in ("PYTHON", "PANDOC", "WSL_DISTRO", "OPENFOAM_BASHRC"):\n'
        ' os.environ.pop("VIBEFLOW_"+name,None)\nfrom pathlib import Path\n'
        + 'root=Path(__file__).parent\n'
        + f'os.environ["VIBEFLOW_NODE"]=str(root/"missing-node.exe") if (root/"missing-node").exists() else {str(node)!r}\n'
        + 'from app.main import app\n'
        + 'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory=' + repr(str(bundle)) + ', html=True))\n', 'utf8')
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0,'host_tool_versions':versions,
              'isolated_search_path':json.loads(selected_path.stdout)}
    print('RUNTIME_SETUP_BROWSER_EVIDENCE',api.root,flush=True)
    try:
        with api:
            case = api.create_case('Isolated Runtime Setup')
            api.call('PUT', api.prefix + '/file', {'path': FILE, 'content': 'runtime setup path formatting\n'})
            initial = api.call('GET', '/runtime/config')
            configured_python = str(Path(sys._base_executable).resolve(strict=True))
            assert configured_python != initial['python']
            version = subprocess.run([configured_python, '--version'], check=True, capture_output=True, text=True, timeout=15).stdout.split()[1]
            installed = subprocess.run(['wsl.exe', '-l', '-q'], check=True, capture_output=True, timeout=12).stdout.decode('utf-16le').split()
            member_email = 'setup-viewer-' + uuid.uuid4().hex + '@example.com'
            invited = api.call('POST', '/auth/register', {'email': member_email, 'password': 'Isolated-test-20261119', 'name': 'Setup member'})
            with sqlite3.connect(api.root / 'test.sqlite') as db:
                # Existing registration provisions an admin role for a private org.
                # Use a regular member in this fixture to exercise the real auth gate.
                changed = db.execute("update memberships set role='member' where user_id=(select id from users where email=?)", (member_email,)).rowcount
                assert changed == 1
            settings = {'base': api.base, 'root': str(api.root), 'token': api.token, 'member_token': invited['token'],
                'case': case['name'], 'channel': args.channel, 'initial': initial, 'alternate_python': configured_python,
                'node': str(node),
                'alternate_version': version, 'missing_python': str(api.root / 'missing-python.exe'),
                'missing_foam': '/usr/lib/openfoam/vf-uninstalled-' + uuid.uuid4().hex + '/etc/bashrc',
                'absent_distro': '' if 'openfoam2406-default' in installed else 'openfoam2406-default'}

            def stage(phase):
                subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                               input=json.dumps(settings | {'phase': phase}), text=True, check=True, timeout=220)
                report[phase] = json.loads((api.root / ('runtime-setup-' + phase + '.json')).read_text(encoding='utf8'))

            stage('edit')
            original_pid = api.proc.pid
            api.proc.terminate()
            api.proc.wait(timeout=12)
            (api.root/'missing-node').unlink()
            restart(api)
            assert api.proc.pid != original_pid
            stage('restart')
            job_id = api.call('POST', api.prefix + '/jobs', {
                'runtime': 'wsl', 'timeout_seconds': 50, 'input_paths': [FILE],
                'command': "test -f Ref/path-format-proof.txt && printf 'SETUP_WSL_CWD=' && pwd",
            }, expect=(200, 202), extra_headers={'Idempotency-Key': uuid.uuid4().hex})['id']
            job = api.wait_job(job_id, timeout=90)
            log = api.call('GET', f'/jobs/{job_id}/log?offset=0')['chunk']
            converted = re.search(r'SETUP_WSL_CWD=(/mnt/d/[^\r\n]+)', log)
            assert converted and api.root.name in converted[1], log
            assert job['attempt']['process_stop']['verified']
            report['actual_wsl_path_conversion'] = converted[1]
            with sqlite3.connect(api.root / 'test.sqlite') as db:
                for phase, key, kind, selection in (
                    ('edit', 'alternate_python_request_id', 'runtime.python_probe', {'python': configured_python}),
                    ('edit', 'missing_foam_request_id', 'runtime.wsl_probe', {'bashrc': settings['missing_foam']}),
                    ('restart', 'default_python_request_id', 'runtime.python_probe', {'python': initial['python']}),
                ):
                    identifier = report[phase][key]
                    actual_kind, profile, ended_at = db.execute(
                        'select kind,profile_json,ended_at from native_requests where id=?', (identifier,)
                    ).fetchone()
                    assert actual_kind == kind and ended_at is not None
                    registered = json.loads(profile)
                    assert all(registered[name] == value for name, value in selection.items()), registered
            report['native_probe_profile_selection_verified'] = True
        report['passed'] = report['api_exited'] = api.proc.poll() is not None
    finally:
        (api.root / 'runtime-setup-result.json').write_text(json.dumps(report, indent=2), 'utf8')
    print('RUNTIME_SETUP_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        run(parser.parse_args())
