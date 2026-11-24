"""CLI API journals on disposable databases with real process gates and faults."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url

from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from app.services import process_identity_native as identity

WORKER = r'''
import importlib.util,json,os,subprocess,sys,time
from pathlib import Path
mode=sys.argv[1];root=Path(sys.argv[2]);spec=importlib.util.spec_from_file_location('birth',sys.argv[3])
birth=importlib.util.module_from_spec(spec);spec.loader.exec_module(birth)
(root/('cli-'+str(os.getpid())+'.json')).write_text(json.dumps({'mode':mode,'birth':birth.observe(os.getpid()),'cwd':os.getcwd()}))
def send(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
    request=json.loads(line);method=request.get('method');rid=request.get('id');params=request.get('params',{})
    if method=='initialize':
        if mode=='init_wait':
            (root/'init-held').touch()
            while True:time.sleep(.1)
        result={} if mode=='bad_init' else {'userAgent':'fixture/1','platformFamily':'windows','platformOs':'windows'}
    elif method=='initialized':
        if mode=='detached':
            options={'creationflags':subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
            (root/'leaf.json').write_text(json.dumps(birth.observe(child.pid)))
        continue
    elif method=='account/read':
        if mode=='eof':raise SystemExit(7)
        if mode=='account_error':
            send({'id':rid,'error':{'code':-32000,'message':'fixture account unavailable'}});continue
        result={'account':{'type':'chatgpt','email':'fixture@example.test','accessToken':'synthetic-do-not-publish'},'requiresOpenaiAuth':False}
    elif method=='model/list':result={'data':[{'id':'fixture','model':'fixture','displayName':'Fixture','accessToken':'synthetic-do-not-publish'}],'nextCursor':None}
    elif method=='windowsSandbox/readiness':result={'status':'invented' if mode=='bad_sandbox' else 'notConfigured','diagnostics':'synthetic-do-not-publish'}
    elif method=='thread/start':
        with (root/'thread-starts.log').open('a') as stream:stream.write(str(os.getpid())+'\n')
        if mode=='thread_wait':
            (root/'thread-held').touch()
            while True:time.sleep(.1)
        result={'thread':{'id':'thread-'+str(os.getpid())}}
    else:result={}
    send({'id':rid,'result':result})
'''

CONTROLLED = '''import asyncio,contextvars,copy,json,sys,time
from datetime import timedelta
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select,func,update
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import NativeCliSession,Conversation,Case,Job
from app.services import codex_sessions as sessions,codex_records as records,owned_process
from app.services.durable_events import now
root=Path(__file__).parent;mode='normal';unverified=set();saved={}
sessions.HEARTBEAT_INTERVAL=.1
sessions.command=lambda credential=None:[sys._base_executable,'-I','-S','-u',str(root/'cli.py'),mode,str(root),str(Path(records.identity.__file__))]
def selected_mode(owner):return sessions.SESSIONS[owner.id].selected['argv'][-3]
slow=contextvars.ContextVar('cli_slow_birth',default=False)
receipt_fail=contextvars.ContextVar('cli_receipt_failure',default=False)
commit=AsyncSession.commit
async def committing(db):
    if db.info.pop('cli_fail_commit',False) or receipt_fail.get():raise OSError('isolated CLI commit failure')
    return await commit(db)
AsyncSession.commit=committing
probe=records.verify_root
async def probing(birth):
    await probe(birth)
    if slow.get():await asyncio.sleep(.25)
records.verify_root=probing
register_root=records.register_root
async def registering(db,owner,birth,transport):
    selected=selected_mode(owner)
    if selected=='root_commit':
        await register_root(db,owner,birth,transport);db.info['cli_fail_commit']=True;return
    if selected in {'bad_birth','bad_nonce','foreign_birth'}:
        birth=copy.deepcopy(birth)
        if selected=='bad_birth':birth['host']['creation_time_100ns']=str(int(birth['host']['creation_time_100ns'])+1)
        elif selected=='bad_nonce':birth['launch_nonce']='0'*64
        else:
            birth['host']=json.loads((root/'sentinel.json').read_text());birth['pid']=birth['host']['pid']
    if selected=='expired_lease':
        row=await db.get(NativeCliSession,owner.id);row.lease_expires_at=now()+timedelta(seconds=.15);await db.flush()
    if selected=='permission_changed':
        row=await db.get(Conversation,sessions.SESSIONS[owner.id].conversation_id);row.permission_mode='approve_writes'
    if selected=='source_changed':
        row=await db.get(Case,sessions.SESSIONS[owner.id].case_id);row.s3_prefix += '-rebound'
    if selected=='unverified_stop':unverified.add(owner.nonce)
    token=slow.set(selected=='expired_lease')
    try:return await register_root(db,owner,birth,transport)
    finally:slow.reset(token)
records.register_root=registering
session_register_root=sessions.Session.register_root
async def session_root(self,birth):
    if selected_mode(self.owner)=='owner_changed':
        async with SessionLocal() as db:
            await db.execute(update(NativeCliSession).where(NativeCliSession.id==self.id).values(owner_token_sha256='0'*64))
            await db.commit()
    return await session_register_root(self,birth)
sessions.Session.register_root=session_root
session_prepare=sessions.Session.prepare
async def preparing_session(self):
    await session_prepare(self)
    if selected_mode(self.owner)=='admission_abort':raise ValueError('isolated pre-spawn abort')
sessions.Session.prepare=preparing_session
reading_row=sessions.row;held=set()
async def reading(identifier,user_id):
    session=sessions.SESSIONS.get(identifier)
    if session is not None and selected_mode(session.owner)=='response_hold' and identifier not in held:
        held.add(identifier);(root/'response-held.json').write_text(json.dumps({'id':identifier}))
        while not (root/'release-response').exists():await asyncio.sleep(.02)
    return await reading_row(identifier,user_id)
sessions.row=reading
register_bridge=records.register_bridge
async def bridging(db,owner,transport):
    selected=selected_mode(owner);original=transport.bridge_birth
    if selected=='bad_bridge':transport.bridge_birth=json.loads((root/'sentinel.json').read_text())
    try:
        result=await register_bridge(db,owner,transport)
        if selected=='bridge_commit':db.info['cli_fail_commit']=True
        return result
    finally:transport.bridge_birth=original
records.register_bridge=bridging
activate=records.activate
async def activating(db,owner,result):
    value=await activate(db,owner,result)
    if selected_mode(owner)=='activate_commit':db.info['cli_fail_commit']=True
    return value
records.activate=activating
complete=records.complete
async def completing(owner,*args,**kwargs):
    token=receipt_fail.set(selected_mode(owner)=='receipt_failure')
    try:return await complete(owner,*args,**kwargs)
    finally:receipt_fail.reset(token)
records.complete=completing
heartbeat=records.heartbeat
async def beating(db,owner):
    if selected_mode(owner)=='heartbeat_failure':
        row=await db.get(NativeCliSession,owner.id)
        if row.phase=='active':raise OSError('isolated CLI heartbeat failure')
    return await heartbeat(db,owner)
records.heartbeat=beating
stop=owned_process.OwnedProcess._stop
async def stopping(self,*args,**kwargs):
    result=await stop(self,*args,**kwargs)
    if self.nonce in unverified:result['verified']=False;result['error']='OSError'
    return result
owned_process.OwnedProcess._stop=stopping
@app.post('/api/v1/fixture/mode/{value}')
async def choose(value:str,user=Depends(get_current_user)):
    global mode
    mode=value
    return {'mode':mode}
@app.get('/api/v1/fixture/internal/{identifier}')
async def internal(identifier:str,user=Depends(get_current_user)):
    session=sessions.SESSIONS.get(identifier)
    async with SessionLocal() as db:
        row=await db.get(NativeCliSession,identifier)
        return {'state':records.public(row),'stop':session.client.stop_result if session else None,
                'jobs':await db.scalar(select(func.count()).select_from(Job))}
@app.post('/api/v1/fixture/idle/{identifier}')
async def idle(identifier:str,user=Depends(get_current_user)):
    sessions.SESSIONS[identifier].last_activity=time.monotonic()-1000
    return {'ok':True}
@app.post('/api/v1/fixture/corrupt/{identifier}/{kind}')
async def corrupt(identifier:str,kind:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeCliSession,identifier)
        fields=('profile_json','process_json','owner_identity_json','initialize_json','stop_json')
        if kind=='restore':
            for key,value in saved.pop(identifier).items():setattr(row,key,value)
        else:
            saved[identifier]={key:getattr(row,key) for key in fields}
            if kind in {'profile','binding'}:
                value=json.loads(row.profile_json);value['program_sha256']='bad' if kind=='profile' else value['program_sha256']
                if kind=='binding':value['user_id']+=1000
                row.profile_json=json.dumps(value)
            elif kind in {'root','bridge'}:
                value=json.loads(row.process_json)
                if kind=='root':value['root']['launch_nonce']='0'*64
                else:value['bridge']['creation_time_100ns']='invalid'
                row.process_json=json.dumps(value)
            elif kind=='owner':row.owner_identity_json='{}'
            elif kind=='initialize':row.initialize_json='{"accessToken":"synthetic"}'
            elif kind=='stop':
                value=json.loads(row.stop_json);value['launch_nonce']='0'*64;row.stop_json=json.dumps(value)
        await db.commit()
        return records.public(row)
'''


def wait_for(operation, predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:value = operation()
        except OSError:
            time.sleep(.04)
            continue
        if predicate(value):
            return value
        time.sleep(.04)
    raise TimeoutError('CLI fixture did not reach expected state')


def run_database(url, folder):
    folder.mkdir(parents=True)
    if url is not None:
        alembic(url, folder, 'upgrade', 'head')
    checks = []
    api = IsolatedServer('codex-session-api', app='cli_controlled:app', database_url=url)
    (api.root / 'cli.py').write_text(WORKER, encoding='utf8')
    (api.root / 'cli_controlled.py').write_text(CONTROLLED, encoding='utf8')
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(240)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sentinel_birth = identity.observe(sentinel.pid)
    (api.root / 'sentinel.json').write_text(json.dumps(sentinel_birth), encoding='utf8')
    try:
        with api:
            primary = api.create_case('CLI journal')
            primary['workspace_id'] = api.call('GET', '/workspaces')[0]['id']
            primary_id = primary['id']
            def create(mode='normal', *, global_session=False):
                api.call('POST', '/fixture/mode/' + mode)
                if mode == 'detached':
                    (api.root / 'leaf.json').unlink(missing_ok=True)
                if global_session:
                    return api.call('POST', '/codex/sessions', {})
                case = api.call('POST', f"/workspaces/{primary['workspace_id']}/cases", {'name': 'CLI ' + mode + ' ' + uuid.uuid4().hex[:6]})
                conv = api.call('POST', '/conversations', {'case_id': case['id'], 'permission_mode': 'plan'})
                body = {'case_id': case['id'], 'conversation_id': conv['id']}
                return body, api.call('POST', '/codex/sessions', body)
            body, value = create()
            sid = value['id'];path = '/codex/sessions/' + sid
            assert value['phase']=='active' and value['process']['root'] and value['process']['bridge']
            assert 'profile' not in value and 'owner_token' not in json.dumps(value)
            account = api.call('GET',path+'/account');models = api.call('GET',path+'/models')
            assert 'synthetic' not in json.dumps([account,models])
            assert api.call('GET','/fixture/internal/'+sid)['jobs']==0
            checks.append('native_cli_lifetime_does_not_create_job_or_publish_secrets')
            assert api.call('GET',path+'/sandbox')=={'runtime':'windows','legacy_readiness':'notConfigured',
                'mxc_availability':'not_probed','setup_authority':'deployment_operator'}
            checks.append('readiness_projects_only_known_status_without_claiming_mxc')
            for invalid in ({'program':'arbitrary'}, {'auth_home':'arbitrary'}, {'env':{}}, {'user_id':1}):
                api.call('POST','/codex/sessions',invalid,expect=422)
            checks.append('http_cannot_supply_command_environment_or_private_home')
            with ThreadPoolExecutor(2) as pool:
                def start_thread():
                    try:return api.call('POST',path+'/thread',expect=200)
                    except AssertionError as error:
                        assert 'got 409' in str(error),str(error)
                        return None
                starts = [pool.submit(start_thread) for _ in range(2)]
                replies = [future.result() for future in starts]
                assert sum(reply is not None for reply in replies) == 1
                assert next(reply for reply in replies if reply is not None)['thread_id']
            api.call('POST',path+'/thread',expect=409)
            assert len((api.root/'thread-starts.log').read_text().splitlines())==1
            checks.append('durable_thread_admission_prevents_duplicate_start')
            api.call('POST',f"/cases/{body['case_id']}:copy",{'name':'blocked'},expect=409)
            api.call('DELETE',f"/cases/{body['case_id']}",expect=409)
            api.call('DELETE',f"/conversations/{body['conversation_id']}",expect=409)
            checks.append('active_cli_blocks_copy_trash_and_conversation_delete')
            token=api.token
            api.create_case('Foreign CLI user')
            api.call('GET',path,expect=404)
            api.call('GET',path+'/sandbox',expect=404)
            assert not api.call('GET','/codex/sessions')
            api.token=token
            checks.append('cli_history_is_owned_by_creator')
            closed=api.call('DELETE',path)
            assert closed['phase']=='closed' and closed['cleanup_verified']
            assert api.call('DELETE',path)['cleanup_verified']
            for kind in ('profile','binding','root','bridge','owner','initialize','stop'):
                bad=api.call('POST','/fixture/corrupt/'+sid+'/'+kind)
                assert bad['phase']=='evidence_invalid',kind
                api.call('DELETE',path,expect=409)
                api.call('POST','/fixture/corrupt/'+sid+'/restore')
            checks.append('seven_corrupt_journals_are_read_only_and_not_replayed')
            api.call('POST',f"/cases/{body['case_id']}:copy",{'name':'after close'})
            api.call('DELETE',f"/conversations/{body['conversation_id']}")
            api.call('GET',path,expect=404)
            checks.append('verified_closed_history_can_follow_explicit_conversation_delete')
            global_value=create(global_session=True)
            assert global_value['case_id'] is None
            api.call('DELETE','/codex/sessions/'+global_value['id'])
            checks.append('account_only_cli_has_explicit_null_case_scope')

            for mode in ('bad_birth','bad_nonce','foreign_birth','bad_bridge','root_commit','bridge_commit','expired_lease',
                         'permission_changed','source_changed','owner_changed','bad_init','activate_commit','admission_abort'):
                api.call('POST','/fixture/mode/'+mode)
                case=api.call('POST',f"/workspaces/{primary['workspace_id']}/cases",{'name':'CLI '+mode})
                conv=api.call('POST','/conversations',{'case_id':case['id'],'permission_mode':'plan'})
                api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id']},
                         expect=409 if mode in {'permission_changed','source_changed','expired_lease','owner_changed'} else 502)
                states=api.call('GET','/codex/sessions');state=next(row for row in states if row['case_id']==case['id'])
                if mode=='owner_changed':
                    assert state['phase']=='starting' and state['cleanup_verified'] is None
                else:
                    assert state['phase']=='failed' and state['cleanup_verified'],(mode,state)
                if mode not in {'bad_init','activate_commit'}:
                    assert not any(json.loads(path.read_text())['mode']==mode for path in api.root.glob('cli-*.json')),mode
                assert identity.probe(sentinel_birth)['state']=='match'
                checks.append(mode+'_fails_closed_without_foreign_signal')

            _body,value=create('account_error');path='/codex/sessions/'+value['id']
            api.call('GET',path+'/account',expect=502)
            assert api.call('GET',path)['phase']=='active'
            api.call('DELETE',path)
            checks.append('explicit_metadata_rpc_error_keeps_session_usable')
            _body,value=create('bad_sandbox');path='/codex/sessions/'+value['id']
            api.call('GET',path+'/sandbox',expect=502)
            assert api.call('GET',path)['cleanup_verified']
            checks.append('unknown_sandbox_readiness_fails_closed')
            for mode in ('eof','heartbeat_failure','unverified_stop','receipt_failure'):
                _body,value=create(mode);path='/codex/sessions/'+value['id']
                if mode=='eof':api.call('GET',path+'/account',expect=502)
                elif mode=='heartbeat_failure':pass
                else:api.call('DELETE',path,expect=502)
                if mode=='receipt_failure':
                    state=api.call('GET',path);assert state['phase']=='active' and state['cleanup_verified'] is None
                    internal=api.call('GET','/fixture/internal/'+value['id']);assert internal['stop']['verified']
                else:
                    state=wait_for(lambda:api.call('GET',path),lambda row:row['phase']=='failed')
                    assert state['cleanup_verified'] is (mode!='unverified_stop'),(mode,state)
                checks.append(mode+'_retains_actual_cleanup_semantics')

            _body,value=create('detached');path='/codex/sessions/'+value['id']
            wait_for(lambda:(api.root/'leaf.json').exists(),bool)
            leaf=json.loads((api.root/'leaf.json').read_text())
            result=api.call('POST','/runtime/processes:cancel',{'case_id':value['case_id']})
            assert result['ok'] and result['cancelled']['codex']==1,result
            assert api.call('GET',path)['cleanup_verified'] and identity.probe(leaf)['state']=='exited'
            checks.append('case_supervisor_waits_for_detached_cli_descendant')
            _body,value=create();path='/codex/sessions/'+value['id']
            api.call('POST','/fixture/idle/'+value['id'])
            state=wait_for(lambda:api.call('GET',path),lambda row:row['phase']=='closed')
            assert state['cleanup_verified']
            checks.append('idle_cli_retires_with_persisted_receipt')

            api.call('POST','/fixture/mode/init_wait')
            with ThreadPoolExecutor(1) as pool:
                starting=pool.submit(api.call,'POST','/codex/sessions',{})
                wait_for(lambda:(api.root/'init-held').exists(),bool)
                state=next(row for row in api.call('GET','/codex/sessions') if row['phase']=='starting' and row['case_id'] is None)
                assert state['process']['root'] and state['process']['bridge']
                assert api.call('DELETE','/codex/sessions/'+state['id'])['cleanup_verified']
                try:starting.result()
                except AssertionError as error:assert 'got 409' in str(error),str(error)
                else:raise AssertionError('Cancelled initialization unexpectedly succeeded')
            checks.append('close_during_initialize_joins_startup_and_tree')

            api.call('POST','/fixture/mode/response_hold')
            connection=socket.create_connection(('127.0.0.1',api.port),timeout=10)
            connection.sendall((f'POST /api/v1/codex/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {api.token}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}').encode('ascii'))
            wait_for(lambda:(api.root/'response-held.json').exists(),bool)
            identifier=json.loads((api.root/'response-held.json').read_text())['id']
            connection.shutdown(socket.SHUT_RDWR);connection.close()
            state=wait_for(lambda:api.call('GET','/codex/sessions/'+identifier),lambda row:row['phase']=='failed')
            assert state['cleanup_verified'] and state['error']=='StartupResponseLost'
            checks.append('disconnect_after_activation_joins_scope_without_start_task_deadlock')

            _body,value=create('thread_wait');path='/codex/sessions/'+value['id']
            payload=b''
            connection=socket.create_connection(('127.0.0.1',api.port),timeout=10)
            connection.sendall((f'POST /api/v1{path}/thread HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {api.token}\r\nContent-Length: 0\r\n\r\n').encode('ascii'))
            wait_for(lambda:(api.root/'thread-held').exists(),bool)
            started=time.monotonic()
            api.call('PUT',f"/cases/{value['case_id']}/file",{'path':'Ref/during-cli.txt','content':'case lock remains available'})
            assert time.monotonic()-started<3
            checks.append('native_thread_rpc_does_not_hold_case_write_lock')
            connection.shutdown(socket.SHUT_RDWR);connection.close()
            state=wait_for(lambda:api.call('GET',path),lambda row:row['phase']=='failed')
            assert state['cleanup_verified'] and state['thread_state']=='starting'
            api.call('POST',path+'/thread',expect=409)
            checks.append('real_http_disconnect_joins_thread_rpc_without_replay')

            _body,value=create('detached');path='/codex/sessions/'+value['id']
            wait_for(lambda:(api.root/'leaf.json').exists(),bool)
            root_birth=value['process']['root']['host'];leaf=json.loads((api.root/'leaf.json').read_text())
            api.proc.kill();api.proc.wait(timeout=10)
            wait_for(lambda:identity.probe(root_birth)['state'],lambda state:state=='exited')
            wait_for(lambda:identity.probe(leaf)['state'],lambda state:state=='exited')
            api.log.close();api.log=(api.root/'api-restart.log').open('w',encoding='utf8')
            api.proc=subprocess.Popen([sys.executable,'-m','uvicorn',api.app,'--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(api.port),'--no-access-log'],
                cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            wait_for(lambda:api.opener.open(api.base+'/api/health',timeout=2).status if api.proc.poll() is None else 0,lambda status:status==200)
            before=set(api.root.glob('cli-*.json'))
            history=api.call('GET',path);assert history['cleanup_verified'] is None
            api.call('DELETE',path,expect=409);api.call('POST',path+'/thread',expect=409)
            api.call('POST',f"/cases/{value['case_id']}:copy",{'name':'blocked after restart'},expect=409)
            assert set(api.root.glob('cli-*.json'))==before
            assert identity.probe(sentinel_birth)['state']=='match'
            checks.extend(['owner_death_retires_actual_cli_and_detached_leaf','restart_reads_unknown_history_without_process_or_thread_replay'])
            result={'passed':True,'count':len(checks),'checks':checks,'foreign_sentinel_untouched':True,'product_api':True,
                    'fixture_cli':True,'actual_codex_inference':False,'evidence':str(api.root)}
    finally:
        sentinel.terminate();sentinel.wait(timeout=10)
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='codex-session-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run_database,None,root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_codex_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_codex_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            results['postgresql']=await asyncio.to_thread(run_database,url.set(database=name).render_as_string(hide_password=False),root/'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (root/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'databases':list(results),'counts':{key:value['count'] for key,value in results.items()},'evidence':str(root)}))


if __name__=='__main__':asyncio.run(main())
