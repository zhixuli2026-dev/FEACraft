"""Cancel an actual job, terminal, VTK renderer and DeepSeek-selected shell by case."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import uuid
from urllib.parse import quote, urlencode

import aiohttp
import httpx

from app.services import process_identity_native as identity
from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer, SERVER


ENTRY = 'platform/server/live_llm_runtime_supervisor_acceptance.py'
ROOT = SERVER.parents[1]
STL = ('solid tri\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n'
       'vertex 0 1 0\nendloop\nendfacet\nendsolid\n')


async def until(read, accepts, label, *, seconds=60):
    try:
        async with asyncio.timeout(seconds):
            while True:
                value = await read()
                if accepts(value):
                    return value
                await asyncio.sleep(.2)
    except TimeoutError:
        raise AssertionError(f'Timed out while observing {label}') from None


async def collect_messages(client, path, body, events):
    async with client.stream('POST', path, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith('data:'):
                events.append(json.loads(line[5:]))


async def read_terminal_output(ws, marker):
    await ws.send_json({'type': 'input', 'data': 'set /a 271+383\r'})
    out = bytearray()
    async with asyncio.timeout(30):
        while marker not in out:
            msg = await ws.receive()
            assert msg.type == aiohttp.WSMsgType.BINARY, msg.type
            out.extend(msg.data)


async def observe_processes(client, api, case_id, terminal, profile, model, receipt, events):
    async def get(path):
        response = await client.get(path)
        response.raise_for_status()
        return response.json()

    async def post(path, body):
        response = await client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    async def tool_execution(path):
        response = await client.get(path)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def only_child(result):
        journal = result.get('children') or {}
        return next(iter(journal.get('children', {}).values()), None)

    async def probe(record):
        return await asyncio.to_thread(identity.probe, record)

    body = {'case_id': case_id}
    processes_path = f'/runtime/processes?case_id={case_id}'
    terminal_path = f'/terminal/sessions/{terminal}'
    started_name = 'Ref/ffg10-start-' + uuid.uuid4().hex + '.txt'
    finished_name = started_name.replace('start-', 'finish-')
    marker = uuid.uuid4().hex
    command = (f"Set-Content -LiteralPath '{started_name}' -Value '{marker}'; "
               f"Start-Sleep -Seconds 140; Set-Content -LiteralPath '{finished_name}' -Value '{marker}'")
    prompt = ('请只调用一次 run_shell_command，不调用其它工具，不重试。runtime 必须是 powershell，'
              'timeout_seconds 必须是 170，command 逐字使用下面一行，保留引号和分号。'
              '这是本地隔离算例中的作业取消验收。我会在命令暂停时从界面停止。\n' + command)

    job = await post(api.prefix + '/jobs', {'type': 'shell', 'runtime': 'windows',
                     'command': 'echo FFG10_NATIVE_JOB_ACTIVE & ping -n 260 127.0.0.1 >nul', 'timeout_seconds': 300})
    job_path = f'/jobs/{job["id"]}'
    await until(lambda: get(job_path), lambda row: row['status'] == 'running'
                and 'FFG10_NATIVE_JOB_ACTIVE' in row.get('log_backlog', ''), 'live native job output')
    file = await client.put(api.prefix + '/file', json={'path': 'D-post/actual.stl', 'content': STL})
    file.raise_for_status()
    viz = await post(api.prefix + '/viz:sessions', {'file_path': 'D-post/actual.stl'})
    viz_path = f'/viz:sessions/{viz["id"]}'
    assert viz['phase'] == 'active' and viz['ready'], viz
    scene = await client.get(f'{api.base}/viz-proxy/{viz["id"]}/index.html', headers={'Authorization': 'Bearer ' + api.token})
    scene.raise_for_status()
    assert scene.content
    conversation = await post('/conversations', {'case_id': case_id, 'title': 'Actual owner cancellation',
                              'provider_profile_id': profile['id'], 'model': model, 'permission_mode': 'full_access'})
    cid = conversation['id']
    receipt.update(job_id=job['id'], terminal_id=terminal, viz_id=viz['id'], conversation_id=cid)
    stream = asyncio.create_task(collect_messages(client, f'/conversations/{cid}/messages',
                                {'content': prompt, 'provider_profile_id': profile['id'], 'model': model}, events))
    try:
        async def selected_tool():
            if stream.done():
                await stream
                raise AssertionError('Actual model stream ended before the owned shell could be observed')
            return next((event for event in events if event.get('type') == 'tool.state'
                         and event.get('status') == 'running'), None)

        choice = await until(selected_tool, bool, 'the real model selecting a tool', seconds=120)
        assert choice['tool'] == 'run_shell_command'
        assert choice['args']['runtime'] == 'powershell' and choice['args']['timeout_seconds'] == 170
        assert choice['args']['command'] == command, 'Actual model selected a different command'
        tid = f'{cid}:{choice["tool_call_id"]}'
        agent_path = f'/conversations/{cid}/tool-calls/{quote(tid, safe="")}/execution'

        async def marker_response(path):
            response = await client.get(api.prefix + '/raw', params={'path': path})
            assert response.status_code in {200, 404}, response.status_code
            return response.text.strip() if response.is_success else None

        await until(lambda: marker_response(started_name), lambda value: value == marker,
                    'the actual PowerShell command writing its first marker')
        assert await marker_response(finished_name) is None
        agent = await until(lambda: tool_execution(agent_path), lambda row: row and only_child(row)
                            and only_child(row)['phase'] == 'running', 'the actual Agent shell process registration')
        receipt['agent_execution_id'] = agent['id']
        observed = await get(processes_path)
        selected = {item['kind']: item['id'] for item in observed['processes']}
        assert selected.keys() == {'job', 'terminal', 'viz', 'agent', 'agent-tool'}, observed
        assert selected['job'] == f'job:{job["id"]}' and selected['terminal'] == f'terminal:{terminal}'
        assert selected['viz'] == f'viz:{viz["id"]}' and selected['agent'] == f'agent:{cid}'
        active_job = await get(job_path)
        active_terminal = await get(terminal_path)
        births = {'job': active_job['attempt']['process_identity']['host'],
                  'terminal': active_terminal['process']['root']['host'], 'viz': viz['process']['host'],
                  'agent_shell': only_child(agent)['identity']['host']}
        checks = {family: await probe(birth) for family, birth in births.items()}
        assert all(item['state'] == 'match' for item in checks.values()), checks
        receipt.update(active=observed, live_observations=checks)
        assert (await get('/runtime/monitor'))['processes'] == dict.fromkeys(selected, 1)

        cancelled = await post('/runtime/processes:cancel', body)
        receipt['cancel'] = cancelled
        assert cancelled['ok'] and cancelled['cancelled'] == dict.fromkeys(selected, 1), cancelled
        await asyncio.wait_for(asyncio.shield(stream), timeout=60)
        calls = [event for event in events if event.get('type') == 'tool.state' and event.get('status') == 'running']
        assert calls == [choice], calls
        assert sum(event.get('type') == 'run.cancelled' for event in events) == 1, events[-8:]
        assert await marker_response(finished_name) is None
        final_job = await until(lambda: get(job_path), lambda row: row['status'] == 'cancelled'
                                and bool(row['attempt']['process_stop']), 'the job stop record')
        final_terminal = await get(terminal_path)
        final_viz = await get(viz_path)
        final_agent = await get(agent_path)
        assert final_terminal['phase'] == 'closed' and final_viz['phase'] == 'closed'
        assert final_agent['phase'] == final_agent['tool_call_status'] == 'cancelled', final_agent
        stop_records = {'job': final_job['attempt']['process_stop'], 'terminal': final_terminal['stop'],
                        'viz': final_viz['stop'], 'agent_shell': only_child(final_agent)['stop']}
        assert all(stop['verified'] and stop['scope_empty'] for stop in stop_records.values()), stop_records
        stops = {family: await probe(birth) for family, birth in births.items()}
        assert all(item['state'] == 'exited' for item in stops.values()), stops
        assert (await get(processes_path))['processes'] == []
        receipt.update(process_stops=stop_records, exit_observations=stops, marker_never_completed=True,
                       agent_final={'phase': final_agent['phase'], 'tool_status': final_agent['tool_call_status']})
    finally:
        if not stream.done():
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)


async def verify(configuration, review):
    api = IsolatedServer('live-runtime-supervisor', app='live_fixture:app')
    (api.root / 'live_fixture.py').write_text(FIXTURE, encoding='utf8')
    report = {'passed': False, 'provider': configuration['provider'], 'model': configuration['model'],
              'mock_llm_calls': 0, 'isolated_storage': 'fs', 'isolated_database': 'sqlite', 'review': str(review)}
    events = []
    print('LIVE_RUNTIME_SUPERVISOR_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            case = await asyncio.to_thread(api.create_case, 'Real case process supervision')
            profile = await asyncio.to_thread(api.call, 'POST', '/provider-profiles', {
                'provider': configuration['provider'], 'name': 'Actual process supervision verification',
                'base_url': configuration['base_url'], 'default_model': configuration['model'],
                'api_key': configuration['key']})
            query = urlencode({'case': case['id'], 'shell': 'cmd', 'token': api.token, 'cols': 90, 'rows': 24})
            timeout = httpx.Timeout(180, connect=8)
            headers = {'Authorization': 'Bearer ' + api.token}
            async with httpx.AsyncClient(base_url=api.base + '/api/v1', headers=headers,
                                         timeout=timeout, trust_env=False) as client, aiohttp.ClientSession() as browser:
                try:
                    async with browser.ws_connect(api.base.replace('http:', 'ws:') + '/ws/terminal?' + query) as ws:
                        ready = await ws.receive_json(timeout=30)
                        assert ready['type'] == 'ready' and ready['alive'] is True, ready
                        await read_terminal_output(ws, b'654')
                        await observe_processes(client, api, case['id'], ready['session'], profile,
                                                configuration['model'], report, events)
                except BaseException:
                    response = await client.post('/runtime/processes:cancel', json={'case_id': case['id']})
                    report['failure_cleanup_status'] = response.status_code
                    raise
        trace = api.root / 'upstream.jsonl'
        upstream = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
        calls = [item for item in upstream if item['kind'] == 'http' and item['path'].endswith('/chat/completions')]
        assert len(calls) == 1 and calls[0]['host'] == 'api.deepseek.com' and calls[0]['status'] == 200
        assert any(item.get('model') == configuration['model'] for item in upstream if item['kind'] == 'stream_identity')
        report['passed'] = True
    except BaseException as error:
        report.update(error=type(error).__name__, detail=str(error).replace(configuration['key'], '[REDACTED]')[:1800])
        raise RuntimeError('Live process supervision acceptance failed. See ' + str(api.root / 'live-result.json')) from None
    finally:
        trace = api.root / 'upstream.jsonl'
        upstream = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        report.update(upstream=upstream, llm_calls=sum(item['kind'] == 'http' and item['path'].endswith('/chat/completions')
                                                     for item in upstream),
                      api_exited=api.proc is not None and api.proc.poll() is not None)
        for name, value in (('live-result.json', report), ('live-model-events.json', events)):
            raw = json.dumps(value, ensure_ascii=False, indent=2)
            assert configuration['key'] not in raw
            (api.root / name).write_text(raw, encoding='utf8')
        print('LIVE_RUNTIME_SUPERVISOR_RESULT ' + str(api.root / 'live-result.json'), flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf8'))
    assert review['entry'] == ENTRY
    for name, digest in review['sources'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    configuration = await source(args.source_profile_id)
    assert configuration['model'] == 'deepseek-flash'
    await verify(configuration, args.review)


if __name__ == '__main__':
    asyncio.run(main())
