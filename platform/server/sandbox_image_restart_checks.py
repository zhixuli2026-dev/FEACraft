"""Actual API death during registered image cleanup; no model substitution."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sandbox_retirement_checks import verify
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native

FIXTURE = '''
import asyncio,os,json
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import sandbox_image_cleanup as cleanup,native_request_records as records
from app.services.native_request_execution import RequestExecutor
def phase():
 p=Path('image-fault.json')
 return json.loads(p.read_text())['phase'] if p.exists() else None
def die(label):
 Path('image-fault.json').unlink()
 with Path('image-deaths.jsonl').open('a') as stream:
  stream.write(json.dumps({'phase':label,'pid':os.getpid()})+'\\n');stream.flush();os.fsync(stream.fileno())
 os._exit(79)
_create=records.create
async def create(*args,**kwargs):
 result=await _create(*args,**kwargs)
 if phase()=='owner_created':
  Path('image-owner-ready.json').write_text(json.dumps({'request_id':result.id}))
  async with asyncio.timeout(60):
   while not Path('image-owner-exit').exists():await asyncio.sleep(.05)
  die('owner_created')
 return result
records.create=create
_register=records.register
async def register(*args,**kwargs):
 result=await _register(*args,**kwargs)
 if phase()=='child_registered':die('child_registered')
 return result
records.register=register
_release=records.release
async def release(*args,**kwargs):
 result=await _release(*args,**kwargs)
 if phase()=='child_released':die('child_released')
 return result
records.release=release
_run=RequestExecutor.run_sync
async def run(self,*args,**kwargs):
 result=await _run(self,*args,**kwargs)
 if phase()=='native_finished':die('native_finished')
 return result
RequestExecutor.run_sync=run
_complete=records.complete
async def complete(*args,**kwargs):
 result=await _complete(*args,**kwargs)
 if phase()=='request_completed':die('request_completed')
 return result
records.complete=complete
_event=cleanup.append_event
async def event(db,case_id,kind,*args,**kwargs):
 result=await _event(db,case_id,kind,*args,**kwargs)
 if kind==cleanup.DONE:db.info['image_completion']=True
 return result
cleanup.append_event=event
_commit=AsyncSession.commit
async def commit(db):
 completing=db.info.pop('image_completion',False)
 if completing and phase()=='completion_before_commit':die('completion_before_commit')
 await _commit(db)
 if completing and phase()=='completion_after_commit':die('completion_after_commit')
AsyncSession.commit=commit
'''


async def after_retirement(api, sid, rid, proof, continuation=None):
    report = {'passed':False, 'llm_calls':0, 'mock_llm_calls':0, 'checks':[]}
    endpoint = '/terminal/sessions/' + sid + '/image-cleanup'
    body = {'recovery_id':rid, 'confirm':True}
    def query(sql, args=()):
        with sqlite3.connect(api.root/'test.sqlite') as db:
            return db.execute(sql,args).fetchall()
    def save():
        trace = api.root/'upstream.jsonl'
        if trace.exists():
            report['llm_calls'] = sum(row['kind']=='http' and row['path'].endswith('/chat/completions')
                for row in map(json.loads,trace.read_text().splitlines()))
        (api.root/'image-restart-checks.json').write_text(json.dumps(report,indent=2))
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind});save()
        print('IMAGE_RESTART_PASS '+name,flush=True)
    async def call(method,path,**kwargs):
        return await asyncio.to_thread(api.call,method,path,**kwargs)
    def journal_rows():
        return query("SELECT id,phase,children_json,result_json,owner_identity_json FROM native_requests WHERE kind='terminal.cleanup'")
    async def start():
        api.proc=subprocess.Popen(api.proc.args,cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        async with asyncio.timeout(40):
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET','/api/health',api_prefix=False)).get('ok'):break
                except OSError:pass
                await asyncio.sleep(.1)
    async def exited():
        async with asyncio.timeout(60):
            while True:
                observations=[]
                for request_id,phase,raw,_,owner in journal_rows():
                    if phase!='active':continue
                    observations.append({'request_id':request_id,'role':'owner','observation':identity.probe(json.loads(owner))})
                    for child in json.loads(raw).get('children',{}).values():
                        process=child['identity'];assert process is not None
                        observations.append({'request_id':request_id,'role':'host','observation':identity.probe(process['host'])})
                        observations.append({'request_id':request_id,'role':'native',
                            'observation':await probe_native(process['native']['distribution'],process['native']['process'])})
                if all(item['observation']['state']=='exited' for item in observations):
                    report.setdefault('exit_observations',[]).append(observations);save();return
                await asyncio.sleep(.2)
    async def crash(label):
        (api.root/'image-fault.json').write_text(json.dumps({'phase':label}))
        task=asyncio.create_task(call('POST',endpoint,body=body))
        if label=='owner_created':
            async with asyncio.timeout(20):
                while not (api.root/'image-owner-ready.json').exists():await asyncio.sleep(.05)
            before=journal_rows()
            assert (await call('GET',endpoint))['status']=='pending'
            await call('POST',endpoint,body=body,expect=409)
            assert journal_rows()==before
            assert image.exists()
            record('live_owner_blocks_retry_and_get_does_not_fence')
            (api.root/'image-owner-exit').write_text('exit isolated API')
        try:
            await task
            raise AssertionError('API did not exit at '+label)
        except (OSError,ConnectionError):pass
        assert await asyncio.to_thread(api.proc.wait,15)==79
        assert json.loads((api.root/'image-deaths.jsonl').read_text().splitlines()[-1])['phase']==label
        before={row[0]:row[2:] for row in journal_rows()}
        await exited();await start()
        after={row[0]:row[2:] for row in journal_rows()}
        assert before==after,'Restart must preserve original journals'
        record('actual_api_exit_'+label)
    try:
        snapshot=json.loads(query('SELECT profile_json FROM native_requests WHERE id=?',(rid,))[0][0])
        image=Path(snapshot['image_root'])/'volume.ext4'
        archive=Path(snapshot['root'])/'recovery.zip';original=archive.read_bytes()
        assert image.resolve().parent.parent==(api.root/'files/.terminal-export').resolve()
        for label in ('owner_created','child_registered','child_released','native_finished',
                      'request_completed','completion_before_commit','completion_after_commit'):
            await crash(label)
            assert archive.read_bytes()==original
            state=await call('GET',endpoint)
            assert state['status']==('completed' if label=='completion_after_commit' else 'pending')
            await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False,
                expect=200 if label=='completion_after_commit' else 409)
        done=await call('GET',endpoint)
        assert done['image_absent'] and done['deleted_this_request'] is False and not image.exists()
        assert await call('POST',endpoint,body=body)==done
        assert len(query("SELECT id FROM durable_events WHERE type='terminal.image_cleanup_completed'"))==1
        proofs=query("SELECT id,payload_json FROM durable_events WHERE type='terminal.image_cleanup_abandoned'")
        assert len(proofs)==4
        for identifier,raw in proofs:
            value=json.loads(raw)
            assert value['normal_stop_receipt_invented'] is False
            phase,journal=query('SELECT phase,children_json FROM native_requests WHERE id=?',(value['request_id'],))[0]
            assert phase=='failed' and all(item['observation']['state']=='exited' for item in value['observations'])
            if json.loads(journal).get('children'):
                assert len(value['observations'])==3
        await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False)
        record('independent_exit_proofs_admit_retry_without_forging_child_stop_or_deletion')
        identifier,raw=proofs[0];corrupt=json.loads(raw);corrupt['observations'][0]['observation']['state']='match'
        try:
            with sqlite3.connect(api.root/'test.sqlite') as db:
                db.execute('UPDATE durable_events SET payload_json=? WHERE id=?',(json.dumps(corrupt),identifier))
            await call('GET',endpoint,expect=409)
            await call('POST',endpoint,body=body,expect=409)
            await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False,expect=409)
            await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False,expect=409)
        finally:
            with sqlite3.connect(api.root/'test.sqlite') as db:
                db.execute('UPDATE durable_events SET payload_json=? WHERE id=?',(raw,identifier))
        record('corrupt_reconciliation_proof_blocks_status_retry_validation_and_admin','protocol_fault')
        await call('GET','/terminal/sessions/'+sid+'/recoveries/'+rid+'/download',raw=True)
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_restored_file_after_seven_cleanup_api_exits','real_llm')
        report.update(passed=True,receipt=done)
        return report['llm_calls']
    finally:save()


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    asyncio.run(verify(fixture_prefix=FIXTURE,after_retirement=after_retirement))
