"""Real request-owned SyncTeX queries, frozen history and native failure cleanup."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
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

from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path
from mpi_scope_smoke import inspect_native

MAIN='\\documentclass{article}\n\\usepackage{hyperref}\n\\begin{document}\n\\section{Introduction}\nIntro text.\\newpage\n\\section{Results}\nUniqueResultText and conclusion.\n\\end{document}\n'
PDF='E-report/latex/build/main.pdf'
CONTROLLED='''import asyncio,contextvars,json,os,sys
from pathlib import Path
from sqlalchemy import select,func,update
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from app.main import app
from app.db import SessionLocal,get_db
from app.deps import get_current_user
from app.models import NativeRequest,Job
from app.routers.workspaces import get_owned_case
from app.services import native_request_execution as execution,native_request_records as records,native_execution as native
from app.services import synctex_execution
root=Path(__file__).parent
owners={};calls={};unverified=set();fail_commit=contextvars.ContextVar('synctex_commit_failure',default=False)
def mode(case_id):
    path=root/('mode-'+str(case_id));return path.read_text() if path.exists() else ''
start=execution.RequestExecutor.start
async def starting(self):
    result=await start(self)
    if self.kind=='synctex':
        async with SessionLocal() as db:row=await db.get(NativeRequest,self.owner.id);self.test_case_id=row.case_id
        owners[self.owner.id]=self.test_case_id
        (root/('channel-'+self.owner.id+'.json')).write_text(json.dumps({'address':self.address,'capability':self.local_capability}))
    return result
execution.RequestExecutor.start=starting
command=execution.RequestExecutor._command
def commands(self,purpose,parameters):
    argv,timeout,limit=command(self,purpose,parameters)
    if self.kind!='synctex':return argv,timeout,limit
    setting=mode(self.test_case_id);calls[self.owner.id]=parameters
    if setting in {'hold','timeout','disconnect','owner_death','legacy_source','legacy_pdf','late_head','capability','frozen_history'}:
        argv=['wsl.exe','-d',self.profile['distro'],'--exec','python3','-I','-S','-u',native.wsl_path(root/'held_synctex.py'),
            native.wsl_path(synctex_execution.NATIVE),native.wsl_path(Path(parameters['config'])),parameters['sha256'],
            native.wsl_path(root),self.owner.id]
        if setting=='timeout':timeout=3
    elif setting=='output_limit':
        code='import runpy,sys;from pathlib import Path;f=runpy.run_path(sys.argv[1])["execute"];f.__globals__["STDOUT_LIMIT"]=16;f(Path(sys.argv[2]),sys.argv[3])'
        argv=['wsl.exe','-d',self.profile['distro'],'--exec','python3','-I','-S','-u','-c',code,
            native.wsl_path(synctex_execution.NATIVE),native.wsl_path(Path(parameters['config'])),parameters['sha256']]
    elif setting=='editor_env':
        import shlex
        marker=native.wsl_path(root/'forbidden-editor')
        editor='python3 -c '+shlex.quote('from pathlib import Path;Path('+repr(marker)+').touch()')
        code='import os,runpy,sys;from pathlib import Path;os.environ["SYNCTEX_EDITOR"]=sys.argv[4];runpy.run_path(sys.argv[1])["execute"](Path(sys.argv[2]),sys.argv[3])'
        argv=['wsl.exe','-d',self.profile['distro'],'--exec','python3','-I','-S','-u','-c',code,
            native.wsl_path(synctex_execution.NATIVE),native.wsl_path(Path(parameters['config'])),parameters['sha256'],editor]
    return argv,timeout,limit
execution.RequestExecutor._command=commands
register=records.register
async def registering(owner,nonce,identifier,birth,distribution):
    setting=mode(owners.get(owner.id,-1));token=fail_commit.set(setting=='register_failure')
    altered=json.loads(json.dumps(birth))
    if setting=='bad_birth':altered['native']['process']['start_ticks']=str(int(altered['native']['process']['start_ticks'])+1)
    if setting=='bad_distro':altered['native']['distribution']='vibeflow-wrong-distribution'
    if setting=='unverified_stop':unverified.add(birth['launch_nonce'])
    try:result=await register(owner,nonce,identifier,altered,distribution)
    finally:fail_commit.reset(token)
    if setting=='config_changed':
        with Path(calls[owner.id]['config']).open('ab') as stream:stream.write(b' ')
    if setting=='pdf_changed':
        with (Path(calls[owner.id]['config']).parent/'snapshot.pdf').open('ab') as stream:stream.write(b' ')
    if setting=='extra_sidecar':
        import gzip
        folder=Path(calls[owner.id]['config']).parent
        (folder/'snapshot.synctex').write_bytes(gzip.decompress((folder/'snapshot.synctex.gz').read_bytes()))
    return result
records.register=registering
finish=records.finish
async def finishing(owner,*args):
    token=fail_commit.set(mode(owners.get(owner.id,-1))=='receipt_failure')
    try:return await finish(owner,*args)
    finally:fail_commit.reset(token)
records.finish=finishing
complete=records.complete
async def completing(owner,*args,**kwargs):
    token=fail_commit.set(mode(owners.get(owner.id,-1))=='result_commit_failure')
    try:return await complete(owner,*args,**kwargs)
    finally:fail_commit.reset(token)
records.complete=completing
commit=AsyncSession.commit
async def committing(db):
    if fail_commit.get():raise OSError('isolated SyncTeX persistence failure')
    return await commit(db)
AsyncSession.commit=committing
stop=native.OwnedProcess._stop
async def stopping(self,*args):
    result=await stop(self,*args)
    if self.nonce in unverified:
        result['verified']=False;result['error']='OSError';raise OSError('isolated unverified cleanup')
    return result
native.OwnedProcess._stop=stopping

@app.get('/fixture/synctex/{case_id}')
async def inspect(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    rows=await db.scalars(select(NativeRequest).where(NativeRequest.case_id==case_id,NativeRequest.kind=='synctex.query').order_by(NativeRequest.created_at))
    return {'requests':[records.public(row) for row in rows],
        'jobs':await db.scalar(select(func.count()).select_from(Job).where(Job.case_id==case_id)),'prefix':case.s3_prefix}

@app.post('/fixture/expire/{identifier}')
async def expire(identifier:str):
    from datetime import timedelta
    from app.services.durable_events import now
    async with SessionLocal() as db:
        await db.execute(update(NativeRequest).where(NativeRequest.id==identifier).values(lease_expires_at=now()-timedelta(seconds=1)))
        await db.commit()
    return {'ok':True}

@app.post('/fixture/head/{case_id}')
async def head(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    from app.models import ArtifactPublicationHead
    from app.services.latex_artifacts import channel
    await get_owned_case(case_id,user,db)
    db.add(ArtifactPublicationHead(case_id=case_id,kind=channel('E-report/latex/main.tex')))
    await db.commit()
    return {'ok':True}

@app.post('/fixture/share/{case_id}/{viewer_id}/{enabled}')
async def share(case_id:int,viewer_id:int,enabled:bool,user=Depends(get_current_user),db=Depends(get_db)):
    from app.models import Membership,Workspace
    from app.services.sharing import set_share
    case=await get_owned_case(case_id,user,db)
    workspace=await db.get(Workspace,case.workspace_id)
    membership=await db.scalar(select(Membership).where(Membership.user_id==user.id))
    viewer=await db.scalar(select(Membership).where(Membership.user_id==viewer_id,Membership.org_id==membership.org_id))
    if viewer is None:db.add(Membership(user_id=viewer_id,org_id=membership.org_id,role='viewer'))
    await set_share(db,workspace,membership.org_id,'viewer',enabled)
    await db.commit()
    return {'ok':True}

@app.on_event('startup')
async def watch():
    async def ending():
        while not (root/'exit-sync-owner').exists():await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(ending())
'''
HELD='''import json,os,runpy,shutil,sys,tempfile,threading,time
from pathlib import Path
module=runpy.run_path(sys.argv[1]);execute=module['execute'];config=Path(sys.argv[2]);root=Path(sys.argv[4]);identifier=sys.argv[5]
popen=execute.__globals__['subprocess'].Popen
folder=Path(tempfile.mkdtemp(prefix='vibeflow-synctex-test-'))
source=json.loads(config.read_text())['pdf'];target=folder/'snapshot.pdf';shutil.copyfile(source,target)
sidecar=Path(source).with_suffix('.synctex.gz').read_bytes();os.mkfifo(folder/'snapshot.synctex.gz')
def starting(argv,*args,**kwargs):
    prefix=argv[-1].rsplit(':',1)[0];argv=[*argv[:-1],prefix+':'+str(target)]
    process=popen(argv,*args,**kwargs)
    marker=root/('native-'+identifier+'.json');temporary=marker.with_suffix('.writing')
    temporary.write_text(json.dumps({'local_pid':process.pid,'folder':str(folder)}));temporary.replace(marker)
    def feed():
        while not (root/('release-'+identifier)).exists():time.sleep(.02)
        try:
            with (folder/'snapshot.synctex.gz').open('wb') as stream:stream.write(sidecar)
        except BrokenPipeError:pass
    threading.Thread(target=feed,daemon=True).start()
    return process
execute.__globals__['subprocess'].Popen=starting
try:execute(config,sys.argv[3])
finally:shutil.rmtree(folder)
'''


def poll(api,case_id,condition,seconds=35):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=api.call('GET','/fixture/synctex/'+str(case_id),api_prefix=False)
        if condition(value):return value
        time.sleep(.03)
    raise TimeoutError(json.dumps(value))


def cleanup_native(folders):
    if not folders:return
    script='import json,shutil,sys,re;from pathlib import Path\nfor name in json.loads(sys.stdin.read()):\n p=Path(name);assert p.parent==Path("/tmp") and re.fullmatch("vibeflow-synctex-test-[A-Za-z0-9_-]+",p.name)\n if p.exists():shutil.rmtree(p)\n'
    result=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',script],
        input=json.dumps(sorted(folders)).encode(),capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')


def member_at(api,case_id,identifier,marker):
    value=json.loads(marker.read_text())
    row=next(row for row in poll(api,case_id,lambda value:True)['requests'] if row['id']==identifier)
    child=next(iter(row['children']['children'].values()))
    # The helper's Popen PID is local to its private namespace. Observe the
    # actual process through the independent outer probe, as registration does.
    members=inspect_native({'root':child['identity']['native']['process']})
    tools=[item for item in members if item['comm']=='synctex']
    assert len(tools)==1,members
    value['identity']=tools[0]['identity']
    marker.with_suffix('.observed').write_text(json.dumps(value))
    return value


def verify(database_url=None):
    server=IsolatedServer('synctex-execution',app='controlled_synctex:app',database_url=database_url)
    (server.root/'controlled_synctex.py').write_text(CONTROLLED,encoding='utf-8')
    (server.root/'held_synctex.py').write_text(HELD,encoding='utf-8')
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-c','import sys;sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    native_sentinel=subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c','import sys;sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    checks=[];runs=[];readbacks=[];folders=set()
    try:
        with server as api,ThreadPoolExecutor(max_workers=2) as pool:
            api.create_case('Owned SyncTeX');primary_case=api.case_id;primary_token=api.token;primary_prefix=api.prefix
            api.put('E-report/latex/main.tex',MAIN)
            built=api.call('POST',api.prefix+'/latex:build',{'recipe':'pdflatex'})
            api.wait_job(built['job_id'])
            original=api.call('GET',api.prefix+'/latex/results?publication_id='+built['publication_id'])
            info=poll(api,api.case_id,lambda row:True);case_root=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/info['prefix']
            pdf_bytes=(case_root/PDF).read_bytes();sidecar_bytes=(case_root/Path(PDF).with_suffix('.synctex.gz')).read_bytes()
            frozen=case_root/'.vibeflow/publications'/built['publication_id']/'files'/PDF
            immutable_hash=hashlib.sha256(frozen.read_bytes()).hexdigest()
            modes=('normal','hold','frozen_history','capability','editor_env','bad_birth','bad_distro','register_failure','config_changed','pdf_changed','extra_sidecar',
                   'output_limit','unverified_stop','receipt_failure','result_commit_failure','timeout','disconnect')
            for mode in modes:
                (api.root/('mode-'+str(api.case_id))).write_text(mode)
                previous=poll(api,api.case_id,lambda row:True);count=len(previous['requests']);jobs=previous['jobs']
                body={'pdf':PDF,'page':2,'x':134,'y':157,'publication_id':built['publication_id']}
                expected=504 if mode=='timeout' else 502 if mode in {'bad_birth','bad_distro','register_failure','config_changed','pdf_changed','extra_sidecar','output_limit','unverified_stop','receipt_failure','result_commit_failure'} else 200
                if mode=='disconnect':
                    sock=socket.create_connection(('127.0.0.1',api.port),5);data=json.dumps(body).encode()
                    sock.sendall(('POST /api/v1'+api.prefix+'/synctex:query HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer '+api.token+
                        '\r\nContent-Type: application/json\r\nContent-Length: '+str(len(data))+'\r\n\r\n').encode()+data)
                else:future=pool.submit(api.call,'POST',api.prefix+'/synctex:query',body,expect=expected)
                current=poll(api,api.case_id,lambda row:len(row['requests'])>count);request=current['requests'][-1];member=None
                if mode in {'hold','frozen_history','capability','timeout','disconnect'}:
                    marker=api.root/('native-'+request['id']+'.json');deadline=time.monotonic()+15
                    while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
                    assert marker.exists(),mode
                    member=member_at(api,api.case_id,request['id'],marker);folders.add(member['folder'])
                    assert inspect_native({'probe':[member['identity']]})[0]['state']=='match'
                    api.put('E-report/query-concurrent.bin',mode)
                    assert poll(api,api.case_id,lambda row:True)['jobs']==jobs
                    if mode=='frozen_history':api.put('E-report/latex/main.tex','\n\n'+MAIN)
                    if mode=='capability':
                        channel=json.loads((api.root/('channel-'+request['id']+'.json')).read_text())
                        with socket.create_connection(('127.0.0.1',int(channel['address'].rsplit(':',1)[1])),5) as wire:
                            wire.sendall((json.dumps({'schema':'vibeflow.native-call.v1','capability':'0'*64,'purpose':'synctex_query','parameters':{}})+'\n').encode())
                            assert json.loads(wire.makefile('rb').readline())['type']=='error'
                    if mode=='disconnect':sock.close()
                    elif mode!='timeout':(api.root/('release-'+request['id'])).touch()
                if mode=='disconnect':
                    final=poll(api,api.case_id,lambda row:row['requests'][-1]['phase']=='cancelled')
                else:
                    response=future.result(timeout=40)
                    final=poll(api,api.case_id,lambda row:row['requests'][-1]['phase']!='active' or mode=='result_commit_failure')
                    if expected==200:
                        assert response['file']=='E-report/latex/main.tex' and response['line']==7,response
                        assert response['publication_id']==built['publication_id']
                        assert response['source_current_matches']==(mode!='frozen_history'),response
                row=final['requests'][-1]
                if mode=='frozen_history':api.put('E-report/latex/main.tex',MAIN)
                if mode=='result_commit_failure':
                    assert row['phase']=='active' and row['ended_at'] is None
                    api.call('POST','/fixture/expire/'+row['id'],api_prefix=False)
                    row=poll(api,api.case_id,lambda row:True)['requests'][-1]
                    assert row['phase']=='recovery_required'
                else:assert row['phase']==('cancelled' if mode=='disconnect' else 'succeeded' if expected==200 else 'failed'),row
                children=list(row['children']['children'].values());assert len(children)==1,row
                child=children[0]
                if mode=='receipt_failure':assert child['phase']=='running' and child['stop'] is None
                else:
                    assert child['stop'] is not None
                    assert child['stop']['verified']==(mode!='unverified_stop')
                if child['identity'] is not None:
                    assert identity.probe(child['identity']['host'])['state']=='exited'
                    assert asyncio.run(probe_native(child['identity']['native']['distribution'],child['identity']['native']['process']))['state']=='exited'
                if member is not None:assert inspect_native({'probe':[member['identity']]})[0]['state']=='exited'
                assert final['jobs']==jobs and hashlib.sha256(frozen.read_bytes()).hexdigest()==immutable_hash
                assert not (api.root/'forbidden-editor').exists()
                assert sentinel.poll() is None and native_sentinel.poll() is None
                checks.append(mode);runs.append({'mode':mode,'request':row,'member':member});readbacks.append((api.token,api.case_id,row))
                print('PASS owned_synctex_'+mode,flush=True)
            viewer=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'sync-viewer-123','name':'SyncTeX viewer'})
            api.call('POST',f'/fixture/share/{api.case_id}/{viewer["user"]["id"]}/true',api_prefix=False)
            (api.root/('mode-'+str(api.case_id))).write_text('normal')
            viewer_headers={'Authorization':'Bearer '+viewer['token']}
            response=api.call('POST',api.prefix+'/synctex:query',body,extra_headers=viewer_headers)
            assert response['line']==7
            api.call('PUT',api.prefix+'/file',{'path':'E-report/viewer.txt','content':'denied'},expect=404,extra_headers=viewer_headers)
            row=poll(api,api.case_id,lambda row:True)['requests'][-1]
            readbacks.append((api.token,api.case_id,row));checks.append('viewer_readonly');runs.append({'mode':'viewer_readonly','request':row})
            before=poll(api,api.case_id,lambda row:True);count=len(before['requests'])
            (api.root/('mode-'+str(api.case_id))).write_text('hold')
            future=pool.submit(api.call,'POST',api.prefix+'/synctex:query',body,expect=404,extra_headers=viewer_headers)
            current=poll(api,api.case_id,lambda row:len(row['requests'])>count);request=current['requests'][-1]
            marker=api.root/('native-'+request['id']+'.json');deadline=time.monotonic()+15
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
            assert marker.exists();member=member_at(api,api.case_id,request['id'],marker);folders.add(member['folder'])
            api.call('POST',f'/fixture/share/{api.case_id}/{viewer["user"]["id"]}/false',api_prefix=False)
            (api.root/('release-'+request['id'])).touch();future.result(timeout=30)
            row=poll(api,api.case_id,lambda row:True)['requests'][-1]
            assert row['phase']=='succeeded' and row['cleanup_verified'] and inspect_native({'probe':[member['identity']]})[0]['state']=='exited'
            readbacks.append((api.token,api.case_id,row));checks.append('permission_revoked');runs.append({'mode':'permission_revoked','request':row,'member':member})
            print('PASS owned_synctex_viewer_and_permission_recheck',flush=True)
            # A plain uploaded pair uses the legacy channel, with the same owned
            # query and strict read-only freshness checks.
            api.create_case('Legacy SyncTeX');legacy_case=api.case_id
            api.put('E-report/latex/main.tex',MAIN)
            info=poll(api,api.case_id,lambda row:True);legacy_root=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/info['prefix']
            (legacy_root/PDF).parent.mkdir(parents=True,exist_ok=True)
            (legacy_root/PDF).write_bytes(pdf_bytes);(legacy_root/Path(PDF).with_suffix('.synctex.gz')).write_bytes(b'not gzip')
            api.call('POST',api.prefix+'/synctex:query',{'pdf':PDF,'page':2,'x':134,'y':157},expect=422)
            assert not poll(api,api.case_id,lambda row:True)['requests']
            checks.append('legacy_invalid_gzip');runs.append({'mode':'legacy_invalid_gzip','request_created':False})
            for mode in ('legacy_normal','legacy_source','legacy_pdf','late_head'):
                (legacy_root/PDF).write_bytes(pdf_bytes);(legacy_root/Path(PDF).with_suffix('.synctex.gz')).write_bytes(sidecar_bytes)
                (legacy_root/'E-report/latex/main.tex').write_text(MAIN,encoding='utf-8')
                (api.root/('mode-'+str(api.case_id))).write_text(mode)
                before=poll(api,api.case_id,lambda row:True);count=len(before['requests'])
                future=pool.submit(api.call,'POST',api.prefix+'/synctex:query',{'pdf':PDF,'page':2,'x':134,'y':157},expect=200 if mode=='legacy_normal' else 409)
                current=poll(api,api.case_id,lambda row:len(row['requests'])>count);request=current['requests'][-1];member=None
                if mode!='legacy_normal':
                    marker=api.root/('native-'+request['id']+'.json');deadline=time.monotonic()+15
                    while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
                    assert marker.exists();member=member_at(api,api.case_id,request['id'],marker);folders.add(member['folder'])
                    if mode=='legacy_source':api.put('E-report/latex/main.tex','\n'+MAIN)
                    elif mode=='legacy_pdf':(legacy_root/PDF).write_bytes(pdf_bytes+b'changed')
                    else:api.call('POST','/fixture/head/'+str(api.case_id),api_prefix=False)
                    (api.root/('release-'+request['id'])).touch()
                response=future.result(timeout=30)
                if mode=='legacy_normal':assert response['line']==7 and 'publication_id' not in response,response
                else:assert response['detail']['code']=='input_stale',response
                row=poll(api,api.case_id,lambda row:True)['requests'][-1]
                assert row['phase']=='succeeded' and row['cleanup_verified'] and poll(api,api.case_id,lambda row:True)['jobs']==0
                if member:assert inspect_native({'probe':[member['identity']]})[0]['state']=='exited'
                checks.append(mode);runs.append({'mode':mode,'request':row,'member':member});readbacks.append((api.token,api.case_id,row))
                print('PASS owned_synctex_'+mode,flush=True)
            # Owner death preserves incomplete evidence; the reader must not
            # issue signals, replay the query or invent a stop receipt.
            api.case_id=primary_case;api.prefix=primary_prefix;api.token=primary_token
            (api.root/('mode-'+str(primary_case))).write_text('owner_death')
            before=poll(api,primary_case,lambda row:True);count=len(before['requests'])
            future=pool.submit(api.call,'POST',api.prefix+'/synctex:query',{'pdf':PDF,'page':2,'x':134,'y':157,'publication_id':built['publication_id']})
            current=poll(api,primary_case,lambda row:len(row['requests'])>count);dead=current['requests'][-1]
            marker=api.root/('native-'+dead['id']+'.json');deadline=time.monotonic()+15
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
            assert marker.exists();member=member_at(api,primary_case,dead['id'],marker);folders.add(member['folder'])
            dead=next(row for row in poll(api,primary_case,lambda value:True)['requests'] if row['id']==dead['id'])
            (api.root/'exit-sync-owner').touch()
            while identity.probe(dead['owner_identity'])['state']!='exited' and time.monotonic()<deadline:time.sleep(.02)
            assert identity.probe(dead['owner_identity'])['state']=='exited'
            while inspect_native({'probe':[member['identity']]})[0]['state']!='exited' and time.monotonic()<deadline:time.sleep(.02)
            assert inspect_native({'probe':[member['identity']]})[0]['state']=='exited'
            try:future.result(timeout=10)
            except OSError:pass
            checks.append('owner_death');runs.append({'mode':'owner_death','request':dead,'member':member})
        reader=IsolatedServer('synctex-reader',app='synctex_reader:app',database_url=database_url)
        overrides={key:server.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        if database_url is None:overrides['VIBEFLOW_DATABASE_URL']=server.env['VIBEFLOW_DATABASE_URL']
        (reader.root/'synctex_reader.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+CONTROLLED,encoding='utf-8')
        with reader as api:
            for token,case_id,row in readbacks:
                api.token=token;persisted=poll(api,case_id,lambda value:True)
                same=next(item for item in persisted['requests'] if item['id']==row['id'])
                assert same['children']==row['children'] and same['phase']==row['phase']
            api.token=primary_token;api.call('POST','/fixture/expire/'+dead['id'],api_prefix=False)
            persisted=poll(api,primary_case,lambda value:True)
            same=next(item for item in persisted['requests'] if item['id']==dead['id'])
            assert same['phase']=='recovery_required' and same['ended_at'] is None and same['children']==dead['children']
            child=next(iter(same['children']['children'].values()))
            assert child['stop'] is None and asyncio.run(probe_native(child['identity']['native']['distribution'],child['identity']['native']['process']))['state']=='exited'
            assert sentinel.poll() is None and native_sentinel.poll() is None
    finally:
        sentinel.communicate(b'x',timeout=10);native_sentinel.communicate(b'x',timeout=10);cleanup_native(folders)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root),'reader_evidence':str(reader.root),
        'immutable_input_sha256':immutable_hash,'no_jobs_created_by_queries':True,'readback_count':len(readbacks)+1,
        'actual_synctex_fifo_wait':True,'case_write_before_native_release':True,'owner_death_no_fabricated_receipt':True}
    (server.root/'synctex-execution-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='synctex-execution-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_synctex_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_synctex_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('SYNCTEX_EXECUTION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
