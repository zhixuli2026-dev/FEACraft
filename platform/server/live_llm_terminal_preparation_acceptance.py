"""Actual WebSocket startup, Windows/WSL PTYs, and S3 preparation faults."""
import asyncio
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
import uuid

import httpx
import websockets

from live_llm_job_repeat_acceptance import main, put


FIXTURE = r'''
import asyncio
from pathlib import Path
from threading import Event, Lock
from fastapi import Depends
from sqlalchemy import select
from app import storage
from app.config import get_settings
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import NativeSession
from app.routers import terminal
from app.services import request_deadline, staging_download, terminal_records
from app.services.node_storage_budget import DIRECTORY

pause = None
pause_lock = Lock()
openings = set()
original_prepare = storage.prepare_job_staging
original_download = staging_download.download
original_open = terminal._new_session
original_gate = terminal_records.register_gate


def hold(mode):
    with pause_lock:
        gate = pause
        deadline = request_deadline.current()
        if gate is None or gate['mode'] != mode or deadline is None or gate['release'].is_set():
            return
        gate['deadlines'].append(deadline)
    if not gate['release'].wait(35):
        raise TimeoutError('The actual terminal I/O barrier was not released')


def prepare(prefix, stamp):
    stage = original_prepare(prefix, stamp)
    hold('snapshot')
    return stage


def download(prefix, path, record, target):
    digest = original_download(prefix, path, record, target)
    if path == 'Ref/source.txt':
        hold('download')
    return digest


async def opening(*args, **kwargs):
    task = asyncio.current_task()
    openings.add(task)
    try:
        return await original_open(*args, **kwargs)
    finally:
        openings.discard(task)


async def gated(owner, transport):
    result = await original_gate(owner, transport)
    gate = pause
    if gate is not None and gate['mode'] in {'gate','gate_with_unknown_evidence'}:
        if gate['mode'] == 'gate_with_unknown_evidence':
            root = storage._fs_root()/'.terminal-export'/Path(transport.profile['command'][-1]).name
            (root/'unverified-evidence.txt').write_text('retain this unknown resource',encoding='utf8')
        gate['registered'] = True
        while not gate['release'].is_set():
            await asyncio.sleep(.05)
    return result


storage.prepare_job_staging = prepare
staging_download.download = download
terminal._new_session = opening
terminal_records.register_gate = gated


@app.post('/api/v1/testing/terminal-prepare')
async def arm_terminal(body: dict, user=Depends(get_current_user)):
    global pause
    with pause_lock:
        if pause is not None:
            pause['release'].set()
        mode = body['mode']
        assert mode in {'snapshot', 'download', 'gate', 'gate_with_unknown_evidence', 'off'}
        pause = {'mode': mode, 'release': Event(), 'deadlines': [], 'registered': False} if mode != 'off' else None
        get_settings().terminal_prepare_timeout_seconds = body['seconds']
    return {'mode': mode}


@app.get('/api/v1/testing/terminal-prepare')
async def terminal_prepare_state(user=Depends(get_current_user)):
    with pause_lock:
        deadlines = list(pause['deadlines']) if pause else []
        registered = pause is not None and pause['registered']
    async with SessionLocal() as db:
        rows = (await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id)
                                  .order_by(NativeSession.created_at))).all()
    root = storage._fs_root()
    return {'blocked': len(deadlines), 'expired': bool(deadlines) and all(d.expired for d in deadlines),
        'gate_registered': registered, 'openings': len(openings), 'cancelling': sum(t.cancelling()>0 for t in openings),
        'stage_entries': len(list((root / '.staging').glob('*'))),
        'node_claims': len(list((root / DIRECTORY).glob('*.json'))),
        'rows': [{**terminal_records.public(row), 'stage': json.loads(row.profile_json)['staging_prefix']} for row in rows]}
'''


async def exercise(api, remote, *, sandbox=False):
    control = '/testing/terminal-prepare'
    root = Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])
    base = api.base.replace('http:', 'ws:') + '/ws/terminal?'

    def connect(shell='cmd', sid=None):
        query = {'token': api.token, 'case': api.case_id, 'shell': shell}
        if sid is not None:
            query['session'] = sid
        return websockets.connect(base+urlencode(query), proxy=None, close_timeout=3)

    async def call(method, path, body=None):
        return await asyncio.to_thread(api.call, method, path, body)

    async def arm(mode, seconds=90):
        assert (await call('POST', control, {'mode': mode, 'seconds': seconds}))['mode'] == mode

    async def wait_state(predicate, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            one = await call('GET', control)
            if predicate(one):
                return one
            await asyncio.sleep(.1)
        raise AssertionError('Terminal state did not arrive: '+str({k:v for k,v in one.items() if k!='rows'}))

    async def control_frame(ws, kind):
        async with asyncio.timeout(45):
            while True:
                frame = await ws.recv()
                if isinstance(frame, str):
                    message = json.loads(frame)
                    if message['type'] == kind:
                        return message
                    assert message['type'] != 'error', message

    def settled(state):
        return state['openings'] == state['stage_entries'] == state['node_claims'] == 0

    report = {'store': 's3' if remote else 'fs', 'checks': []}
    if remote:
        await arm('snapshot')
        try:
            async with connect() as ws:
                await wait_state(lambda state: state['blocked']==1 and state['stage_entries']>=2 and not state['rows'])
                await asyncio.wait_for(asyncio.to_thread(put, api, 'Ref/source.txt', 'UPDATED SOURCE'), 10)
                await arm('off')
                refused = await control_frame(ws, 'error')
                assert (refused['status'],refused['code']) == (409,'input_stale'), refused
        finally:
            await arm('off')
        assert not (await wait_state(settled))['rows']
        report['checks'].append('same-case write during snapshot pause, stale startup refused, stage removed')

        await arm('snapshot')
        try:
            async with connect() as ws:
                await wait_state(lambda state: state['blocked']==1)
                await ws.close()
                cancelled = await wait_state(lambda state: state['cancelling']==1)
                assert cancelled['stage_entries']>=2 and not cancelled['rows'], cancelled
        finally:
            await arm('off')
        assert not (await wait_state(settled))['rows']
        report['checks'].append('pre-ready WebSocket disconnect drained real completed snapshot without allocating a shell')

        await arm('download', 14)
        try:
            async with connect() as one, connect() as two:
                try:
                    holds = await wait_state(lambda state: state['blocked']==2 and state['expired'])
                    assert holds['node_claims']>=2 and holds['openings']==2 and not holds['rows'], holds
                    async with connect() as third:
                        refusal=await control_frame(third, 'error')
                        assert (refusal['status'],refusal['code']) == (429,'terminal_preparation_busy'), refusal
                finally:
                    await arm('off')
                for ws in (one, two):
                    timeout=await control_frame(ws, 'error')
                    assert (timeout['status'],timeout['code']) == (504,'terminal_preparation_timeout'), timeout
        finally:
            await arm('off')
        assert not (await wait_state(settled))['rows']
        report['checks'].append('expired S3 reads held their two slots until release; overload 429 and cleanup before 504')

    for gated_shell in (('wsl','wsl-sandbox') if sandbox else ('wsl',)):
        await arm('gate')
        try:
            async with connect(gated_shell) as ws:
                gated = await wait_state(lambda state: state['gate_registered'], timeout=90)
                candidate = gated['rows'][-1]
                assert candidate['shell']==gated_shell and candidate['process']['gate']['process'], candidate
                await ws.close()
                ended = await wait_state(lambda state: settled(state) and state['rows'][-1]['cleanup_verified'] is True,
                                         timeout=55)
                assert ended['rows'][-1]['id']==candidate['id'] and ended['rows'][-1]['phase']=='failed', ended
        finally:
            await arm('off')
        report['checks'].append(gated_shell+' gate cancelled before shell release with verified process stop')

    case = await call('GET', api.prefix)
    shells = []
    for shell in ('cmd','wsl'):
        marker = 'ACTUAL_'+uuid.uuid4().hex
        output = 'Ref/terminal-'+shell+'.txt'
        command = ('echo '+marker+'>Ref\\terminal-cmd.txt\r' if shell=='cmd'
                   else 'printf "%s:%s" "$(id -u)" "'+marker+'" > '+output+'\r')
        expected = marker if shell=='cmd' else '65534:'+marker
        async with connect(shell) as ws:
            await ws.send(json.dumps({'type':'resize','cols':91,'rows':29}))
            await ws.send(json.dumps({'type':'input','data':command}))
            ready = await control_frame(ws, 'ready')
            resize = await control_frame(ws, 'resize_accepted')
            sid = ready['session']
            assert ready['alive'] and (resize['cols'],resize['rows'])==(91,29)
            state = await wait_state(lambda state: any(row['id']==sid for row in state['rows']))
            row = next(row for row in state['rows'] if row['id']==sid)
            actual_file = root/(row['stage'] or case['storage_prefix'])/output
            end = time.monotonic()+20
            observed = None
            while time.monotonic()<end:
                try:observed=actual_file.read_text(encoding='utf8').strip()
                except (FileNotFoundError, PermissionError):pass
                if observed==expected:break
                await asyncio.sleep(.1)
            assert observed==expected, (shell, observed)
        async with connect(shell, sid) as ws:
            attached = await control_frame(ws,'ready')
            assert attached['session']==sid and attached['alive'] is True
        async with httpx.AsyncClient(base_url=api.base+'/api/v1', timeout=60, trust_env=False,
                                     headers={'Authorization':'Bearer '+api.token}) as client:
            response = await client.delete('/terminal/sessions/'+sid)
            assert response.status_code==200, response.text
        done = await call('GET','/terminal/sessions/'+sid)
        content = await call('GET',api.prefix+'/file?'+urlencode({'path':output}))
        assert done['phase']=='closed' and done['cleanup_verified'] is True and done['stop']['verified'] is True
        assert content['content'].strip()==expected
        shells.append({'shell':shell,'uid':65534 if shell=='wsl' else None,'early_input':True,'reattached':True,
                       'actual_output_persisted':True,'verified_stop':True})
    report['shells']=shells
    if sandbox:
        report['unreleased_private_images'] = len(list(root.glob('.terminal-export/*/volume.ext4')))
        assert report['unreleased_private_images']==0, 'Cancelled sandbox still occupies an image'
        await arm('gate_with_unknown_evidence')
        try:
            async with connect('wsl-sandbox') as ws:
                gated=await wait_state(lambda state:state['gate_registered'],timeout=90)
                candidate=gated['rows'][-1]
                await ws.close()
                retained=await wait_state(lambda state:settled(state) and state['rows'][-1]['phase']=='failed',timeout=55)
                row=retained['rows'][-1]
                assert row['id']==candidate['id'] and row['cleanup_verified'] is True, row
                assert row['error']=='SandboxStartupImageCleanupIncomplete', row
        finally:
            await arm('off')
        images = list(root.glob('.terminal-export/*/volume.ext4'))
        assert len(images)==1 and images[0].stat().st_size==128*1024*1024
        assert (images[0].parent/'unverified-evidence.txt').read_text(encoding='utf8')=='retain this unknown resource'
        report['checks'].append('unknown sandbox evidence and its image were retained after verified stop')
        marker = 'SANDBOX_'+uuid.uuid4().hex
        async with connect('wsl-sandbox') as ws:
            ready = await control_frame(ws, 'ready')
            command = 'printf %s "'+marker+'" > Ref/sandbox-terminal.txt; printf "\\n%s\\n" "'+marker[:10]+'""'+marker[10:]+'"\r'
            await ws.send(json.dumps({'type':'input','data':command}))
            # The expected output is a full line. A copy of the typed command
            # contains it only inside the redirection expression.
            received=b''
            async with asyncio.timeout(30):
                while b'\n'+marker.encode()+b'\r\n' not in received.replace(b'\r\r\n',b'\r\n'):
                    data = await ws.recv()
                    assert isinstance(data,bytes), data
                    received=(received+data)[-16384:]
            await ws.send(json.dumps({'type':'input','data':'exit\r'}))
            assert (await control_frame(ws,'exit'))['code']==0
        closed = await call('GET','/terminal/sessions/'+ready['session'])
        saved = await call('GET',api.prefix+'/file?path=Ref/sandbox-terminal.txt')
        assert closed['phase']=='closed' and closed['cleanup_verified'] is True and saved['content']==marker
        assert len(list(root.glob('.terminal-export/*/volume.ext4')))==2
        report['checks'].append('actual sandbox command, export and readback after normal exit')
    await wait_state(settled)
    report['stage_and_claims_drained']=True
    return report


def scenario(api, *, store=None, **_):
    put(api,'Ref/source.txt','ORIGINAL SOURCE')
    report=asyncio.run(exercise(api, store is not None, sandbox=os.getenv('VIBEFLOW_TEST_TERMINAL_SANDBOX')=='1'))
    (api.root/'terminal-preparation.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE))
