"""Actual case-bound cmd and non-root WSL work-directory storage limits."""
import asyncio
import json
import math
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import time

import aiohttp

from app.config import Settings
from app.services.owned_process import wsl_path
from app.services.process_identity_native import probe
from terminal_execution_smoke import LEAF, SPAWN, connect, detached, detached_exited, exited, ready
from testsupport import IsolatedServer, SERVER


MIB = 1024 * 1024
SEED = b'RB13_EXISTING_USER_FILE\n'
GROWTH = 'rb13-terminal-growth.bin'
MARKER = '.rb13-acceptance-case'
FIXTURE = '''
import json, os
from pathlib import Path
from fastapi import Depends
from app.config import get_settings
from app.deps import get_current_user
from app.main import app
from app.routers import terminal
from app.services.process_identity_native import observe

root=Path(__file__).parent
os.environ.update(VIBEFLOW_TERMINAL_SESSION_LIFETIME_SECONDS='0',
    VIBEFLOW_TERMINAL_SESSION_MAX_OUTPUT_BYTES='0',VIBEFLOW_TERMINAL_SESSION_WORKSPACE_LIMIT_MB='0',
    VIBEFLOW_EXECUTION_STORAGE_POLL_SECONDS='.1',VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0')
get_settings.cache_clear()

@app.post('/fixture/terminal-storage-settings')
def settings(body: dict,user=Depends(get_current_user)):
    os.environ['VIBEFLOW_TERMINAL_SESSION_WORKSPACE_LIMIT_MB']=str(body['workspace_mb'])
    os.environ['VIBEFLOW_NODE_STORAGE_MIN_FREE_MB']=str(body['min_free_mb'])
    os.environ['VIBEFLOW_STORAGE_MEASUREMENT_MAX_ENTRIES']=str(body['entries'])
    get_settings.cache_clear()
    cfg=get_settings()
    return {'workspace_mb':cfg.terminal_session_workspace_limit_mb,'min_free_mb':cfg.node_storage_min_free_mb,
            'entries':cfg.storage_measurement_max_entries}

@app.get('/fixture/terminal-owner')
def actual_owner(user=Depends(get_current_user)):
    return {'birth':observe(os.getpid()),'sessions':sorted(
        identifier for identifier, session in terminal._sessions.items() if session.user_id==user.id)}
'''

WRITER = r'''
import os, sys, time
from pathlib import Path

mode, release_path = sys.argv[1:]
assert Path('.rb13-acceptance-case').read_bytes()==b'RB13_EXISTING_USER_FILE\n'
if os.name!='nt': assert os.getuid()==65534
target=Path('rb13-terminal-growth.bin')
with target.open('xb') as output:
    output.write(b'R'*(256*1024)); output.flush(); os.fsync(output.fileno())
print('RB13_WRITER_WAITING',flush=True)
limit=time.monotonic()+90
while not Path(release_path).exists():
    if time.monotonic()>=limit: raise TimeoutError('Isolated writer was not released')
    time.sleep(.03)
if mode=='entries':
    folder=Path('entry-budget-fixture');folder.mkdir()
    for i in range(144): (folder/str(i)).touch()
else:
    with target.open('ab') as output:
        output.write(b'R'*(1024*1024));output.flush();os.fsync(output.fileno())
print('RB13_WRITER_GREW',flush=True)
while time.monotonic()<limit: time.sleep(.1)
'''


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n',encoding='utf8')


def set_limit(api,workspace_mb=0,min_free_mb=0,entries=1000000):
    value={'workspace_mb':workspace_mb,'min_free_mb':min_free_mb,'entries':entries}
    assert api.call('POST','/fixture/terminal-storage-settings',value,api_prefix=False)==value


def owner(api):
    return api.call('GET','/fixture/terminal-owner',api_prefix=False)


def session(api, identifier):
    return api.call('GET','/terminal/sessions/'+identifier)


def new_case(api,name):
    workspace=api.call('GET','/workspaces')[0]
    case=api.call('POST',f'/workspaces/{workspace["id"]}/cases',{'name':name})
    user=api.call('GET','/auth/me')['id']
    path=api.root/'files'/'workspaces'/str(user)/'cases'/str(case['id'])
    assert path.is_dir() and path.resolve().is_relative_to((api.root/'files').resolve())
    (path/MARKER).write_bytes(SEED)
    return case['id'], path


async def frame(ws):
    async with asyncio.timeout(35):
        while True:
            data=await ws.receive()
            if data.type==aiohttp.WSMsgType.TEXT:
                value=json.loads(data.data)
                if value['type'] in ('error','exit','ready'):
                    return value
            elif data.type!=aiohttp.WSMsgType.BINARY:
                raise AssertionError(data)


async def wait_until(predicate, timeout=35):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=predicate()
        if result:
            return result
        await asyncio.sleep(.12)
    raise TimeoutError('Actual terminal did not reach the expected state')


async def start_writer(api,ws,shell,cwd,mode,release):
    source=api.root/'workspace-writer.py'
    if shell=='cmd':
        command=subprocess.list2cmdline([sys._base_executable,'-I','-S','-u',str(source),mode,str(release)])+'\r\n'
    else:
        command=shlex.join(['python3','-I','-S','-u',wsl_path(source),mode,wsl_path(release)])+'\n'
    await ws.send_json({'type':'input','data':command})
    target=cwd/GROWTH
    await wait_until(lambda:target.is_file() and target.stat().st_size==256*1024)
    return target


async def preflight_rejection(api,client,shell,case,cwd,kind):
    if kind=='initial_size':
        set_limit(api,workspace_mb=1)
        (cwd/GROWTH).write_bytes(b'B'*(MIB+8))
        expected='执行目录用量'
    else:
        free=shutil.disk_usage(cwd).free
        set_limit(api,workspace_mb=16,min_free_mb=math.ceil(free/MIB)+256)
        expected='磁盘可用空间'
    before=api.call('GET','/terminal/sessions?include_closed=true')
    async with await connect(client,api,shell,case=case) as ws:
        status=await frame(ws)
        assert (status.get('type'),status.get('code'),status.get('reason'),status.get('status'))==(
            'error','terminal_resource_limit','disk_limit',409),status
        assert expected in status['message']
        close=await ws.receive()
        assert close.type==aiohttp.WSMsgType.CLOSE and close.data==1013
    assert api.call('GET','/terminal/sessions?include_closed=true')==before
    assert (cwd/MARKER).read_bytes()==SEED
    return {'error':status,'native_record_created':False}


async def default_ignores_later_limit(api,client,shell):
    set_limit(api)
    case,cwd=new_case(api,shell+' default directory behavior')
    async with await connect(client,api,shell,case=case) as ws:
        identifier=await ready(ws)
        before=session(api,identifier)
        set_limit(api,workspace_mb=1)
        release=api.root/('default-'+identifier+'.go')
        target=await start_writer(api,ws,shell,cwd,'bytes',release)
        release.touch()
        await wait_until(lambda:target.stat().st_size>MIB)
        await asyncio.sleep(.65)
        assert session(api,identifier)['phase']=='active'
    async with await connect(client,api,shell,identifier=identifier) as ws:
        assert await ready(ws)==identifier and session(api,identifier)['process']==before['process']
    assert api.call('DELETE','/terminal/sessions/'+identifier)['ok'] is True
    stopped=session(api,identifier)
    assert stopped['phase']=='closed' and stopped['cleanup_verified'] is True
    await exited(stopped)
    assert target.stat().st_size==MIB+256*1024 and (cwd/MARKER).read_bytes()==SEED
    return {'id':identifier,'continued_and_attached':True,'workspace_bytes':target.stat().st_size}


async def retained_workspace_limit(api,client,shell,reason):
    set_limit(api)
    case,cwd=new_case(api,shell+' '+reason+' actual writer')
    set_limit(api,workspace_mb=1)
    async with await connect(client,api,shell,case=case) as ws:
        identifier=await ready(ws)
        before=session(api,identifier)
        if shell=='wsl':
            assert before['process']['gate']['process']['uid']==65534
        member=await detached(api,ws,shell,identifier)
        release=api.root/('limited-'+identifier+'.go')
        target=await start_writer(api,ws,shell,cwd,'entries' if reason=='disk_unverified' else 'bytes',release)
        await asyncio.sleep(.35)
        assert session(api,identifier)['phase']=='active'
    if reason=='disk_limit':
        # Settings can affect a later launch; the open case's cap is frozen.
        set_limit(api)
    else:
        # Install this global scan limit only after normal account admission
        # and the real writer are ready. The detached case-scanner exercises it.
        set_limit(api,workspace_mb=1,entries=128)
    release.touch()

    def completed():
        value=session(api,identifier)
        return value if value['phase']=='failed' else None

    final=await wait_until(completed,timeout=40)
    assert final['error']=='TerminalResourceLimit' and final['cleanup_verified'] is True
    assert final['stop']['reason']==reason and final['process']==before['process']
    assert identifier not in owner(api)['sessions']
    await exited(final)
    await detached_exited(member)
    set_limit(api)
    async with await connect(client,api,shell,identifier=identifier) as ws:
        assert await ready(ws)==identifier
        status=await frame(ws)
        assert status['code']=='terminal_resource_limit' and status['reason']==reason,status
    assert (cwd/MARKER).read_bytes()==SEED
    extra={}
    if reason=='disk_limit':
        assert target.read_bytes()==b'R'*(MIB+256*1024)
    else:
        assert target.stat().st_size==256*1024
        assert (cwd/'entry-budget-fixture').is_dir() and any((cwd/'entry-budget-fixture').iterdir())
        extra['retained_directory_entries']=sum(1 for _ in cwd.rglob('*'))
        assert extra['retained_directory_entries']>128
    return {'id':identifier,'reason':reason,'disconnected_when_exceeded':True,'final':final,
            'actual_detached_child':member,'retained_bytes':target.stat().st_size,**extra}


async def wait_api_exit(birth,port):
    def stopped():
        with socket.socket() as connection:
            connection.settimeout(.5)
            return probe(birth)['state']=='exited' and connection.connect_ex(('127.0.0.1',port))!=0
    await wait_until(stopped,timeout=15)
    return {'actual_pid':birth['pid'],'port':port,'process_exited':True,'port_closed':True}


async def close_remaining_fixture_sessions(api,report):
    for identifier in owner(api)['sessions']:
        try:
            api.call('DELETE','/terminal/sessions/'+identifier)
            state=session(api,identifier)
            assert state['cleanup_verified'] is True
            await exited(state)
            report.setdefault('failure_session_cleanup',[]).append({'id':identifier,'verified':True,'stop':state['stop']})
        except Exception as error:
            report.setdefault('cleanup_failures',[]).append({'id':identifier,'error':type(error).__name__})


async def verify():
    api=IsolatedServer('terminal-workspace-budget',app='terminal_workspace_fixture:app')
    print('TERMINAL_STORAGE_EVIDENCE',api.root,flush=True)
    report={'passed':False,'database':'sqlite','storage':'fs','running':{},'no_release':{},'api_exits':[],
            'actual_model_requests':0,'mock_llm_calls':0}
    (api.root/'terminal_workspace_fixture.py').write_text(FIXTURE,encoding='utf8')
    (api.root/'workspace-writer.py').write_text(WRITER,encoding='utf8')
    (api.root/'terminal_leaf.py').write_text(LEAF,encoding='utf8')
    (api.root/'terminal_spawn.py').write_text(SPAWN,encoding='utf8')
    assert Settings.model_fields['terminal_session_workspace_limit_mb'].default==0
    last_api=None
    try:
        with api:
            api.create_case('Terminal workspace test bootstrap')
            email=api.call('GET','/auth/me')['email']
            birth=owner(api)['birth']
            last_api=(birth,api.port)
            try:
                async with aiohttp.ClientSession() as client:
                    for shell in ('cmd','wsl'):
                        cases=report['no_release'][shell]=[]
                        for kind in ('initial_size','initial_low_water'):
                            set_limit(api)
                            case,cwd=new_case(api,shell+' '+kind)
                            cases.append(await preflight_rejection(api,client,shell,case,cwd,kind))
                        running=report['running'][shell]=[]
                        running.append(await default_ignores_later_limit(api,client,shell))
                        running.append(await retained_workspace_limit(api,client,shell,'disk_limit'))
                    report['running']['cmd'].append(await retained_workspace_limit(api,client,'cmd','disk_unverified'))
                    assert owner(api)['sessions']==[]
            finally:
                try:
                    await close_remaining_fixture_sessions(api,report)
                except Exception as error:
                    report.setdefault('cleanup_failures',[]).append({'error':type(error).__name__})
            assert not report.get('cleanup_failures')
        report['api_exits'].append(await wait_api_exit(birth,api.port))
        shutil.copyfile(api.root/'api.log',api.root/'startup-first-api.log')

        with api:
            api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
            new_birth=owner(api)['birth']
            last_api=(new_birth,api.port)
            assert new_birth!=birth and owner(api)['sessions']==[]
            async with aiohttp.ClientSession() as client:
                for shell,cases in report['running'].items():
                    for case in (case for case in cases if 'reason' in case):
                        final=session(api,case['id'])
                        assert all(final[k]==case['final'][k] for k in ('process','stop','error','ended_at','tail'))
                        async with await connect(client,api,shell,identifier=case['id']) as ws:
                            assert await ready(ws)==case['id']
                            status=await frame(ws)
                            assert status['code']=='terminal_resource_limit' and status['reason']==case['reason']
        report['api_exits'].append(await wait_api_exit(new_birth,api.port))
        report['cold_replay']=True
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:550]}
        if last_api and not any(receipt['port']==last_api[1] for receipt in report['api_exits']):
            try:
                report['api_exits'].append(await wait_api_exit(*last_api))
            except Exception as cleanup:
                report.setdefault('cleanup_failures',[]).append({'error':type(cleanup).__name__})
        raise
    finally:
        write(api.root/'terminal-workspace-budget.json',report)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parents[0]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    asyncio.run(verify())
