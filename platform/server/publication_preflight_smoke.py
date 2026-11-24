"""Real native revalidation outside DB locks; commit fences on two APIs/databases."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from testsupport import IsolatedServer, SERVER
from geometry_publication_smoke import wait_file
from workflow_migration_smoke import alembic

CONTROLLED='''import contextvars,json,subprocess
from pathlib import Path
from fault_fixture import FileBarrier
from app.services import artifact_publication as pub
from app.services.report_handler import ReportHandler
from app.services.latex_handler import LatexHandler
from app.services import jobrunner
from sqlalchemy.ext.asyncio import AsyncSession
root=Path(__file__).parent
in_commit=contextvars.ContextVar('test_native_commit',default=False)
cancel_commit=contextvars.ContextVar('test_cancel_commit',default=False)
persist=jobrunner._persist_status
async def persist_status(job_id,status,*args,**kwargs):
    token=cancel_commit.set(status=='cancelled' and (root/'cancel-commit-failure').exists())
    try:return await persist(job_id,status,*args,**kwargs)
    finally:cancel_commit.reset(token)
jobrunner._persist_status=persist_status
commit=AsyncSession.commit
async def committing(db):
    if cancel_commit.get():raise OSError('injected cancellation commit failure')
    return await commit(db)
AsyncSession.commit=committing
cancel=jobrunner.JobProcess.cancel
def cancelling(self):
    if (root/'cancel-commit-failure').exists():(root/'signalled-before-commit').touch()
    return cancel(self)
jobrunner.JobProcess.cancel=cancelling
run=subprocess.run
def checked_run(*args,**kwargs):
    if in_commit.get():
        (root/'native-inside-commit').touch()
        raise RuntimeError('A compiler or parser ran inside the publication transaction')
    return run(*args,**kwargs)
subprocess.run=checked_run
final=pub.finalize
async def finalize(*args,**kwargs):
    token=in_commit.set(True)
    try:return await final(*args,**kwargs)
    finally:in_commit.reset(token)
pub.finalize=finalize
def decorate(original):
    def artifacts(self,run,descriptor,*args):
        records=original(self,run,descriptor,*args)
        case_id=descriptor['case_id']
        marker=root/('mode-'+str(case_id))
        mode=marker.read_text() if marker.exists() else ''
        if mode=='compiler_error':raise OSError('injected independent compiler failure')
        if mode and mode!='foreign_snapshot':
            (root/('context-'+str(case_id)+'.json')).write_text(json.dumps({'run':str(run),'records':records}))
            FileBarrier(root,'prepared-'+str(case_id),timeout=90).wait()
        return records
    return artifacts
ReportHandler.artifacts=decorate(ReportHandler.artifacts)
LatexHandler.artifacts=decorate(LatexHandler.artifacts)
prepare=pub.prepare_for_finish
async def preparing(*args,**kwargs):
    result=await prepare(*args,**kwargs)
    if result is not None:
        marker=root/('mode-'+str(result.case_id))
        if marker.exists() and marker.read_text()=='foreign_snapshot':
            result.attempt_id='00000000-0000-0000-0000-000000000000'
    return result
pub.prepare_for_finish=preparing
from app.main import app
'''


def start_peer(api):
    peer=IsolatedServer('publication-preflight-peer')
    peer.log=(peer.root/'api.log').open('w',encoding='utf-8')
    peer.proc=subprocess.Popen([sys.executable,'-m','uvicorn','controlled_preflight:app',
        '--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(peer.port),'--no-access-log'],
        cwd=api.root,env=api.env,stdout=peer.log,stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        if peer.proc.poll() is not None:
            raise AssertionError((peer.root/'api.log').read_text(encoding='utf-8'))
        try:
            if peer.call('GET','/api/health',api_prefix=False).get('ok'):return peer
        except (OSError,ValueError):pass
        time.sleep(.05)
    peer.__exit__(TimeoutError,None,None)
    raise TimeoutError('Owned peer did not become ready')


def completed(api,job_id,expected='succeeded'):
    deadline=time.monotonic()+200
    while time.monotonic()<deadline:
        row=api.call('GET',f'/jobs/{job_id}')
        if row['ended_at'] is not None and row['attempt']['phase']==expected and row['status']==expected:
            return row
        if row['status'] in {'failed','interrupted'} and row['ended_at'] is not None:
            raise AssertionError(row)
        time.sleep(.05)
    raise TimeoutError(json.dumps(row))


def verify(database_url=None):
    server=IsolatedServer('publication-preflight',app='controlled_preflight:app',database_url=database_url)
    (server.root/'controlled_preflight.py').write_text(CONTROLLED,encoding='utf-8')
    checks=[];observations=[]
    with server as api:
        peer=start_peer(api)
        try:
            scenarios=[('report',mode) for mode in ('cancel','cancel_local','cancel_commit_error','source','resource','winner','output','foreign_snapshot','compiler_error')]
            scenarios.append(('latex','cancel'))
            for kind,mode in scenarios:
                api.create_case(kind+' preflight '+mode)
                peer.token,peer.prefix=api.token,api.prefix
                root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                if kind=='report':
                    api.put('Ref/plot.svg','<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="blue"/></svg>')
                    source='E-report/report.md'
                    api.put(source,'# Independent report\n\n![figure](../Ref/plot.svg)\n')
                    body={};aliases=['E-report/build/report.html','E-report/build/report.docx']
                else:
                    source='E-report/latex/main.tex'
                    api.put(source,'\\documentclass{article}\n\\begin{document}Independent native PDF.\\end{document}\n')
                    body={'main':source,'recipe':'pdflatex'};aliases=['E-report/latex/build/main.pdf']
                def build(client=api):return client.call('POST',api.prefix+'/'+kind+':build',body)
                def current():return peer.call('GET',api.prefix+'/'+kind+'/results')['publication']['id']
                initial=build();completed(api,initial['job_id'])
                assert current()==initial['publication_id']
                for client in (api,peer):
                    assert not client.call('POST',f'/jobs/{initial["job_id"]}:cancel',{})['ok']
                assert current()==initial['publication_id']
                previous={path:hashlib.sha256((root/path).read_bytes()).hexdigest() for path in aliases}
                marker=api.root/f'mode-{api.case_id}';marker.write_text(mode)
                submitted=build()
                expected='failed';elapsed=None;winner=None
                if mode in {'foreign_snapshot','compiler_error'}:
                    row=completed(api,submitted['job_id'],'failed')
                    assert row['attempt']['reason_code']=='publication_failed',row
                    error=row['attempt']['outputs']['publication_error']
                    assert error['type']==('OSError' if mode=='compiler_error' else 'ValueError'),error
                else:
                    barrier=api.root/f'prepared-{api.case_id}'
                    wait_file(barrier.with_suffix('.ready'),timeout=200)
                    marker.write_text('')  # A newer generation must not inherit this barrier.
                    try:
                        row=api.call('GET',f'/jobs/{submitted["job_id"]}')
                        assert row['attempt']['phase']=='finalizing' and row['status']=='running',row
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            def action():
                                peer.put('Ref/independent-writer.txt','committed while native validation was in flight')
                                if mode in {'cancel','cancel_local'}:
                                    return (api if mode=='cancel_local' else peer).call('POST',f'/jobs/{submitted["job_id"]}:cancel',{})
                                if mode=='cancel_commit_error':
                                    failure=api.root/'cancel-commit-failure';failure.touch()
                                    try:
                                        return api.call('POST',f'/jobs/{submitted["job_id"]}:cancel',{},expect=500,raw=True)
                                    finally:failure.unlink()
                                if mode=='source':return peer.put(source,'# Edited during validation\n')
                                if mode=='resource':return peer.put('Ref/plot.svg','<svg xmlns="http://www.w3.org/2000/svg"/>')
                                if mode=='winner':return build(peer)
                            started=time.monotonic();future=pool.submit(action)
                            try:response=future.result(timeout=8)
                            except BaseException:
                                barrier.with_suffix('.release').touch()
                                raise
                            elapsed=time.monotonic()-started
                        if mode in {'cancel','cancel_local'}:
                            assert response['ok'];expected='cancelled'
                        elif mode=='cancel_commit_error':
                            assert not (api.root/'signalled-before-commit').exists()
                            unchanged=api.call('GET',f'/jobs/{submitted["job_id"]}')
                            assert unchanged['status']=='running' and unchanged['attempt']['phase']=='finalizing',unchanged
                            expected='succeeded';winner=submitted
                        elif mode=='winner':
                            winner=response
                            # The new generation cannot publish while this old
                            # validator remains an active case writer. Wait for
                            # its finalization boundary, then drain the old one;
                            # generation fencing must still supersede the old result.
                            deadline=time.monotonic()+200
                            while True:
                                newer=peer.call('GET',f'/jobs/{winner["job_id"]}')
                                if newer['attempt']['phase']=='finalizing':break
                                assert newer['status'] in {'queued','running'},newer
                                if time.monotonic()>=deadline:raise TimeoutError('New generation did not reach finalization')
                                time.sleep(.1)
                            barrier.with_suffix('.release').touch()
                            completed(peer,winner['job_id']);expected='succeeded'
                            assert current()==winner['publication_id']
                        elif mode=='output':
                            context=json.loads((api.root/f'context-{api.case_id}.json').read_text())
                            run=Path(context['run']).resolve();assert run.is_relative_to(api.root.resolve())
                            path=next(name for name in context['records'] if name.endswith('.html'))
                            with (run/path).open('ab') as output:output.write(b'changed after independent validation')
                    finally:
                        barrier.with_suffix('.release').touch()
                    row=completed(api,submitted['job_id'],expected)
                    if mode in {'source','resource'}:assert row['attempt']['reason_code']=='input_stale',row
                    if mode=='output':assert row['attempt']['reason_code']=='publication_failed',row
                    if mode=='winner':assert row['attempt']['outputs']['publication']['status']=='superseded',row
                assert current()==(winner or initial)['publication_id']
                if winner is None:
                    assert all(hashlib.sha256((root/path).read_bytes()).hexdigest()==digest for path,digest in previous.items())
                assert not (api.root/'native-inside-commit').exists()
                check=kind+'_'+mode;checks.append(check)
                observations.append({'check':check,'job_id':submitted['job_id'],'status':row['status'],
                    'attempt_phase':row['attempt']['phase'],'reason':row['attempt']['reason_code'],
                    'independent_api_write_seconds':elapsed,'active_publication':current()})
                (api.root/'preflight-progress.json').write_text(json.dumps(observations,indent=2)+'\n',encoding='utf-8')
                print('PASS '+check+'_preserves_publication_fences_without_native_commit_lock',flush=True)
        finally:
            peer.__exit__(None,None,None)
    result={'passed':True,'count':len(checks),'checks':checks,'observations':observations,'evidence':str(server.root)}
    (server.root/'preflight-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='publication-preflight-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_preflight_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_preflight_[0-9a-f]{32}',name)
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
    print('PUBLICATION_PREFLIGHT_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
