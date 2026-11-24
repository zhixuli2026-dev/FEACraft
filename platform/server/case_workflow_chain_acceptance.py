"""Actual CasePanel conduction/report chain, integrity fault and target recomputation.

Every execution and artifact is native. Only a clearly recorded physical file
integrity fault is injected and then restored byte-for-byte; no inference runs.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import traceback
from urllib.parse import quote
import uuid

from testsupport import SERVER,IsolatedServer
from case_workflow_thermal_fixture import RUN,GEOMETRY,case_dictionary,thermal_files

ROOT=SERVER.parents[1]
CORE=['geometry','mesh','solve','postprocess','report']
APP='''import hashlib,json,os
from pathlib import Path
from fastapi import Depends
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import artifact_publication as publication,attempt_workspace
from app.services.process_identity_native import observe

@app.get('/api/v1/test/chain-birth')
async def birth():return observe(os.getpid())

def exact_write(path,data):
    with path.open('wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())

@app.post('/api/v1/test/chain-integrity/{case_id}')
async def integrity(case_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        _,row=await publication.current(db,case.id,'geometry');assert row is not None
        folder,manifest=publication.read_manifest(case.s3_prefix,row)
        name=next(name for name in sorted(manifest['files']) if name.endswith('.stl'))
        original=publication.read_file(case.s3_prefix,row,name)
        target=attempt_workspace.checked_path(folder/'files',name)
        assert target.resolve().is_relative_to(Path.cwd())
        meta=Path.cwd()/'fault-original.json';backup=Path.cwd()/'fault-original.bin'
        assert not meta.exists() and not backup.exists()
        stat=target.stat();bad=original+b'\\nEXPLICIT_UI_INTEGRITY_FAULT\\n'
        record={'case_id':case.id,'publication_id':row.id,'path':name,'absolute':str(target),
            'original_sha256':hashlib.sha256(original).hexdigest(),'bad_sha256':hashlib.sha256(bad).hexdigest(),
            'atime_ns':stat.st_atime_ns,'mtime_ns':stat.st_mtime_ns,'restored':False}
        backup.write_bytes(original);meta.write_text(json.dumps(record),encoding='utf8');exact_write(target,bad)
        return {k:v for k,v in record.items() if k!='absolute'}

@app.post('/api/v1/test/chain-restore/{case_id}')
async def restore(case_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        meta=Path.cwd()/'fault-original.json';record=json.loads(meta.read_text(encoding='utf8'))
        assert record['case_id']==case.id
        target=Path(record['absolute']);assert target.resolve().is_relative_to(Path.cwd())
        original=(Path.cwd()/'fault-original.bin').read_bytes()
        assert hashlib.sha256(original).hexdigest()==record['original_sha256']
        expected=record['original_sha256'] if record['restored'] else record['bad_sha256']
        assert hashlib.sha256(target.read_bytes()).hexdigest()==expected
        exact_write(target,original);os.utime(target,ns=(record['atime_ns'],record['mtime_ns']))
        _,row=await publication.current(db,case.id,'geometry');assert row.id==record['publication_id']
        assert publication.read_file(case.s3_prefix,row,record['path'])==original
        record['restored']=True;meta.write_text(json.dumps(record),encoding='utf8')
        return {'restored':True,'original_sha256':record['original_sha256'],'publication_id':row.id}
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def history(path,limits=None):
    names=['workflow_runs','workflow_steps','jobs','job_attempts']
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
        if limits is None:limits={name:db.execute('SELECT coalesce(max(rowid),0) FROM '+name).fetchone()[0] for name in names}
        return limits,{name:hashlib.sha256(repr(db.execute('SELECT * FROM '+name+' WHERE rowid<=? ORDER BY rowid',(limits[name],)).fetchall()).encode()).hexdigest() for name in names}


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services.process_identity_native import observe
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    api=IsolatedServer('case-workflow-chain',app='case_chain_fixture:app')
    (api.root/'case_chain_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'actual_native_jobs':[],
        'review_sha256':sha(args.review),'backend':'FS+SQLite','checks':[]}
    entered=False;api_handle=preview=preview_handle=preview_log=None;case_id=None
    def save(): (api.root/'chain-progress.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    def discover():
        if case_id is None:return
        for job in api.call('GET',api.prefix+'/jobs'):
            if job['id'] not in report['actual_native_jobs']:report['actual_native_jobs'].append(job['id'])
    def stopped(identifier,expected=None):
        until=time.monotonic()+300
        while time.monotonic()<until:
            job=api.call('GET','/jobs/'+str(identifier));attempt=job.get('attempt') or {}
            if job['status'] in {'succeeded','failed','cancelled','interrupted'} and attempt.get('phase')==job['status'] and (attempt.get('process_stop') or {}).get('verified') is True:
                report.setdefault('native_stops',{})[str(identifier)]={'status':job['status'],'stop':attempt['process_stop']}
                if expected:assert job['status']==expected,(identifier,job['status'],job.get('completion_error'))
                return job
            if job.get('completion_error'):raise RuntimeError('Actual native finalization error: '+job['completion_error'])
            time.sleep(.2)
        raise TimeoutError('Actual native stop not verified: '+str(identifier))
    def wait_run(identifier,status,seconds=300):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            value=api.call('GET','/workflow-runs/'+identifier);discover();save()
            if value['status']==status:return value
            if value['status'] in {'failed','cancelled','interrupted'} and value['status']!=status:
                (api.root/'failed-run.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
                raise RuntimeError('Actual workflow ended '+value['status'])
            time.sleep(.2)
        raise TimeoutError('Workflow did not reach '+status)
    def wait_browser(name,seconds=1800):
        until=time.monotonic()+seconds
        while not (api.root/name).exists():
            assert api.proc.poll() is None and preview.poll() is None and time.monotonic()<until,name
            discover();save();time.sleep(.3)
        value=json.loads((api.root/name).read_text(encoding='utf8'));assert value['passed'],value
        return value
    def numeric(label,nx):
        post=api.call('GET',api.prefix+'/postprocess/results')
        assert post['metrics']['readiness']['report_ready'] and post['metrics']['readiness']['can_claim_verification']
        metric=next(m for m in post['metrics']['metrics'] if m['name']=='mean.T@1');assert math.isclose(metric['value'],350,abs_tol=1e-6)
        data=api.call('GET',api.prefix+'/raw?path='+quote(RUN+'/1/T',safe=''),raw=True)
        match=re.search(rb'internalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;',data,re.S);assert match
        values=[float(v) for v in match[2].split()];assert int(match[1])==len(values)==nx*4*4
        assert all(math.isfinite(v) for v in values) and max(values)-min(values)>80 and abs(sum(values)/len(values)-350)<1e-6
        result={'cells':len(values),'T_min':min(values),'T_max':max(values),'T_mean':sum(values)/len(values),
            'metric_mean':metric['value'],'field_sha256':hashlib.sha256(data).hexdigest()}
        (api.root/('numeric-'+label+'.json')).write_text(json.dumps(result,indent=2),encoding='utf8');return result
    def report_bytes(identifier):
        result={}
        for suffix in ['html','docx']:
            data=api.call('GET',api.prefix+'/report/artifact?publication_id='+identifier+'&path='+quote('E-report/build/report.'+suffix,safe=''),raw=True)
            assert (b'mean.T@1' in data if suffix=='html' else data.startswith(b'PK'))
            result[suffix]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        return result
    try:
        api.__enter__();entered=True;report['API_birth']=api.call('GET','/test/chain-birth');api_handle=ProcessHandle.query(report['API_birth'])
        case=api.create_case('CasePanel complete conduction chain');case_id=case['id'];user=api.call('GET','/auth/me')
        info=api.call('GET',api.prefix+'/caseDict')
        api.call('PUT',api.prefix+'/caseDict',{'content':case_dictionary(info['content']),'expected_revision':info['dict_revision']})
        for name,content in thermal_files().items():api.put(name,content)
        seed=api.put('A-geo/geometry.py',GEOMETRY);discover()
        for identifier in list(report['actual_native_jobs']):stopped(identifier,'succeeded')
        info=api.call('GET',api.prefix+'/caseDict')
        original=api.call('POST',api.prefix+'/workflow-runs',{'target':'report','run_dir':RUN,'expected_revision':info['dict_revision'],
            'parameters':{'geometry_mode':'export','report_mode':'compose'},'budget':{'timeout_seconds':180,'max_log_bytes':4*1024*1024}},
            extra_headers={'Idempotency-Key':str(uuid.uuid4())})
        original=wait_run(original['id'],'awaiting_confirmation')
        for step in original['steps']:
            for attempt in step['attempts']:stopped(attempt['job_id'],'succeeded')
        # Actual comment-only edit changes the confirmation revision without
        # changing engineering fingerprints. The UI must explicitly re-arm it.
        info=api.call('GET',api.prefix+'/caseDict')
        api.call('PUT',api.prefix+'/caseDict',{'content':info['content']+'\n// UI bookkeeping confirmation revision\n','expected_revision':info['dict_revision']})
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview','--host','127.0.0.1','--port',str(port),'--strictPort'],
            cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        report['preview_birth']=observe(preview.pid);preview_handle=ProcessHandle.query(report['preview_birth'])
        setup={'root':str(api.root),'web':'http://127.0.0.1:'+str(port),'email':user['email'],'case_id':case_id,'run_id':original['id'],
            'requires_bookkeeping_confirmation_rearm':True}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2),encoding='utf8');print('CASE_CHAIN_READY '+str(api.root),flush=True)
        report['original_browser']=wait_browser('browser-original-finished.json')
        finished=wait_run(original['id'],'succeeded');discover()
        for identifier in list(report['actual_native_jobs']):stopped(identifier,'succeeded')
        graph=api.call('GET',api.prefix+'/dependencies?run_dir='+RUN);assert graph['recompute']==[] and graph['needs_inspection']==[]
        report['original_numeric']=numeric('original',20)
        old_report=api.call('GET',api.prefix+'/report/results');old_id=old_report['publication']['id'];old_bytes=report_bytes(old_id)
        report['old_report']={'publication':old_report['publication'],'files':old_bytes}
        limits,old_history=history(api.root/'test.sqlite')
        report['checks'].append('actual_original_full_chain_with_explicit_B_C_D_E_and_rearmed_bookkeeping_confirmation')
        mesh=api.call('GET',api.prefix+'/raw?path='+quote(RUN+'/system/blockMeshDict',safe=''),raw=True).decode('utf8')
        assert '(20 4 4)' in mesh;api.put(RUN+'/system/blockMeshDict',mesh.replace('(20 4 4)','(24 4 4)'))
        stale=api.call('GET',api.prefix+'/dependencies?run_dir='+RUN)
        assert stale['recompute']==CORE[1:] and stale['needs_inspection']==[],stale
        report['stale_graph']=stale
        report['integrity_fault']=api.call('POST','/test/chain-integrity/'+str(case_id),{})
        unknown=api.call('GET',api.prefix+'/dependencies?run_dir='+RUN);assert 'geometry' in unknown['needs_inspection']
        (api.root/'browser-fault-setup.json').write_text(json.dumps({'old_run_id':original['id'],'old_report_id':old_id,'fault':report['integrity_fault']},indent=2),encoding='utf8')
        report['fault_browser']=wait_browser('browser-fault-observed.json')
        report['integrity_restore']=api.call('POST','/test/chain-restore/'+str(case_id),{})
        restored=api.call('GET',api.prefix+'/dependencies?run_dir='+RUN);assert restored['recompute']==CORE[1:] and restored['needs_inspection']==[]
        assert report_bytes(old_id)==old_bytes
        (api.root/'browser-recompute-ready.json').write_text(json.dumps({'old_run_id':original['id'],'expected_reused':['geometry'],'expected_rebuild':CORE[1:]},indent=2),encoding='utf8')
        report['recompute_browser']=wait_browser('browser-recompute-finished.json')
        successors=api.call('GET',api.prefix+'/workflow-runs')
        created=[r for r in successors if r['id']!=original['id']];assert len(created)==1,created
        new=wait_run(created[0]['id'],'succeeded')
        relation=new['recomputation'];assert relation['source_run_id']==original['id'] and set(relation['reused'])=={'geometry'} and relation['rebuild']==CORE[1:]
        geometry=next(step for step in new['steps'] if step['key']=='geometry');assert geometry['execution_mode']=='reuse' and geometry['attempts']==[]
        discover()
        for identifier in list(report['actual_native_jobs']):stopped(identifier,'succeeded')
        report['new_numeric']=numeric('recomputed',24)
        latest=api.call('GET',api.prefix+'/report/results');assert latest['publication']['id']!=old_id and latest['publication']['generation']>old_report['publication']['generation']
        assert report_bytes(old_id)==old_bytes;assert history(api.root/'test.sqlite',limits)[1]==old_history
        report['new_report']={'publication':latest['publication'],'files':report_bytes(latest['publication']['id'])}
        report['recomputation']={'run_id':new['id'],'source_run_id':original['id'],'reused':list(relation['reused']),'rebuild':relation['rebuild']}
        report['old_workflow_step_job_attempt_rows_unchanged']=True;report['old_report_bytes_still_readable_unchanged']=True
        report['checks'].append('actual_mesh_change_preserves_geometry_rebuilds_downstream_and_pinned_old_report_remains_readable')
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1600],'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                if (api.root/'fault-original.json').exists():report['integrity_restore_cleanup']=api.call('POST','/test/chain-restore/'+str(case_id),{})
                if case_id is not None:
                    discover()
                    for row in api.call('GET',api.prefix+'/workflow-runs'):
                        if row['status'] not in {'succeeded','failed','cancelled','interrupted'}:api.call('POST','/workflow-runs/'+row['id']+':cancel',{'expected_version':row['version']})
                    for identifier in report['actual_native_jobs']:
                        job=api.call('GET','/jobs/'+str(identifier))
                        if job['status'] in {'queued','running'}:api.call('POST','/jobs/'+str(identifier)+':cancel',{})
                        stopped(identifier)
                for row in api.call('GET','/terminal/sessions?include_closed=true')['sessions']:
                    if row.get('cleanup_verified') is not True:api.call('DELETE','/terminal/sessions/'+row['id'])
                report['terminal_cleanup']=api.call('GET','/terminal/sessions?include_closed=true')['sessions']
                assert all(row.get('cleanup_verified') is True for row in report['terminal_cleanup'])
            except BaseException as error:report['cleanup_failure']=type(error).__name__+': '+str(error)[:700]
        if preview:
            if preview.poll() is None:preview.terminate()
            preview.wait(timeout=15)
        if preview_handle:preview_handle.wait_exit(15);preview_handle.close();report['preview_exit_verified']=True
        if preview_log:preview_log.close()
        if entered:api.__exit__(None if report.get('functional_passed') else RuntimeError,None,None)
        if api_handle:api_handle.wait_exit(15);api_handle.close();report['API_exit_verified']=True
        report['all_actual_native_stops_verified']=all(str(identifier) in report.get('native_stops',{}) and report['native_stops'][str(identifier)]['stop']['verified'] is True for identifier in report['actual_native_jobs'])
        report['passed']=bool(report.get('functional_passed') and report.get('API_exit_verified') and report.get('preview_exit_verified') and report['all_actual_native_stops_verified'] and 'cleanup_failure' not in report)
        (api.root/'chain-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8');print('CASE_CHAIN_RESULT '+str(api.root/'chain-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
