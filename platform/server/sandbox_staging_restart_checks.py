"""Real API termination at staging metadata and SQL commit boundaries.

All injected writes are actual bytes in an isolated real S3/WSL case. Faults
neither intercept model traffic nor manufacture model/process responses.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sandbox_retirement_checks import verify

FIXTURE = '''
import os,json
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import sandbox_staging_cleanup as cleanup
def phase():
 p=Path('staging-fault.json')
 return json.loads(p.read_text())['phase'] if p.exists() else None
def die(label):
 Path('staging-fault.json').unlink()
 with Path('staging-deaths.jsonl').open('a') as f:
  f.write(json.dumps({'phase':label,'pid':os.getpid()})+'\\n');f.flush();os.fsync(f.fileno())
 os._exit(78)
_write=cleanup.write_expected
def write(path,encoded,maximum):
 kind='plan' if path.parent.name==cleanup.DIRECTORY else 'marker'
 if phase()==kind+'_prefix':
  with path.open('xb') as stream:
   stream.write(encoded[:17]);stream.flush();os.fsync(stream.fileno())
  die(kind+'_prefix')
 _write(path,encoded,maximum)
 if phase()==kind+'_complete':die(kind+'_complete')
cleanup.write_expected=write
_remove=cleanup.remove_entry
def remove(*args):
 _remove(*args)
 if args[2]['kind']=='file' and phase()=='unlink':die('unlink')
cleanup.remove_entry=remove
_event=cleanup.append_event
async def event(db,case_id,kind,*args,**kwargs):
 result=await _event(db,case_id,kind,*args,**kwargs)
 if kind in {cleanup.INTENT,cleanup.DONE}:
  db.info['staging_commit']='intent' if kind==cleanup.INTENT else 'completion'
 return result
cleanup.append_event=event
_commit=AsyncSession.commit
async def commit(db):
 kind=db.info.pop('staging_commit',None)
 if kind and phase()==kind+'_before_commit':die(kind+'_before_commit')
 await _commit(db)
 if kind and phase()==kind+'_after_commit':die(kind+'_after_commit')
AsyncSession.commit=commit
'''


async def after_retirement(api, sid, rid, proof, continuation=None):
    report = {'passed':False,'llm_calls':0,'mock_llm_calls':0,'checks':[]}
    endpoint='/terminal/sessions/'+sid+'/staging-cleanup'
    body={'recovery_id':rid,'confirm':True}
    def save():
        trace=api.root/'upstream.jsonl'
        if trace.exists():
            rows=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            report['llm_calls']=sum(r['kind']=='http' and r['path'].endswith('/chat/completions') for r in rows)
        (api.root/'staging-restart-checks.json').write_text(json.dumps(report,indent=2))
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind});save()
        print('STAGING_RESTART_PASS '+name,flush=True)
    def query(sql,args=()):
        with sqlite3.connect(api.root/'test.sqlite') as db:return db.execute(sql,args).fetchall()
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    async def admin(expected):return await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False,expect=expected)
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
    async def crash(phase):
        (api.root/'staging-fault.json').write_text(json.dumps({'phase':phase}))
        try:
            await call('POST',endpoint,body=body)
            raise AssertionError('API did not terminate at '+phase)
        except (OSError,ConnectionError):pass
        assert await asyncio.to_thread(api.proc.wait,15)==78
        assert json.loads((api.root/'staging-deaths.jsonl').read_text().splitlines()[-1])['phase']==phase
        await start()
    try:
        selected=json.loads(query('SELECT profile_json FROM native_sessions WHERE id=?',(sid,))[0][0])
        snapshot=json.loads(query('SELECT profile_json FROM native_requests WHERE id=?',(rid,))[0][0])
        stage=api.root/'files'/selected['staging_prefix'];assert stage.resolve().parent==(api.root/'files/.staging').resolve()
        manifest=stage.with_name(stage.name+'.manifest.json');marker=stage.with_name(stage.name+'.terminal-cleanup.json')
        plan=api.root/'files/.terminal-cleanup'/(sid+'.json')
        original={p.relative_to(stage).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.rglob('*') if p.is_file()}
        def intact():
            assert all(hashlib.sha256((stage/name).read_bytes()).hexdigest()==sha for name,sha in original.items())
        await crash('intent_before_commit')
        assert not query("SELECT id FROM durable_events WHERE type='terminal.staging_cleanup_started'")
        assert not plan.exists();intact();await admin(409)
        record('intent_rollback_on_actual_api_exit_precedes_all_metadata_and_deletion')
        await crash('intent_after_commit')
        assert len(query("SELECT id FROM durable_events WHERE type='terminal.staging_cleanup_started'"))==1
        assert not plan.exists();intact();assert (await call('GET',endpoint))['status']=='pending';await admin(409)
        record('committed_intent_survives_before_first_plan_byte')
        await crash('plan_prefix')
        assert plan.stat().st_size==17;intact();await admin(409)
        partial=plan.read_bytes()
        try:
            plan.write_bytes(b'unrecognized metadata')
            await call('POST',endpoint,body=body,expect=409)
            assert plan.read_bytes()==b'unrecognized metadata';intact()
        finally:plan.write_bytes(partial)
        record('unknown_plan_bytes_are_preserved_and_never_repaired','protocol_fault')
        await crash('plan_complete')
        intent=json.loads(query("SELECT payload_json FROM durable_events WHERE type='terminal.staging_cleanup_started'")[0][0])
        assert hashlib.sha256(plan.read_bytes()).hexdigest()==intent['plan_sha256'];intact()
        record('actual_partial_plan_reconstructed_only_against_committed_hash')
        await crash('marker_prefix')
        assert marker.stat().st_size==17;intact();partial=marker.read_bytes()
        try:
            marker.write_bytes(b'unknown other cleanup')
            await call('POST',endpoint,body=body,expect=409)
            assert marker.read_bytes()==b'unknown other cleanup';intact()
        finally:marker.write_bytes(partial)
        record('unknown_marker_bytes_are_preserved','protocol_fault')
        await crash('marker_complete')
        assert json.loads(marker.read_bytes())['intent_id']==intent['intent_id'];intact()
        record('actual_partial_marker_finished_before_any_unlink')
        await crash('unlink')
        remaining=[p for p in stage.rglob('*') if p.is_file()]
        assert len(remaining)==len(original)-1
        full=plan.read_bytes()
        try:
            plan.write_bytes(full[:17])
            await call('POST',endpoint,body=body,expect=409)
            assert plan.read_bytes()==full[:17] and all(p.exists() for p in remaining)
        finally:plan.write_bytes(full)
        record('damaged_plan_after_deletion_is_not_invented_from_survivors','protocol_fault')
        await crash('completion_before_commit')
        assert not any(p.exists() for p in (stage,manifest,marker))
        assert not query("SELECT id FROM durable_events WHERE type='terminal.staging_cleanup_completed'")
        assert (await call('GET',endpoint))['status']=='pending';await admin(409)
        record('all_files_absent_without_completion_commit_still_blocks_admin')
        await crash('completion_after_commit')
        done=await call('GET',endpoint);assert done['status']=='completed'
        assert await call('POST',endpoint,body=body)==done
        assert len(query("SELECT id FROM durable_events WHERE type='terminal.staging_cleanup_completed'"))==1
        await admin(200)
        record('actual_commit_before_response_loss_returns_one_idempotent_receipt')
        assert (Path(snapshot['root'])/'recovery.zip').exists()
        await call('GET',api.prefix+'/raw?path=Ref/retirement.txt',expect=404)
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_restored_file_after_all_restart_boundaries','real_llm')
        report['passed']=True
        return report['llm_calls']
    finally:save()


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    from s3testsupport import ObjectStore
    with ObjectStore(executable=Path(__file__).resolve().parents[2]/'third_party/seaweed-4.52-runtime/weed') as store:
        asyncio.run(verify(store,fixture_prefix=FIXTURE,after_retirement=after_retirement))
