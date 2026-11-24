"""Ordinary Windows / WSL work-directory limits with actual SeaweedFS staging."""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import shutil
import socket
import sys

import aiohttp
import httpx

from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from terminal_execution_smoke import LEAF, SPAWN, connect, detached, detached_exited, exited, ready
from terminal_workspace_budget_acceptance import (
    FIXTURE, GROWTH, MARKER, MIB, SEED, WRITER, close_remaining_fixture_sessions,
    frame, owner, session, set_limit, start_writer, wait_api_exit, wait_until, write,
)
from testsupport import IsolatedServer, SERVER


OBSERVE = '''
from contextlib import contextmanager
from contextvars import ContextVar
import re
from app.db import SessionLocal
from app.models import NativeSession
from app.services import s3_file_transactions, staging_sync
from fastapi import HTTPException

@app.get('/fixture/terminal-stage/{identifier}')
async def stage_identity(identifier: str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeSession,identifier)
        if row is None or row.created_by!=user.id:
            raise HTTPException(404)
        selected=json.loads(row.profile_json)
        if not re.fullmatch(r'\\.staging/job-[0-9]+-[0-9a-f]{32}',selected['staging_prefix'] or ''):
            raise HTTPException(409)
        return {name:selected[name] for name in ('source_prefix','staging_prefix')}

current_sync = ContextVar('current_terminal_sync', default=None)
current_move = ContextVar('current_terminal_move', default=None)
sync_original=staging_sync.sync_authorized
apply_original = s3_file_transactions._apply
copy_original = s3_file_transactions._temporary_copy


@contextmanager
def observed_copy(size):
    record = current_move.get()
    if record is not None:
        record['temporary_bytes'] = max(record['temporary_bytes'], size)
    with copy_original(size) as stream:
        yield stream


def observed_apply(case, directory, index, entry, side, expected_side, **options):
    stage = current_sync.get()
    if stage is None or side != 'after' or entry['after'] is None:
        return apply_original(case, directory, index, entry, side, expected_side, **options)
    record = {'path':entry['path'], 'bytes':entry['after']['size'], 'temporary_bytes':0,
              'used_staging_source':isinstance(options.get('retained_source'), staging_sync.StagingFile)}
    token = current_move.set(record)
    try:
        return apply_original(case, directory, index, entry, side, expected_side, **options)
    finally:
        current_move.reset(token)
        stage['installations'].append(record)


async def observed_sync(stage, prefix, case_id, authorize, **options):
    local=next((value for value in terminal._sessions.values() if value.staging_prefix==stage),None)
    state={'stage':stage,'source':prefix,'case_id':case_id,'session':local.id if local else None,
           'verified_native_stop_before_sync':local.cleanup_verified if local else None, 'installations':[]}
    token = current_sync.set(state)
    try:
        result=await sync_original(stage,prefix,case_id,authorize,**options)
        state['result']=result
        return result
    except Exception as error:
        state['failure']=type(error).__name__
        state['detail']=str(error)[:250]
        raise
    finally:
        current_sync.reset(token)
        with (root/'actual-staging-sync.jsonl').open('a',encoding='utf8') as witness:
            witness.write(json.dumps(state,ensure_ascii=False)+'\\n')
staging_sync.sync_authorized=observed_sync
s3_file_transactions._apply = observed_apply
s3_file_transactions._temporary_copy = observed_copy
'''

REMOTE_ORIGINAL=b'RB13_REMOTE_FILE_WON_CONFLICT\n'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def upload(api, case, name, data):
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      trust_env=False,timeout=42) as client:
        response=client.post(f'/cases/{case}/upload',params={'path':name,'expected_absent':'true'},
                             files={'file':(name,data,'application/octet-stream')})
    assert response.status_code==200,(response.status_code,response.text[:300])


def new_case(api,name):
    workspace=api.call('GET','/workspaces')[0]
    case=api.call('POST',f'/workspaces/{workspace["id"]}/cases',{'name':name})['id']
    upload(api,case,MARKER,SEED)
    return case


def read_file(api,case,name):
    return api.call('GET',f'/cases/{case}/raw?path={name}',raw=True)


def staged_directories(api):
    root=api.root/'files'/'.staging'
    return {p.name for p in root.glob('job-*')}


async def stage_rejection(api,client,shell,kind):
    set_limit(api)
    case=new_case(api,shell+' '+kind)
    if kind=='oversized-source':
        upload(api,case,GROWTH,b'B'*(MIB+8))
        set_limit(api,workspace_mb=1)
    else:
        free=shutil.disk_usage(api.root/'files').free
        set_limit(api,workspace_mb=16,min_free_mb=math.ceil(free/MIB)+256)
    before=api.call('GET','/terminal/sessions?include_closed=true')
    staging_before=staged_directories(api)
    async with await connect(client,api,shell,case=case) as ws:
        result=await frame(ws)
        assert result['type']=='error' and result['status']==409,result
        if kind=='oversized-source':
            assert (result['code'],result['reason'])==('terminal_resource_limit','disk_limit') and '执行目录用量' in result['message']
        else:
            # The existing staging reservation blocks before downloading any bytes.
            assert '节点缓存或磁盘剩余空间不足' in result['message']
    assert api.call('GET','/terminal/sessions?include_closed=true')==before
    assert staged_directories(api)==staging_before
    set_limit(api)
    assert read_file(api,case,MARKER)==SEED
    if kind=='oversized-source':
        assert read_file(api,case,GROWTH)==b'B'*(MIB+8)
    return {'shell':shell,'kind':kind,'receipt':result,'new_session':False,'staging_left':False}


async def runtime_limit(api,client,shell,*,conflict=False):
    set_limit(api)
    case=new_case(api,shell+(' concurrent ordinary file publication' if conflict else ' sync after disk stop'))
    set_limit(api,workspace_mb=1)
    async with await connect(client,api,shell,case=case) as ws:
        identifier=await ready(ws)
        before=session(api,identifier)
        if shell=='wsl':assert before['process']['gate']['process']['uid']==65534
        selected=api.call('GET','/fixture/terminal-stage/'+identifier,api_prefix=False)
        cwd=(api.root/'files'/selected['staging_prefix']).resolve(strict=True)
        assert cwd.parent==(api.root/'files'/'.staging').resolve() and (cwd/MARKER).read_bytes()==SEED
        manifest=cwd.parent/(cwd.name+'.manifest.json')
        assert json.loads(manifest.read_text(encoding='utf8'))['source_prefix']==selected['source_prefix']
        member=await detached(api,ws,shell,identifier)
        opener=api.root/('s3-growth-'+identifier+'.go')
        target=await start_writer(api,ws,shell,cwd,'bytes',opener)
        assert session(api,identifier)['phase']=='active'
    if conflict:
        # A real second writer wins in the source while the disconnected
        # terminal's private copy still differs from the committed object.
        await asyncio.to_thread(upload,api,case,GROWTH,REMOTE_ORIGINAL)
        assert read_file(api,case,GROWTH)==REMOTE_ORIGINAL
    set_limit(api)
    opener.touch()

    def terminal():
        value=session(api,identifier)
        return value if value['phase']=='failed' else None

    final=await wait_until(terminal,timeout=65)
    assert final['process']==before['process'] and final['stop']['scope_empty']
    assert final['error']=='TerminalResourceLimit' and final['cleanup_verified'] is True
    assert final['stop']['reason']=='disk_limit'
    await exited(final)
    await detached_exited(member)
    assert read_file(api,case,MARKER)==SEED
    events=[json.loads(line) for line in (api.root/'actual-staging-sync.jsonl').read_text(encoding='utf8').splitlines()]
    one,=[event for event in events if event['session']==identifier]
    assert one['stage']==selected['staging_prefix'] and one['verified_native_stop_before_sync'] is True
    measured=b'R'*(MIB+256*1024)
    source=read_file(api,case,GROWTH)
    if conflict:
        assert one['failure']=='StagingConflict' and '远端文件已存在' in one['detail'],one
        assert source==REMOTE_ORIGINAL and target.read_bytes()==measured and manifest.is_file()
        assert not (cwd.parent/(cwd.name+'.terminal-cleanup.json')).exists()
    else:
        assert one['result']['uploaded']==1 and one['result']['deleted']==0,one
        assert source==measured and not cwd.exists() and not manifest.exists()
        assert one['installations'] == [
            {'path':GROWTH,'bytes':len(measured),'temporary_bytes':0,'used_staging_source':True}], one
    return {'shell':shell,'id':identifier,'case_id':case,'stop':final['stop'],'process':final['process'],
            'detached_child':member,'sync':one,'source_current_sha256':digest(source),
            'stage_manifest_retained':conflict,'stage_expected_bytes':len(measured),
            'stage_local_file_sha256':digest(measured),'bound_stage':selected}


async def verify(database_url,store):
    api=IsolatedServer('s3-terminal-workspace-budget',app='terminal_workspace_fixture:app',database_url=database_url)
    report={'passed':False,'storage':'seaweedfs','database':'postgresql' if database_url else 'sqlite',
            'store_root':str(store.root),'no_release':[],'finished':[],'api_exits':[],
            'actual_model_requests':0,'mock_llm_calls':0}
    print('TERMINAL_S3_STORAGE_EVIDENCE',api.root,flush=True)
    identity=store.root/'s3-identity.json'
    public_settings={key:value for key,value in store.settings().items()
                     if key not in {'VIBEFLOW_S3_ACCESS_KEY','VIBEFLOW_S3_SECRET_KEY'}}
    setup=f'''import os, json
from pathlib import Path
os.environ.update({public_settings!r})
identity=json.loads(Path({str(identity)!r}).read_text(encoding='utf8'))['identities'][0]['credentials'][0]
os.environ.update(VIBEFLOW_S3_ACCESS_KEY=identity['accessKey'],VIBEFLOW_S3_SECRET_KEY=identity['secretKey'])
del identity
'''
    (api.root/'terminal_workspace_fixture.py').write_text(setup+FIXTURE+OBSERVE,encoding='utf8')
    for name,code in (('workspace-writer.py',WRITER),('terminal_leaf.py',LEAF),('terminal_spawn.py',SPAWN)):
        (api.root/name).write_text(code,encoding='utf8')
    last_api=None
    try:
        anonymous=httpx.get(store.endpoint+'/',trust_env=False,timeout=10)
        signed=store.raw_client.list_buckets()
        assert anonymous.status_code==403 and signed['ResponseMetadata']['HTTPStatusCode']==200
        assert store.bucket in {item['Name'] for item in signed['Buckets']}
        report['s3_authentication']={'unsigned_status':anonymous.status_code,'signed_status':200}
        with api:
            api.create_case('Actual S3 terminal workspace quota')
            email=api.call('GET','/auth/me')['email']
            last_api=(owner(api)['birth'],api.port)
            try:
                async with aiohttp.ClientSession() as client:
                    for shell in ('cmd','wsl'):
                        report['no_release'].append(await stage_rejection(api,client,shell,'oversized-source'))
                        report['finished'].append(await runtime_limit(api,client,shell))
                    report['no_release'].append(await stage_rejection(api,client,'cmd','before-download-low-water'))
                    report['finished'].append(await runtime_limit(api,client,'cmd',conflict=True))
                    assert owner(api)['sessions']==[]
            finally:
                try:await close_remaining_fixture_sessions(api,report)
                except Exception as error:report.setdefault('cleanup_failures',[]).append({'error':type(error).__name__})
            assert not report.get('cleanup_failures')
        report['api_exits'].append(await wait_api_exit(*last_api))
        shutil.copyfile(api.root/'api.log',api.root/'startup-first-api.log')
        with api:
            api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
            last_api=(owner(api)['birth'],api.port)
            async with aiohttp.ClientSession() as client:
                for item in report['finished']:
                    identifier=item['id']
                    observed=session(api,identifier)
                    assert observed['stop']==item['stop'] and observed['process']==item['process']
                    assert digest(read_file(api,item['case_id'],GROWTH))==item['source_current_sha256']
                    async with await connect(client,api,item['shell'],identifier=identifier) as ws:
                        assert await ready(ws)==identifier
                        status=await frame(ws)
                        assert status['code']=='terminal_resource_limit' and status['reason']=='disk_limit'
        report['api_exits'].append(await wait_api_exit(*last_api))
        report['cold_replay']=True
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:600]}
        if last_api and not any(item['port']==last_api[1] and item['actual_pid']==last_api[0]['pid'] for item in report['api_exits']):
            try:report['api_exits'].append(await wait_api_exit(*last_api))
            except Exception as cleanup:report.setdefault('cleanup_failures',[]).append({'error':type(cleanup).__name__})
        raise
    finally:
        write(api.root/'terminal-s3-workspace-budget.json',report)
    return api.root


async def main(args):
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',volume_max=6,
                      authenticated=True)
    try:
        async with disposable_database(args.postgres) as database:
            with store:
                await verify(database,store)
    finally:
        if store.proc is not None:
            port=int(store.endpoint.rsplit(':',1)[-1])
            with socket.socket() as probe:
                probe.settimeout(1)
                port_closed=probe.connect_ex(('127.0.0.1',port))!=0
            launcher_exited=store.proc.poll() is not None
            key_removed=not (store.root/'s3-identity.json').exists()
            write(store.root/'seaweed-exit.json',{'store_root':str(store.root),'port':port,'port_closed':port_closed,
                  'launcher_exited':launcher_exited,'exit_code':store.proc.returncode,'fixture_key_removed':key_removed})
            assert port_closed and launcher_exited and key_removed


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres',action='store_true')
    asyncio.run(main(parser.parse_args()))
