"""Real HTTP document admission outside case transactions; disposable dual DBs."""
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
from mpi_scope_smoke import inspect_native

SVG='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10"><rect width="20" height="10" fill="blue"/></svg>'
TEX='\\documentclass{article}\n\\begin{document}Admission PDF.\\end{document}\n'
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
from app.services.report_handler import ReportHandler
root=Path(__file__).parent
owners={};fail_commit=contextvars.ContextVar('admission_commit_failure',default=False)
def mode(case_id):
    marker=root/('mode-'+str(case_id));return marker.read_text() if marker.exists() else ''
start=execution.RequestExecutor.start
async def starting(self):
    async with SessionLocal() as db:row=await db.get(NativeRequest,self.owner.id);case_id=row.case_id
    self.test_case_id=case_id;owners[self.owner.id]=case_id
    (root/('request-'+self.owner.id+'.json')).write_text(json.dumps({'id':self.owner.id,'case_id':case_id}))
    return await start(self)
execution.RequestExecutor.start=starting
command=execution.RequestExecutor._command
def commands(self,purpose,parameters):
    argv,timeout,limit=command(self,purpose,parameters)
    setting=mode(self.test_case_id)
    if setting in {'hold','source','revision','same_key','disconnect','owner_death','timeout','bad_birth','register_failure','receipt_failure','latex_hold','latex_source','latex_disconnect','latex_owner_death'}:
        if self.kind=='report':
            from app.services import pandoc_execution
            argv=[argv[0],'-I','-S','-u',str(root/'held_request_pandoc.py'),str(root),str(self.test_case_id),
                self.owner.id,str(pandoc_execution.NATIVE),parameters['config'],parameters['sha256']]
        else:
            from app.services import latex_artifacts
            argv=['wsl.exe','-d',self.profile['--distro'],'--exec','python3','-I','-S','-u',native.wsl_path(root/'held_request_latex.py'),
                native.wsl_path(latex_artifacts.NATIVE),self.profile['--recipe'],native.wsl_path(root),str(self.test_case_id),self.owner.id]
        if setting=='timeout':timeout=1
    return argv,timeout,limit
execution.RequestExecutor._command=commands
register=records.register
async def registering(owner,nonce,identifier,birth,distribution):
    setting=mode(owners[owner.id]);token=fail_commit.set(setting=='register_failure')
    if setting=='bad_birth':
        birth=json.loads(json.dumps(birth));birth['host']['creation_time_100ns']=str(int(birth['host']['creation_time_100ns'])+1)
    try:return await register(owner,nonce,identifier,birth,distribution)
    finally:fail_commit.reset(token)
records.register=registering
finish=records.finish
async def finishing(owner,*args):
    token=fail_commit.set(mode(owners[owner.id])=='receipt_failure')
    try:return await finish(owner,*args)
    finally:fail_commit.reset(token)
records.finish=finishing
commit=AsyncSession.commit
async def committing(db):
    if fail_commit.get():raise OSError('isolated document admission commit failure')
    return await commit(db)
AsyncSession.commit=committing
complete=ReportHandler.complete_preparation
async def completed(self,prepared,case_id,user_id):
    result=await complete(self,prepared,case_id,user_id)
    if mode(case_id) in {'resource','shadow'}:
        (root/('prepared-'+str(case_id))).touch()
        while not (root/('release-final-'+str(case_id))).exists():await asyncio.sleep(.02)
    return result
ReportHandler.complete_preparation=completed

@app.get('/fixture/admission/{case_id}')
async def inspect(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    rows=await db.scalars(select(NativeRequest).where(NativeRequest.case_id==case_id).order_by(NativeRequest.created_at))
    return {'requests':[records.public(row) for row in rows],
        'jobs':await db.scalar(select(func.count()).select_from(Job).where(Job.case_id==case_id)),
        'prefix':case.s3_prefix}

@app.on_event('startup')
async def exit_watch():
    async def watch():
        while not (root/'exit-admission-owner').exists():await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(watch())
'''
PANDOC='''import json,runpy,sys,time
from pathlib import Path
root=Path(sys.argv[1]);case=sys.argv[2];identifier=sys.argv[3]
module=runpy.run_path(sys.argv[4]);execute=module['execute'];popen=execute.__globals__['subprocess'].Popen
class Input:
    def __init__(self,stream):self.stream=stream
    def write(self,data):return self.stream.write(data)
    def flush(self):return self.stream.flush()
    def close(self):
        while not (root/('release-tool-'+case)).exists():time.sleep(.02)
        return self.stream.close()
def starting(*args,**kwargs):
    process=popen(*args,**kwargs)
    (root/('tool-'+identifier+'.json')).write_text(json.dumps({'pid':process.pid}))
    process.stdin=Input(process.stdin)
    return process
execute.__globals__['subprocess'].Popen=starting
execute(Path(sys.argv[5]),sys.argv[6])
'''
LATEX='''import json,runpy,subprocess,sys,tempfile,time
from pathlib import Path
module=runpy.run_path(sys.argv[1]);root=Path(sys.argv[3]);case=sys.argv[4];identifier=sys.argv[5]
with tempfile.TemporaryDirectory(prefix='admission-native-',dir=root) as folder:
    source=Path(folder)/'held.tex'
    source.write_text(r"\\documentclass{article}\\begin{document}\\newcount\\spin\\loop\\advance\\spin by 1\\iftrue\\repeat\\end{document}")
    process=subprocess.Popen(['pdflatex','-interaction=nonstopmode','held.tex'],cwd=folder,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (root/('tool-'+identifier+'.json')).write_text(json.dumps({'native_pid':process.pid}))
    try:
        while not (root/('release-tool-'+case)).exists():time.sleep(.02)
    finally:
        process.kill();process.wait(timeout=5)
print(json.dumps(module['probe'](sys.argv[2])),flush=True)
'''


def poll(api,case_id,predicate,seconds=45):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=api.call('GET','/fixture/admission/'+str(case_id),api_prefix=False)
        if predicate(value):return value
        time.sleep(.03)
    raise TimeoutError(json.dumps(value))


def verify(database_url=None,*,latex=False):
    server=IsolatedServer('document-admission',app='admission_controlled:app',database_url=database_url)
    for name,body in [('admission_controlled.py',CONTROLLED),('held_request_pandoc.py',PANDOC),('held_request_latex.py',LATEX)]:
        (server.root/name).write_text(body,encoding='utf-8')
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-c','import sys;sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    modes=(('latex_hold','latex_source','latex_disconnect','latex_owner_death') if latex else
        ('hold','source','revision','resource','shadow','same_key','bad_birth','register_failure','receipt_failure','timeout','disconnect','owner_death'))
    checks=[];runs=[]
    try:
        with server as api,ThreadPoolExecutor(max_workers=4) as pool:
            for mode in modes:
                case=api.create_case('Admission '+mode);case_id=case['id']
                api.put('E-report/report.md','# Admission\n\n![Plot](plot.svg)\n')
                api.put('plot.svg',SVG)
                api.put('E-report/latex/main.tex',TEX)
                before=poll(api,case_id,lambda row:True)
                root=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/before['prefix']
                marker=api.root/('mode-'+str(case_id));marker.write_text(mode)
                key='admission-'+mode
                path=api.prefix+('/latex:build' if latex else '/report:build')
                body={'recipe':'pdflatex'} if latex else {}
                failures={'source','revision','resource','shadow','latex_source'}
                native_failures={'bad_birth','register_failure','receipt_failure','timeout'}
                status=409 if mode in failures|native_failures else 200
                sock=None
                if 'disconnect' in mode:
                    sock=socket.create_connection(('127.0.0.1',api.port),5)
                    data=json.dumps(body).encode()
                    sock.sendall(('POST /api/v1'+path+' HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer '+api.token+
                        '\r\nIdempotency-Key: '+key+'\r\nContent-Type: application/json\r\nContent-Length: '+str(len(data))+'\r\n\r\n').encode()+data)
                else:
                    future=pool.submit(api.call,'POST',path,body,expect=status,extra_headers={'Idempotency-Key':key})
                waiting=poll(api,case_id,lambda row:bool(row['requests']))
                if mode=='same_key':
                    second=pool.submit(api.call,'POST',path,body,extra_headers={'Idempotency-Key':key})
                    waiting=poll(api,case_id,lambda row:len(row['requests'])==2)
                members=[];births=[]
                if mode not in {'resource','shadow','bad_birth','register_failure'}:
                    waiting=poll(api,case_id,lambda row:all(any(c['phase']=='running' for c in r['children']['children'].values()) for r in row['requests']))
                    for request in waiting['requests']:
                        child=next(iter(request['children']['children'].values()));births.append(child['identity'])
                        tool=api.root/('tool-'+request['id']+'.json');deadline=time.monotonic()+10
                        while not tool.exists() and time.monotonic()<deadline:time.sleep(.02)
                        assert tool.exists(),mode
                        if latex:
                            native=inspect_native({'root':child['identity']['native']['process']})
                            real=[item for item in native if item['comm'] in {'pdflatex','pdftex'}]
                            assert real,native;members.extend(real)
                        else:members.append(identity.observe(json.loads(tool.read_text())['pid']))
                    assert waiting['jobs']==0
                    # This write must finish before releasing any native tool.
                    api.put('E-report/concurrent.bin',mode)
                if mode in {'resource','shadow'}:
                    deadline=time.monotonic()+30
                    while not (api.root/('prepared-'+str(case_id))).exists() and time.monotonic()<deadline:time.sleep(.02)
                    assert (api.root/('prepared-'+str(case_id))).exists()
                    api.put('plot.svg' if mode=='resource' else 'E-report/plot.svg',SVG.replace('blue','red'))
                    (api.root/('release-final-'+str(case_id))).touch()
                elif mode in {'source','latex_source'}:api.put('E-report/latex/main.tex' if latex else 'E-report/report.md',TEX+'% changed\n' if latex else '# Changed Markdown\n')
                elif mode=='revision':
                    dictionary=(root/'0-caseDict/caseDict').read_text(encoding='utf-8')
                    version=api.call('GET',api.prefix+'/caseDict')['dict_revision']
                    api.call('PUT',api.prefix+'/caseDict',{'content':dictionary+'\n// changed during native admission\n','expected_revision':version})
                if 'disconnect' in mode:sock.close()
                elif 'owner_death' in mode:
                    owner=waiting['requests'][0]['owner_identity'];(api.root/'exit-admission-owner').touch()
                    deadline=time.monotonic()+15
                    while identity.probe(owner)['state']!='exited' and time.monotonic()<deadline:time.sleep(.02)
                    assert identity.probe(owner)['state']=='exited'
                elif mode not in {'timeout','receipt_failure','bad_birth','register_failure'}:
                    (api.root/('release-tool-'+str(case_id))).touch()
                elif mode=='receipt_failure':(api.root/('release-tool-'+str(case_id))).touch()
                if 'owner_death' in mode:
                    for birth in births:
                        deadline=time.monotonic()+15
                        while identity.probe(birth['host'])['state']!='exited' and time.monotonic()<deadline:time.sleep(.02)
                        assert identity.probe(birth['host'])['state']=='exited'
                    death={'case_id':case_id,'token':api.token,'row':waiting,'members':members,'births':births}
                    # HTTP is expected to close without an application result.
                    try:future.result(timeout=10)
                    except (OSError,AssertionError):pass
                    checks.append(mode);runs.append({'mode':mode,'jobs':0,'births':births});break
                elif 'disconnect' in mode:
                    final=poll(api,case_id,lambda row:all(r['phase']=='cancelled' for r in row['requests']))
                    assert final['jobs']==0
                else:
                    response=future.result(timeout=90)
                    if status==409:
                        final=poll(api,case_id,lambda row:all(r['phase']!='active' for r in row['requests']))
                        assert final['jobs']==0,(mode,response)
                        assert response['detail']['code'] in {'input_stale','native_preparation_failed','revision_conflict'},response
                    else:
                        if mode=='same_key':assert second.result(timeout=90)['job_id']==response['job_id']
                        api.wait_job(response['job_id'])
                        final=poll(api,case_id,lambda row:row['jobs']==1)
                        original_requests=len(final['requests'])
                        (root/('E-report/latex/main.tex' if latex else 'E-report/report.md')).unlink()
                        assert api.call('POST',path,body,extra_headers={'Idempotency-Key':key})['job_id']==response['job_id']
                        replay=poll(api,case_id,lambda row:True)
                        assert len(replay['requests'])==original_requests and replay['jobs']==1
                for birth in births:
                    assert identity.probe(birth['host'])['state']=='exited'
                    if birth['native'] is not None:
                        from app.services.owned_process import probe_native
                        assert asyncio.run(probe_native(birth['native']['distribution'],birth['native']['process']))['state']=='exited'
                if not latex:
                    for member in members:assert identity.probe(member)['state']=='exited'
                assert sentinel.poll() is None
                checks.append(mode);runs.append({'mode':mode,'row':final,'members':members,'births':births})
                print('PASS document_admission_'+mode,flush=True)
        reader=IsolatedServer('admission-reader',app='admission_reader:app',database_url=database_url)
        overrides={key:server.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        if database_url is None:overrides['VIBEFLOW_DATABASE_URL']=server.env['VIBEFLOW_DATABASE_URL']
        code='''from datetime import timedelta
from app.services.durable_events import now
@app.post('/fixture/expire/{identifier}')
async def expire(identifier:str):
    async with SessionLocal() as db:
        await db.execute(update(NativeRequest).where(NativeRequest.id==identifier).values(lease_expires_at=now()-timedelta(seconds=1)))
        await db.commit()
    return {'ok':True}
'''
        (reader.root/'admission_reader.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+CONTROLLED+code,encoding='utf-8')
        with reader as api:
            api.token=death['token'];request=death['row']['requests'][0]
            api.call('POST','/fixture/expire/'+request['id'],api_prefix=False)
            persisted=poll(api,death['case_id'],lambda row:True)
            assert persisted['jobs']==0
            request_now=persisted['requests'][0]
            assert request_now['phase']=='recovery_required' and request_now['ended_at'] is None
            assert request_now['children']==request['children']
            for birth in death['births']:
                if birth['native'] is not None:
                    from app.services.owned_process import probe_native
                    assert asyncio.run(probe_native(birth['native']['distribution'],birth['native']['process']))['state']=='exited'
            if not latex:
                for member in death['members']:assert identity.probe(member)['state']=='exited'
    finally:sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root),'reader_evidence':str(reader.root),
        'production_endpoints':True,'concurrent_write_before_tool_release':True,'replay_before_deleted_source':True,
        'http_disconnect_waits_for_actual_exit':True,'owner_death_has_no_job_or_fabricated_receipt':True}
    (server.root/'admission-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='document-admission-evidence-',dir=SERVER.parents[1]/'tmp'))
    async def both(database_url=None):
        return {'report':await asyncio.to_thread(verify,database_url),'latex':await asyncio.to_thread(verify,database_url,latex=True)}
    results={'sqlite':await both()}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_admission_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_admission_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await both(temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('DOCUMENT_ADMISSION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
