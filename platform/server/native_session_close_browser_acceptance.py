"""Open and stop exact terminal and visualization sessions through browser history."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import subprocess
import sys


STL = ('solid tri\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n'
       'vertex 0 1 0\nendloop\nendfacet\nendsolid\n')
TERMINALS = '''
async ({base, token, case_id, shell}) => {
  const issued = await fetch(base + '/api/v1/connection-tickets', {
    method: 'POST', headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: JSON.stringify({scope: 'terminal', case_id}),
  });
  if (!issued.ok) throw new Error('Terminal authorization: ' + issued.status);
  const {ticket} = await issued.json();
  return await new Promise((resolve, reject) => {
  const query = new URLSearchParams({token: ticket, case: case_id, shell, cols: '90', rows: '25'});
  const socket = new WebSocket(base.replace(/^http/, 'ws') + '/ws/terminal?' + query);
  const timer = setTimeout(() => { socket.close(); reject(new Error('Terminal did not start')); }, 45000);
  socket.onmessage = ({data}) => {
    if (typeof data !== 'string') return;
    const message = JSON.parse(data);
    if (message.type === 'ready' && message.alive === true) {
      clearTimeout(timer); socket.close(); resolve(message.session);
    } else if (message.type === 'error') {
      clearTimeout(timer); socket.close(); reject(new Error(message.code));
    }
  };
  });
};
'''


async def browser_run(data):
    from playwright.async_api import async_playwright, expect

    base = data['base']
    root = Path(data['root'])
    owner = {'Authorization': 'Bearer ' + data['token']}
    outside = {'Authorization': 'Bearer ' + data['outsider']}
    path = {'terminal': '/terminal/sessions/', 'viz': '/viz:sessions/'}
    resources = []
    report = {'passed': False, 'model_calls': 0, 'mock_llm_calls': 0, 'resources': resources, 'checks': []}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=data['channel'])
        context = await browser.new_context(viewport={'width': 1440, 'height': 1060})
        await context.add_init_script('localStorage.setItem("vf_token",' + json.dumps(data['token']) + ')')
        page = await context.new_page()

        async def request(method, endpoint, *, body=None, headers=owner, status=200):
            answer = await context.request.fetch(base + '/api/v1' + endpoint, method=method,
                                                 data=body, headers=headers, timeout=60000)
            assert answer.status == status, (method, endpoint, answer.status, (await answer.text())[:300])
            return await answer.json()

        async def history_select(history, kind, identifier):
            chooser = history.get_by_role('combobox', name='类型', exact=True)
            if not await chooser.is_visible():
                await history.locator(':scope > summary').click()
            await chooser.select_option(kind)
            selected = history.get_by_role('combobox', name='选择会话记录')
            await expect(selected.locator(f'option[value="{identifier}"]')).to_have_count(1, timeout=20000)
            await selected.select_option(identifier)
            detail = history.locator('.native-session-history-detail')
            await expect(detail).to_contain_text('会话 ' + identifier, timeout=20000)
            return detail, selected

        async def browser_close(history, kind, identifier, other):
            detail, selected = await history_select(history, kind, identifier)
            name = '关闭此终端会话' if kind == 'terminal' else '关闭此可视化会话'
            button = detail.get_by_role('button', name=name, exact=True)
            if data['baseline']:
                await expect(detail).to_contain_text('原执行者可由当前服务连接。')
                await expect(button).to_have_count(0)
                return
            await expect(button).to_be_enabled(timeout=20000)
            await button.scroll_into_view_if_needed()
            await page.screenshot(path=root / f'active-{kind}.png')
            box = await button.bounding_box()
            assert box['height'] >= 44 and box['width'] >= 44, box
            async with page.expect_response(lambda response: response.request.method == 'DELETE'
                    and response.url == base + '/api/v1' + path[kind] + identifier, timeout=60000) as response:
                await button.focus()
                await button.press('Enter')
            assert (await response.value).status == 200
            feedback = detail.get_by_role('status').filter(has_text='已关闭此会话，停止已核验。')
            await expect(feedback).to_be_focused(timeout=20000)
            await expect(detail).to_contain_text('完整停止已核验。')
            await expect(selected.locator(f'option[value="{identifier}"]')).to_contain_text('已关闭')
            await expect(button).to_have_count(0)
            survivor = await request('GET', '/native-sessions/' + kind + '/' + other)
            assert survivor['phase'] == 'active' and survivor['alive'] is True and survivor['cleanup_verified'] is None
            report['checks'].append(f'keyboard closed {kind}:{identifier} and left {kind}:{other} running')
            await page.screenshot(path=root / f'closed-{kind}.png')

        try:
            await page.goto(base, wait_until='domcontentloaded')
            await page.locator('[data-case-name=' + json.dumps(data['case_name']) + ']').click(timeout=30000)
            for shell in ('cmd', 'wsl'):
                identifier = await page.evaluate(TERMINALS, {'base': base, 'token': data['token'],
                                                 'case_id': data['case_id'], 'shell': shell})
                resources.append({'kind': 'terminal', 'id': identifier, 'runtime': shell})
            for name in ('target.stl', 'other.stl'):
                filename = 'D-post/' + name
                await request('PUT', data['prefix'] + '/file', body={'path': filename, 'content': STL})
                renderer = await request('POST', data['prefix'] + '/viz:sessions', body={'file_path': filename})
                assert renderer['phase'] == 'active' and renderer['ready']
                resources.append({'kind': 'viz', 'id': str(renderer['id']), 'runtime': 'windows'})
            for resource in resources:
                kind, identifier = resource['kind'], resource['id']
                current = await request('GET', path[kind] + identifier)
                assert current['phase'] == 'active'
                resource['active'] = current
                await request('DELETE', path[kind] + identifier, headers=outside, status=404)
            cmd, wsl, viz, other_viz = [item['id'] for item in resources]
            terminal_tab = page.locator('.dv-tab').filter(has_text=re.compile('^终端$'))
            await terminal_tab.click()
            box = await terminal_tab.bounding_box()
            await page.mouse.move(720, box['y'])
            await page.mouse.down()
            await page.mouse.move(720, box['y'] - 230, steps=12)
            await page.mouse.up()
            terminal_history = page.get_by_test_id('terminal-panel').get_by_test_id('native-session-history')
            await browser_close(terminal_history, 'terminal', cmd, wsl)
            viz_tab = page.locator('.dv-tab').filter(has_text=re.compile('^可视化$'))
            await viz_tab.click()
            viz_group = viz_tab.locator('xpath=ancestor::div[contains(@class,"dv-groupview")][1]')
            viz_history = viz_group.get_by_test_id('native-session-history')
            detail, _ = await history_select(viz_history, 'viz', viz)
            if not data['baseline']:
                async with page.expect_response(lambda response: '/viz-proxy/' + viz + '/index.html' in response.url,
                                                timeout=30000) as response:
                    await detail.get_by_role('button', name='打开已有可视化').click()
                assert (await response.value).status == 200
            await browser_close(viz_history, 'viz', viz, other_viz)
            if not data['baseline']:
                await expect(viz_group.locator('iframe[title="可视化"]')).to_have_count(0, timeout=20000)
                await expect(viz_group.get_by_text('#' + viz + ' :', exact=False)).to_have_count(0, timeout=20000)
                await expect(viz_group.get_by_text('#' + other_viz + ' :', exact=False)).to_be_visible(timeout=20000)
                report['checks'].append('closing the selected renderer clears its iframe and leaves the other renderer listed')
            else:
                report['checks'].append('previous browser history exposes both active records without per-session close controls')
            report['browser'] = browser.version
            report['passed'] = True
        except BaseException as error:
            report.update(error=type(error).__name__, detail=str(error)[:1600])
            await page.screenshot(path=root / 'browser-failure.png')
            raise
        finally:
            try:
                for resource in resources:
                    endpoint = path[resource['kind']] + resource['id']
                    await request('DELETE', endpoint)
                    result = await request('GET', endpoint)
                    assert result['phase'] == 'closed' and result['stop']['verified'] and result['stop']['scope_empty']
                    resource['closed'] = result
            except BaseException as error:
                report.update(passed=False, cleanup_error=type(error).__name__)
                raise
            finally:
                (root / 'native-session-close-browser.json').write_text(json.dumps(report, indent=2), encoding='utf8')
                await browser.close()


async def confirm_exit(path):
    from app.services import process_identity_native as identity
    from app.services.owned_process import probe_native

    report = json.loads(path.read_text(encoding='utf8'))
    try:
        for resource in report['resources']:
            current = resource['closed']
            journal = current['process']
            root = journal['root'] if resource['kind'] == 'terminal' else journal
            states = {'root': identity.probe(root['host'])}
            if resource['kind'] == 'terminal':
                gate = journal['gate']['process']
                if gate['platform'] == 'windows':
                    states['gate'] = identity.probe(gate)
                else:
                    assert gate['uid'] == 65534
                    states['gate'] = await probe_native(root['native']['distribution'], gate)
            else:
                states['renderer'] = identity.probe(current['ready']['process'])
            assert all(state['state'] == 'exited' for state in states.values()), states
            resource['exit_observations'] = states
        report['independent_exit_confirmed'] = True
    except BaseException:
        report['passed'] = False
        raise
    finally:
        path.write_text(json.dumps(report, indent=2), encoding='utf8')


def run(args):
    from testsupport import IsolatedServer

    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    api = IsolatedServer('native-session-close', app='history_fixture:app')
    (api.root / 'history_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf8')
    evidence = api.root / 'native-session-close-browser.json'
    print('NATIVE_SESSION_CLOSE_EVIDENCE ' + str(evidence), flush=True)
    with api:
        case = api.create_case('Selected native session cleanup')
        outside = api.call('POST', '/auth/register', {'email': 'other-ffg14@example.com',
                           'password': 'isolated-other-123', 'name': 'Other'})
        data = {'base': api.base, 'token': api.token, 'case_id': case['id'], 'case_name': case['name'],
                'prefix': api.prefix, 'outsider': outside['token'], 'root': str(api.root), 'channel': args.channel,
                'baseline': args.baseline}
        result = subprocess.run([str(args.browser_python), '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--browser'],
                                input=json.dumps(data), text=True, timeout=420)
    assert api.proc.poll() is not None
    if evidence.exists():
        asyncio.run(confirm_exit(evidence))
        report = json.loads(evidence.read_text(encoding='utf8'))
        report['api_exited'] = True
        evidence.write_text(json.dumps(report, indent=2), encoding='utf8')
    result.check_returncode()
    print('NATIVE_SESSION_CLOSE_PASS ' + str(evidence), flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser_run(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        parser.add_argument('--baseline', action='store_true')
        run(parser.parse_args())
