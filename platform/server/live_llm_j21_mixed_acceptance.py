"""Overlap real PTY, ParaView, DeepSeek SSE and k6 reads on one isolated stack."""

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
import httpx

from live_llm_acceptance import source
from live_llm_j21_sse_load_acceptance import measure_one
from mixed_load_test_support import API_IMAGE, WEB_IMAGE, Deployment, RenderClient, docker
from terminal_load_50_acceptance import open_session

ROOT = Path(__file__).resolve().parents[2]
READS = ROOT / 'platform/tools/k6-mixed-reads.js'


def epoch():
    return time.time_ns() // 1_000_000


def percentile(values):
    values = sorted(values)
    return values[math.ceil(len(values) * .95) - 1] if values else None


def k6_reads(deployment, accounts, starts, k6):
    name = deployment.label + '-mixed-k6'
    command = [
        'docker', 'run', '--rm', '-i', '--pull=never', '--name', name,
        '--network', deployment.network, '--user', '65534:65534',
        '--mount', f'type=bind,source={k6},target=/inputs/k6,readonly',
        '--mount', f'type=bind,source={READS},target=/inputs/reads.js,readonly',
        '-e', 'K6_AUTO_EXTENSION_RESOLUTION=false', '-e', 'K6_NO_USAGE_REPORT=true',
        '-e', f'VIBEFLOW_MIXED_START={starts}',
        '-e', f'VIBEFLOW_MIXED_BASE=http://{deployment.label}-web/api/v1',
        '--entrypoint', '/bin/sh', deployment.api_image, '-c',
        'set -e; umask 077; cat >/tmp/accounts.json; cp /inputs/k6 /tmp/k6; chmod 700 /tmp/k6; '
        'set +e; /tmp/k6 run --quiet /inputs/reads.js >&2; code=$?; cat /tmp/mixed-rest-summary.json; exit "$code"',
    ]
    private_data = [{'id': user['case'], 'email': user['email'], 'token': user['token']} for user in accounts]
    assert len(private_data) == len(accounts)
    try:
        reply = subprocess.run(command, input=json.dumps(private_data).encode(), capture_output=True, timeout=220)
    except subprocess.TimeoutExpired:
        subprocess.run(['docker','rm','--force','--volumes',name], capture_output=True, timeout=12)
        raise
    if not reply.stdout:
        raise RuntimeError(f'Disposable k6 client returned {reply.returncode} without metrics')
    assert all(user['token'].encode() not in reply.stdout for user in accounts)
    return {'exit_code': reply.returncode, 'metrics': json.loads(reply.stdout)}


def first_origin_headers(deployment):
    code = """
import json,time
from pathlib import Path
deadline=time.monotonic()+80
while time.monotonic()<deadline:
    for path in Path('/work/data').glob('upstream-*.jsonl'):
        for line in path.read_text().splitlines():
            try:
                row=json.loads(line)
            except ValueError:
                continue
            if row['kind']=='http' and row['status']==200:
                print(row['headers_ms'])
                raise SystemExit(0)
    time.sleep(.1)
raise SystemExit(1)
"""
    return int(docker('exec',deployment.worker,'/usr/local/bin/python','-B','-c',code,timeout=85))


async def request(client, method, url, token=None, **kwargs):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    async with client.request(method, url, headers=headers, **kwargs) as response:
        if response.status != 200:
            raise RuntimeError(f'{method} {urlsplit(url).path} returned HTTP {response.status}')
        return await response.json()


async def prepare(client, public, owner, config, grid, account):
    api = owner + '/api/v1'
    email = uuid4().hex + '@example.com'
    registered = await request(client, 'POST', api + '/auth/register', json={
        'email':email, 'name':'Mixed-load participant', 'password':'isolated-mixed-test-user',
    })
    token = registered['token']
    workspace = (await request(client, 'GET', api + '/workspaces', token))[0]['id']
    case = (await request(client, 'POST', f'{api}/workspaces/{workspace}/cases', token,
                          json={'name': 'Mixed-load case'}))['id']
    account.update(email=email, token=token, case=case)
    profile = (await request(client, 'POST', api + '/provider-profiles', token, json={
        'name':'Authorized DeepSeek mixed load', 'provider':'deepseek',
        'base_url':config['base_url'], 'default_model':config['model'], 'api_key':config['key'],
    }))['id']
    conversation = (await request(client, 'POST', api + '/conversations', token, json={
        'case_id':case, 'title':'Mixed SSE', 'permission_mode':'plan',
        'provider_profile_id':profile, 'model':config['model'],
    }))['id']
    account['model_account'] = ({'Authorization':'Bearer ' + token}, profile, conversation)
    data, digest = grid
    path = 'D-post/mixed-view.vtp'
    upload_route = f'{api}/cases/{case}/uploads'
    upload = await request(client, 'POST', upload_route, token, json={
        'path':path, 'size':len(data), 'sha256':digest, 'overwrite':False,
    })
    upload_url = upload_route + '/' + upload['id']
    for offset in range(0, len(data), 8*1024*1024):
        await request(client, 'PUT', upload_url, token, params={'offset':offset},
                      data=data[offset:offset+8*1024*1024])
    await request(client, 'POST', upload_url + ':complete', token)
    opened = await request(client, 'POST', f'{api}/cases/{case}/viz:sessions', token,
                           json={'file_path':path,'engine':'paraview'})
    view = RenderClient(client, public, token, opened['id'])
    account['view'] = view
    await view.connect()
    await view.render()
    account['view_ready_ms'] = epoch()
    ws_root = 'ws://' + urlsplit(public).netloc
    ws, sid, row = await open_session(ws_root, account['model_account'][0], case)
    account.update(terminal=ws, terminal_id=sid, terminal_setup=row)
    assert ws is not None and sid is not None and 'command_ms' in row, row


async def terminal_command(socket):
    nonce = uuid4().hex
    expected = ('MIXED_' + nonce).encode()
    seen = b''
    await socket.send(json.dumps({'type':'input','data':f"printf '\\n%s%s\\n' 'MIXED_' '{nonce}'\r"}))
    while expected not in seen:
        message = await socket.recv()
        if isinstance(message, bytes):
            seen = (seen + message)[-8192:]


async def measure(starts, operation, field=None, *, deadline=135):
    await asyncio.sleep(max(0, (starts - epoch()) / 1000))
    started = epoch()
    row = {'started_ms':started}
    measured = time.perf_counter()
    try:
        async with asyncio.timeout(deadline):
            result = await operation()
        if field:
            row[field] = round((time.perf_counter()-measured)*1000, 3)
        if isinstance(result, dict):
            row.update(result)
        elif isinstance(result, tuple):
            row['image_ms'] = result[0]
    except Exception as error:
        row['error'] = type(error).__name__
    row['ended_ms'] = epoch()
    return row


async def cleanup(client, public, account):
    result = {}
    for kind, path, socket in (
        ('terminal', f'/terminal/sessions/{account.get("terminal_id")}', account.get('terminal')),
        ('viz', f'/viz:sessions/{account["view"].sid}' if account.get('view') else None,
         account['view'].socket if account.get('view') else None),
    ):
        if not path or path.endswith('/None'):
            if socket is not None:
                await socket.close()
            continue
        url = public + '/api/v1' + path
        try:
            await request(client, 'DELETE', url, account['token'])
            row = await request(client, 'GET', url, account['token'])
            result[kind] = (row['phase'] == 'closed' and row['cleanup_verified'] is True)
        except (aiohttp.ClientError, RuntimeError, TimeoutError, KeyError, ValueError) as error:
            result[kind] = False
            result[kind + '_error'] = type(error).__name__
        finally:
            if socket is not None:
                await socket.close()
    return result


async def run(deployment, addresses, public, config, users, receipt, k6, grids):
    accounts = [{} for _ in range(users)]
    setup = asyncio.Semaphore(5)
    stop = asyncio.Semaphore(5)
    stop_heartbeat = asyncio.Event()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=85), trust_env=False,
                                    connector=aiohttp.TCPConnector(limit=users * 5)) as client:
        ready = 0
        async def prepare_one(index):
            nonlocal ready
            async with setup:
                grid = grids[1 if index % 12 == 0 else 0]
                await prepare(client, public, addresses[index % 2], config, grid, accounts[index])
                ready += 1
                if ready % 5 == 0 or ready == users:
                    print('MIXED_READY', ready, '/', users, flush=True)

        async def cleanup_one(account):
            async with stop:
                return await cleanup(client, public, account)

        async def pulse(account):
            # aiohttp processes WebSocket control frames while receiving. Keep
            # the prepared connection alive as a browser would during setup.
            await account['view'].rpc('pv.proxy.manager.list', [])
            account['view_heartbeat_ms'] = epoch()

        async def heartbeat():
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=4)
                except TimeoutError:
                    active = [row for row in accounts if 'view_ready_ms' in row]
                    replies = await asyncio.gather(*(pulse(row) for row in active), return_exceptions=True)
                    for account,reply in zip(active,replies):
                        if isinstance(reply, Exception):
                            account['view_heartbeat_error'] = type(reply).__name__

        keeping_connections = asyncio.create_task(heartbeat())
        try:
            prepared = await asyncio.gather(*(prepare_one(index) for index in range(users)), return_exceptions=True)
            errors = [str(e).replace(config['key'], '[REDACTED]')[:180] for e in prepared if isinstance(e, BaseException)]
            receipt.update(prepared=ready, preparation_errors=errors)
            assert not errors and ready == users, errors
            stop_heartbeat.set()
            await keeping_connections
            await asyncio.gather(*(pulse(row) for row in accounts))
            receipt['connections_before_start'] = [
                {key:row[key] for key in ('view_ready_ms','view_heartbeat_ms','view_heartbeat_error') if key in row}
                for row in accounts
            ]
            assert all(row['terminal'].close_code is None and not row['view'].socket.closed for row in accounts)
            starts = epoch() + 5000
            receipt['scheduled_start_ms'] = starts
            async with httpx.AsyncClient(base_url=public + '/api/v1', timeout=125, trust_env=False,
                                         limits=httpx.Limits(max_connections=users)) as streams:
                terminal = asyncio.gather(*(measure(starts, lambda row=row:terminal_command(row['terminal']),
                                                      'command_ms', deadline=25) for row in accounts))
                viz = asyncio.gather(*(measure(starts, lambda row=row:row['view'].drag(0,50)) for row in accounts))
                async def continued_load():
                    origin = asyncio.create_task(asyncio.to_thread(first_origin_headers, deployment))
                    await asyncio.gather(terminal,viz)
                    headers_ms = await origin
                    started_ms = epoch()
                    commands,frames = await asyncio.gather(
                        asyncio.gather(*(measure(started_ms,lambda row=row:terminal_command(row['terminal']),
                                                  'command_ms',deadline=25) for row in accounts)),
                        asyncio.gather(*(measure(started_ms,lambda row=row:row['view'].drag(1,50)) for row in accounts)),
                    )
                    return {'first_provider_headers_ms':headers_ms,'triggered_ms':started_ms,
                            'terminal':commands,'viz':frames}

                groups = await asyncio.gather(
                    asyncio.to_thread(k6_reads, deployment, accounts, starts, k6),
                    asyncio.gather(*(measure(starts, lambda row=row: measure_one(streams, row['model_account'],
                                                                                  config['model'])) for row in accounts)),
                    terminal,viz,continued_load(),
                    return_exceptions=True,
                )
            for key, result in zip(('rest','agent','terminal','viz','during_provider'), groups):
                receipt[key] = {'error':type(result).__name__} if isinstance(result, Exception) else result
        finally:
            stop_heartbeat.set()
            await keeping_connections
            receipt['cleanup'] = await asyncio.gather(*(cleanup_one(row) for row in accounts if row))


def summarize(report):
    users = report['users']
    metrics = report.get('rest', {}).get('metrics', {})
    agents = report.get('agent', []) if isinstance(report.get('agent'), list) else []
    terminals = report.get('terminal', []) if isinstance(report.get('terminal'), list) else []
    viz = report.get('viz', []) if isinstance(report.get('viz'), list) else []
    report['counts'] = {
        'rest':metrics.get('http_reqs',{}).get('values',{}).get('count',0),
        'rest_error_rate':metrics.get('http_req_failed',{}).get('values',{}).get('rate'),
        'rest_check_rate':metrics.get('checks',{}).get('values',{}).get('rate'),
        'agent_first_text':sum(row.get('first_text_ms') is not None for row in agents),
        'agent_completed':sum(row.get('http') == 200 and row.get('completed') is True
                              and not row.get('error_type') and not row.get('error_event') for row in agents),
        'terminal_commands':sum('command_ms' in row for row in terminals),
        'viz_frames':sum('image_ms' in row for row in viz),
        'terminal_stops':sum(row.get('terminal') is True for row in report.get('cleanup',())),
        'viz_stops':sum(row.get('viz') is True for row in report.get('cleanup',())),
    }
    report['p95_ms'] = {
        'rest':metrics.get('http_req_duration',{}).get('values',{}).get('p(95)'),
        'first_text':percentile([row['first_text_ms'] for row in agents if row.get('first_text_ms') is not None]),
        'terminal':percentile([row['command_ms'] for row in terminals if 'command_ms' in row]),
        'viz':percentile([row['image_ms'] for row in viz if 'image_ms' in row]),
    }
    upstream = report.get('upstream', ())
    report['actual_upstream'] = dict(Counter(str(row.get('status', row['kind'])) for row in upstream
                                             if row['kind'] in ('http', 'transport_error')))
    memory = [row for row in upstream if row['kind'] == 'prompt_memory']
    report['agent_stages_p95_ms'] = {
        stage: percentile([row[stage] for row in agents if row.get(stage) is not None])
        for stage in ('response_headers_ms', 'ready_ms', 'context_window_ms')
    }
    report['agent_stages_p95_ms']['prompt_memory'] = percentile([row['elapsed_ms'] for row in memory])
    report['prompt_memory_reads'] = {'total': len(memory), 'single_file': sum(row['single_file'] for row in memory)}
    rest_started = metrics.get('mixed_read_started_epoch_ms',{}).get('values',{}).get('min')
    rest_ended = metrics.get('mixed_read_ended_epoch_ms',{}).get('values',{}).get('max')
    sessions = (agents, terminals, viz)
    if rest_started is not None and rest_ended is not None and all(sessions):
        first = max(rest_started, *(min(row['started_ms'] for row in rows) for rows in sessions))
        last = min(rest_ended, *(max(row['ended_ms'] for row in rows) for rows in sessions))
        report['overlap_ms'] = max(0, round(last - first))
    continued = report.get('during_provider', {})
    continued_terminal = continued.get('terminal', [])
    continued_viz = continued.get('viz', [])
    if rest_started is not None and rest_ended is not None and continued_terminal and continued_viz:
        first = max(rest_started, min(row['started_ms'] for row in continued_terminal),
                    min(row['started_ms'] for row in continued_viz))
        last = min(rest_ended, max(row['ended_ms'] for row in continued_terminal),
                   max(row['ended_ms'] for row in continued_viz))
        ended = {row['trace']:row['ended_ms'] for row in report['upstream'] if row['kind'] == 'model' and row.get('trace')}
        overlap_count = sum(row.get('trace') in ended and row['started_ms'] < last and ended[row['trace']] > first
                            for row in report['upstream'] if row['kind'] == 'http' and row['status'] == 200) if last > first else 0
        continued['overlapping_upstream_calls'] = overlap_count
        continued['terminal_commands'] = sum('command_ms' in row for row in continued_terminal)
        continued['viz_frames'] = sum('image_ms' in row for row in continued_viz)
        continued['viz_p95_ms'] = percentile([row['image_ms'] for row in continued_viz if 'image_ms' in row])
    n = report['counts']
    report['functional_passed'] = (
        n['rest'] == users*12 and n['rest_error_rate'] == 0 and n['rest_check_rate'] == 1
        and all(n[key] == users for key in ('agent_first_text','agent_completed','terminal_commands','viz_frames',
                                           'terminal_stops','viz_stops'))
        and report['actual_upstream'].get('200',0) >= users and report.get('overlap_ms',0) > 0
        and continued.get('terminal_commands') == users and continued.get('viz_frames') == users
        and continued.get('overlapping_upstream_calls',0) > 0
    )
    report['slo_passed'] = report['functional_passed'] and all(
        report['p95_ms'][key] < limit for key,limit in (('first_text',2000),('viz',200))
    ) and continued['viz_p95_ms'] < 200


def main(config, options):
    upstream_url = urlsplit(config['base_url'])
    assert upstream_url.scheme == 'https' and upstream_url.hostname == 'api.deepseek.com'
    assert upstream_url.username is None and upstream_url.password is None
    evidence_root = ROOT / 'tmp'
    evidence_root.mkdir(exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix='ff-j21-mixed-', dir=evidence_root))
    print('MIXED_EVIDENCE', evidence, flush=True)
    grids = [(data, hashlib.sha256(data).hexdigest()) for data in
             (options.small_grid.read_bytes(), options.large_grid.read_bytes())]
    live = set(docker('ps','--no-trunc','-q').splitlines())
    users = options.users
    report = {'users':users,'provider':'DeepSeek','model':config['model'],'simulated_model_calls':0,
              'api_image':options.api_image,'web_image':options.web_image,
              'grids':[{'bytes':len(data),'sha256':digest} for data,digest in grids]}
    try:
        with ExitStack() as contexts:
            deployment = Deployment(contexts, evidence, options.api_image, options.web_image)
            docker('exec', deployment.worker, '/usr/local/bin/python', '-B', '-c',
                         'from cryptography.fernet import Fernet; from pathlib import Path; '
                         'Path("/work/data/live-vault").write_bytes(Fernet.generate_key()); '
                         f'Path("/work/data/llm-allowance").write_text("{users + 2}")')
            deployment.env['VIBEFLOW_VAULT_KEY_PATH'] = '/work/data/live-vault'
            deployment.flags.extend(('-e','VIBEFLOW_VAULT_KEY_PATH'))
            a_id,a = deployment.api(deployment.a)
            b_id,b = deployment.api(deployment.b)
            _,public = deployment.web()
            try:
                asyncio.run(run(deployment, (a,b), public, config, users, report, options.k6, grids))
            finally:
                report['upstream'] = []
                for container, name in ((a_id,deployment.a),(b_id,deployment.b)):
                    output = docker('exec',container,'/bin/sh','-c',
                                    f'if test -f /work/data/upstream-{name}.jsonl; then cat /work/data/upstream-{name}.jsonl; fi')
                    report['upstream'].extend(json.loads(line) for line in output.splitlines())
    except Exception as error:
        report['failure'] = {'type':type(error).__name__,
                             'detail':str(error).replace(config['key'],'[REDACTED]')[:500]}
    finally:
        report['original_services'] = live == set(docker('ps','--no-trunc','-q').splitlines())
        summarize(report)
        data = json.dumps(report, indent=2, ensure_ascii=False)
        assert config['key'] not in data
        (evidence / f'mixed-{users}.json').write_text(data + '\n', encoding='utf-8')
        summary = {k:report[k] for k in ('counts','p95_ms','agent_stages_p95_ms','prompt_memory_reads',
                                         'actual_upstream','functional_passed','slo_passed',
                                         'original_services')}
        summary['during_provider'] = {k:report.get('during_provider',{}).get(k) for k in
                                      ('terminal_commands','viz_frames','viz_p95_ms','overlapping_upstream_calls')}
        print('MIXED_RESULT',json.dumps(summary,ensure_ascii=False),flush=True)
    return not report['functional_passed'] or not report['original_services']


def input_file(value):
    path = Path(value).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise argparse.ArgumentTypeError(f'File is missing or empty: {path}')
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--users', type=int, choices=(2,50), default=50)
    parser.add_argument('--k6', type=input_file, required=True, help='Linux amd64 k6 executable')
    parser.add_argument('--small-grid', type=input_file, required=True, help='Ordinary VTP mesh')
    parser.add_argument('--large-grid', type=input_file, required=True, help='Million-cell VTP mesh')
    parser.add_argument('--api-image', default=API_IMAGE)
    parser.add_argument('--web-image', default=WEB_IMAGE)
    options = parser.parse_args()
    raise SystemExit(main(asyncio.run(source(options.source_profile_id)), options))
