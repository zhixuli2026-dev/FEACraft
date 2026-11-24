"""Actual terminal lineage and two-API SQL audit checks, with optional browser hold.

Synthetic metadata and ACL faults are not LLM, CLI inference, or rendering evidence.
"""
import argparse, asyncio, hashlib, json, os, re, shutil, socket, sqlite3, subprocess, sys, tempfile, time, traceback, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from websockets.sync.client import connect
from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]
APP = '''from app.main import app
import os,uuid
from datetime import timedelta
from fastapi import Depends
from sqlalchemy import select
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import NativeSession,NativeCliSession,NativeVizSession,Case,Workspace,Membership
from app.services.durable_events import now
from app.services.process_identity_native import observe
@app.get('/api/v1/test/lineage-process')
async def birth(): return observe(os.getpid())
@app.post('/api/v1/test/lineage-exit')
async def exit(): os._exit(0)
@app.post('/api/v1/test/lineage-metadata')
async def metadata(body:dict,user=Depends(get_current_user)):
    rows=[]
    async with SessionLocal() as db:
        for kind,model in [('terminal',NativeSession),('cli',NativeCliSession),('viz',NativeVizSession)]:
            fields=dict(case_id=None if kind=='terminal' else body['case_id'],created_by=user.id,
                owner_id='MetadataFaultOnly',owner_token_sha256='0'*64,owner_identity_json='{}',
                launch_nonce=uuid.uuid4().hex,profile_json='{malformed-metadata',phase='failed',
                ended_at=now(),heartbeat_at=now(),lease_expires_at=now()-timedelta(seconds=60),error='MetadataFaultOnly')
            if kind=='terminal':fields.update(id=str(uuid.uuid4()),shell='cmd')
            elif kind=='cli':fields.update(id=str(uuid.uuid4()),runtime='windows')
            else:fields.update(last_activity_at=now())
            row=model(**fields);db.add(row);await db.flush();rows.append({'kind':kind,'id':str(row.id)})
        await db.commit()
    return rows
@app.post('/api/v1/test/lineage-access')
async def access(body:dict,user=Depends(get_current_user)):
    # Isolated ACL fault, not a product ownership-transfer endpoint.
    from app.services import sharing
    async with SessionLocal() as db:
        case=await db.get(Case,body['case_id']);workspace=await db.get(Workspace,case.workspace_id)
        if body['mode']=='restore':workspace.user_id=user.id
        else:
            workspace.user_id=body['other_user_id']
            owner=await db.scalar(select(Membership).where(Membership.user_id==body['other_user_id'],Membership.role=='admin').limit(1))
            membership=await db.scalar(select(Membership).where(Membership.user_id==user.id,Membership.org_id==owner.org_id))
            if membership is None:membership=Membership(user_id=user.id,org_id=owner.org_id,role='viewer');db.add(membership)
            membership.role='member' if body['mode']=='member' else 'viewer'
            await sharing.set_share(db,workspace,owner.org_id,'member' if body['mode']=='member' else 'viewer',body['mode']!='revoked')
        await db.commit()
    return {'mode':body['mode']}
@app.get('/api/v1/test/lineage-native-snapshots')
async def snapshots(user=Depends(get_current_user)):
    from app.services.native_session_review import fingerprint
    async with SessionLocal() as db:
        return {kind:{str(row.id):fingerprint(kind,row) for row in await db.scalars(select(model).where(model.created_by==user.id))}
            for kind,model in [('terminal',NativeSession),('cli',NativeCliSession),('viz',NativeVizSession)]}
@app.post('/api/v1/test/lineage-source-version')
async def source_version(body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeSession,body['id']);assert row.created_by==user.id
        row.version+=1;await db.commit()
    return {'metadata_fault_only':True}
@app.get('/api/v1/test/lineage-owned-terminal-records')
async def owned_terminal_records(user=Depends(get_current_user)):
    import json
    from app.routers.terminal import _sessions
    from app.services.terminal_records import public
    current=observe(os.getpid());result=[]
    async with SessionLocal() as db:
        for row in await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id)):
            if json.loads(row.owner_identity_json)!=current:continue
            value=public(row);local=_sessions.get(row.id)
            close_task=getattr(local,'_close_task',None)
            result.append({'id':row.id,'phase':value['phase'],'cleanup_verified':value['cleanup_verified'],
                'owner_available':local is not None,'memory_cleanup_verified':getattr(local,'cleanup_verified',None),
                'memory_persisted_terminal':getattr(local,'persisted_terminal',None),'memory_error':getattr(local,'error',None),
                'close_task_done':close_task.done() if close_task else None})
    return result
'''
PEER_PREFIX = "import os,json\nfrom pathlib import Path\nos.environ.update(json.loads((Path(__file__).parent/'shared-runtime.json').read_text(encoding='utf8')))\n"


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


async def old_metadata(url, *, seed=False):
    from sqlalchemy import select
    from workflow_migration_smoke import engine_for, legacy
    from app.models import NativeSession, NativeCliSession, NativeVizSession, NativeSessionReview
    from app.services.durable_events import json_text, now
    if seed: await legacy(url, seed=True)
    engine = engine_for(url)
    try:
        async with engine.begin() as connection:
            if seed:
                await connection.execute(NativeSessionReview.__table__.insert().values(id=str(uuid.uuid4()),
                    target_kind='terminal',target_id=str(uuid.uuid4()),case_id=None,actor_user_id=101,
                    snapshot_sha256='0'*64,payload_json=json_text({'migration_metadata_only':True}),created_at=now()))
            return {model.__tablename__: hashlib.sha256(repr((await connection.execute(select(model.__table__).order_by(model.id))).fetchall()).encode()).hexdigest()
                    for model in (NativeSession,NativeCliSession,NativeVizSession,NativeSessionReview)}
    finally: await engine.dispose()


def verify(args, database_url=None):
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    from workflow_migration_smoke import alembic
    reviewed = json.loads(args.review.read_text(encoding='utf8'))
    assert reviewed['fixture_sha256'] == hashlib.sha256(APP.encode()).hexdigest()
    assert reviewed['peer_prefix_sha256'] == hashlib.sha256(PEER_PREFIX.encode()).hexdigest()
    for name, digest in reviewed['source_hashes'].items(): assert sha(ROOT / name) == digest
    api = IsolatedServer('native-session-lineage', app='lineage_fixture:app', database_url=database_url)
    (api.root / 'lineage_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'backend':'postgresql' if database_url else 'sqlite','API_exit_witnesses':[],
        'new_real_model_calls':0,'new_mock_llm_calls':0,'synthetic_metadata_is_native_execution_evidence':False,
        'developer_database_modified':False,'source_review_sha256':sha(args.review)}
    servers=[]; preview=preview_log=preview_handle=None; terminals=[]
    def save(): (api.root/'lineage-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    def enter(server):
        server.__enter__(); birth=server.call('GET','/test/lineage-process'); handle=ProcessHandle.query(birth)
        servers.append((server,birth,handle)); return server
    def stop(server,birth,handle):
        if any(row['birth']==birth for row in report['API_exit_witnesses']): return
        try: server.call('POST','/test/lineage-exit')
        except (OSError,httpx.TransportError): pass
        handle.wait_exit(10);report['API_exit_witnesses'].append({'birth':birth,'state':'exited','reason':'retained_creation_handle'})
        server.__exit__(RuntimeError if 'failure' in report else None,None,None);save()
    def terminal(case_id):
        with connect(api.base.replace('http:','ws:')+'/ws/terminal?case='+str(case_id)+'&shell=cmd&token='+api.token,proxy=None) as ws:
            while True:
                frame=ws.recv(timeout=30)
                if not isinstance(frame,str):continue
                value=json.loads(frame)
                assert value.get('type')!='error','Actual terminal startup refused'
                if value.get('type')=='ready':identifier=value['session'];break
        terminals.append(identifier)
        api.call('DELETE','/terminal/sessions/'+identifier)
        state=api.call('GET','/terminal/sessions/'+identifier);assert state['cleanup_verified'] is True
        return identifier
    def post_same(server,path,body):
        end=time.monotonic()+30
        while True:
            value=server.call('POST',path,body,expect=(200,409));detail=value.get('detail')
            if isinstance(detail,dict) and detail.get('code')=='storage_quota_busy':
                assert time.monotonic()<end,'Actual pre-admission writer busy did not drain'
                time.sleep(.05);continue
            assert detail is None,'Actual same-payload write rejected'
            return value
    print('NATIVE_SESSION_LINEAGE '+str(api.root),flush=True)
    try:
        if database_url is None:
            source=ROOT/'tmp/native-session-review-tepu2w8d'; prior=json.loads((source/'review-result.json').read_text(encoding='utf8'))
            assert prior['passed'] and len(prior['API_exit_witnesses'])==1 and prior['API_exit_witnesses'][0]['state']=='exited'
            report['source_database_sha256']=sha(source/'test.sqlite')
            with sqlite3.connect('file:'+(source/'test.sqlite').as_posix()+'?mode=ro',uri=True) as old:
                with sqlite3.connect(api.root/'test.sqlite') as copy:old.backup(copy)
            shutil.copytree(source/'files',api.root/'files')
            url='sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix()
            before=asyncio.run(old_metadata(url));email=json.loads((ROOT/'tmp/job-recovery-UI-7lxkzlsd/browser-setup.json').read_text(encoding='utf8'))['email']
        else:
            url=database_url;alembic(url,api.root,'upgrade','b19d76e032a8')
            before=asyncio.run(old_metadata(url,seed=True));email=None
        for operation,target in [('upgrade','head'),('downgrade','b19d76e032a8'),('upgrade','head')]:alembic(url,api.root,operation,target)
        assert asyncio.run(old_metadata(url))==before
        report['incremental_upgrade_downgrade_upgrade_preserved_native_rows_and_review_metadata']=True;save()
        enter(api)
        if database_url is None:
            api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token'];api.case_id=1;api.prefix='/cases/1'
        else:
            api.create_case('Actual native session lineage');email=api.call('GET','/auth/me')['email']
        user_id=api.call('GET','/auth/me')['id'];report['case_id']=api.case_id
        old_a=terminal(api.case_id);old_b=terminal(api.case_id);child=terminal(api.case_id)
        other_case=api.call('POST','/workspaces/'+str(api.call('GET','/workspaces')[0]['id'])+'/cases',{'name':'Other lineage scope'})
        other_child=terminal(other_case['id']);report['actual_terminal_ids']=list(terminals)
        faults=api.call('POST','/test/lineage-metadata',{'case_id':api.case_id})
        peer=IsolatedServer('native-lineage-peer',app='peer_lineage_fixture:app',database_url=database_url)
        (peer.root/'shared-runtime.json').write_text(json.dumps({key:value for key,value in api.env.items() if key.startswith('VIBEFLOW_')}),encoding='utf8')
        (peer.root/'peer_lineage_fixture.py').write_text(PEER_PREFIX+APP,encoding='utf8');enter(peer)
        peer.token=api.token;peer.case_id=api.case_id;peer.prefix=api.prefix
        report['peer_root']=str(peer.root)
        frozen_sessions=api.call('GET','/test/lineage-native-snapshots')
        review_fault=next(row for row in faults if row['kind']=='cli')
        review_path='/native-sessions/cli/'+review_fault['id'];review_plan=api.call('GET',review_path)
        review_body={'action':'retain_for_review','snapshot_sha256':review_plan['snapshot_sha256'],'note':'Two actual API processes, same review'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            values=[f.result() for f in [pool.submit(post_same,server,review_path+':retain-for-review',review_body) for server in (api,peer)]]
        assert values[0]==values[1];report['two_API_same_manual_receipt']=values[0]['id']
        prefix='/native-sessions/terminal/'+old_a
        plan=api.call('GET',prefix+'/successors/'+child)
        body={'action':'link_existing_successor','successor_id':child,'source_snapshot_sha256':plan['source_snapshot_sha256'],
            'successor_identity_sha256':plan['successor_identity_sha256'],'note':'Two actual API processes, same existing-session link'}
        bad='0' if body['source_snapshot_sha256'][0]!='0' else '1'
        api.call('POST',prefix+':link-successor',{**body,'source_snapshot_sha256':bad+body['source_snapshot_sha256'][1:]},expect=409)
        with ThreadPoolExecutor(max_workers=2) as pool:
            values=[f.result() for f in [pool.submit(post_same,server,prefix+':link-successor',body) for server in (api,peer)]]
        assert values[0]==values[1] and all(values[0][key] is False for key in ('execution_authorized','stop_evidence_created','context_inherited'))
        link=values[0];report['two_API_same_link_receipt']=link
        assert peer.call('POST',prefix+':link-successor',body)==link
        api.call('POST',prefix+':link-successor',{**body,'note':'different confirmation'},expect=409)
        alternative=api.call('GET','/native-sessions/terminal/'+old_b)
        api.call('POST','/native-sessions/terminal/'+old_b+':link-successor',{**body,'source_snapshot_sha256':alternative['snapshot_sha256']},expect=409)
        for path in [prefix+'/successors/'+old_a,'/native-sessions/terminal/'+child+'/successors/'+old_a,prefix+'/successors/'+other_child]:api.call('GET',path,expect=409)
        for end in (old_a,child):assert any(row['id']==link['id'] for row in api.call('GET','/native-sessions/terminal/'+end)['links'])
        report['same_child_other_source_self_reverse_cross_case_rejected']=True
        assert api.call('GET','/test/lineage-native-snapshots')==frozen_sessions
        report['review_and_lineage_preserve_all_original_session_rows']=True
        api.call('POST','/test/lineage-source-version',{'id':old_a})
        assert api.call('GET',prefix)['snapshot_sha256']!=body['source_snapshot_sha256']
        assert peer.call('POST',prefix+':link-successor',body)==link
        report['original_committed_link_replayed_after_source_version_changed']=True
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other lineage reader'})
        original_token=api.token;api.token=other['token'];other_user=api.call('GET','/auth/me')['id']
        api.call('GET',prefix,expect=404);api.call('POST',prefix+':link-successor',body,expect=404);api.token=original_token
        access_body={'case_id':api.case_id,'other_user_id':other_user}
        try:
            api.call('POST','/test/lineage-access',{**access_body,'mode':'viewer'})
            assert api.call('GET',prefix)['id']==old_a
            api.call('POST',prefix+':link-successor',body,expect=404)
            api.call('POST',review_path+':retain-for-review',review_body,expect=404)
            api.call('POST','/test/lineage-access',{**access_body,'mode':'member'})
            assert api.call('POST',prefix+':link-successor',body)==link
            api.call('POST','/test/lineage-access',{**access_body,'mode':'revoked'})
            api.call('GET',prefix,expect=404);api.call('POST',prefix+':link-successor',body,expect=404)
        finally:api.call('POST','/test/lineage-access',{**access_body,'mode':'restore'})
        report['foreign_viewer_member_revoked_authorization_checked_before_replay']=True
        report['metadata_faults']=faults;report['functional_passed']=True;save()
        if args.browser_hold:
            assert database_url is None,'Browser hold uses the copied SQLite history'
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            assert port not in {8000,api.port,peer.port}
            web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
            preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview','--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,
                env=dict(os.environ,VITE_API_TARGET=api.base),stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            preview_birth=identity.observe(preview.pid);preview_handle=ProcessHandle.query(preview_birth);report['preview_birth']=preview_birth
            web_url='http://127.0.0.1:'+str(port);until=time.monotonic()+30
            with httpx.Client(trust_env=False,timeout=1) as client:
                while True:
                    assert preview.poll() is None
                    try:
                        if client.get(web_url).status_code==200:break
                    except httpx.HTTPError:pass
                    assert time.monotonic()<until;time.sleep(.1)
            setup={'root':str(api.root),'web':web_url,'email':email,'case_id':api.case_id,'source_id':old_a,'new_successor_id':old_b,'existing_successor_id':child}
            (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2),encoding='utf8');save()
            print('NATIVE_LINEAGE_UI_READY '+str(api.root)+' '+web_url,flush=True)
            done=api.root/'browser-finished.json';until=time.monotonic()+900
            while not done.exists():assert preview.poll() is None and time.monotonic()<until,'Browser hold stopped or timed out';time.sleep(.2)
            report['browser']=json.loads(done.read_text(encoding='utf8'));assert report['browser']['passed']
        report['functional_passed']=True
    except BaseException as error:
        report['functional_passed']=False
        report['failure']={'type':type(error).__name__,'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        for server,birth,handle in reversed(servers):
            try:
                if server is api:
                    report['owned_terminal_records_before_exit']=api.call('GET','/test/lineage-owned-terminal-records')
                    for row in report['owned_terminal_records_before_exit']:
                        if row['owner_available'] and row['cleanup_verified'] is not True:
                            api.call('DELETE','/terminal/sessions/'+row['id'])
                    report['owned_terminal_records_after_close']=api.call('GET','/test/lineage-owned-terminal-records')
                    assert all(row['cleanup_verified'] is True for row in report['owned_terminal_records_after_close']), 'An owned terminal lacks persisted stop proof'
                stop(server,birth,handle)
            except BaseException as error:
                report.setdefault('cleanup_failures',[]).append(type(error).__name__)
                # A missing terminal proof makes the case fail, but never
                # leaves this exact API owner live or invents session cleanup.
                try:stop(server,birth,handle)
                except BaseException as exit_error:report.setdefault('cleanup_failures',[]).append(type(exit_error).__name__)
            finally:handle.close()
        if preview is not None:
            if preview.poll() is None:preview.terminate()
            preview.wait(timeout=10)
        if preview_handle is not None:preview_handle.wait_exit(10);report['preview_exited']=True;preview_handle.close()
        if preview_log is not None:preview_log.close()
        report['passed']=bool(report.get('functional_passed') and len(report['API_exit_witnesses'])==2 and not report.get('cleanup_failures') and (not args.browser_hold or report.get('preview_exited')));save()
    print('NATIVE_SESSION_LINEAGE_RESULT '+str(api.root/'lineage-result.json'),flush=True)
    return report['passed'],api.root


async def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--postgres',action='store_true');parser.add_argument('--browser-hold',action='store_true');args=parser.parse_args()
    if args.postgres and args.browser_hold:parser.error('Use SQLite for browser hold')
    if not args.postgres:return 0 if (await asyncio.to_thread(verify,args))[0] else 1
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername=='postgresql+asyncpg' and selected.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='native-lineage-postgres-',dir=ROOT/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False};created=False
    admin=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True;url=selected.set(database=name).render_as_string(hide_password=False)
        passed,api_root=await asyncio.to_thread(verify,args,url);report.update(passed=passed,api_root=str(api_root))
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
                report['temporary_database_absent']=await admin.fetchval('SELECT datname FROM pg_database WHERE datname=$1',name) is None
        finally:await admin.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return 0 if report['passed'] and report.get('temporary_database_absent') else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
