"""Actual API death after persisted preparation, before request registration."""
import argparse
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
import os,json
from pathlib import Path
from app.services import sandbox_image_launch as launch
_observed=launch.observed
async def observed(owner,child_id,value):
 await _observed(owner,child_id,value)
 fault=Path('image-preparation-fault.json')
 if fault.exists() and json.loads(fault.read_text())['stage']==value['stage']:
  fault.unlink()
  with Path('image-preparation-deaths.jsonl').open('a') as stream:
   stream.write(json.dumps({'request_id':owner.id,'child_id':child_id,'preparation':value})+'\\n')
   stream.flush();os.fsync(stream.fileno())
  os._exit(81)
launch.observed=observed
'''


async def after_retirement(api,sid,rid,proof,continuation=None,ambiguous=False):
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'checks':[],'ambiguous_host_ready':ambiguous}
    endpoint='/terminal/sessions/'+sid+'/image-cleanup';body={'recovery_id':rid,'confirm':True}
    def query(sql,args=()):
        with sqlite3.connect(api.root/'test.sqlite') as db:return db.execute(sql,args).fetchall()
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind});print('IMAGE_PREPARATION_PASS '+name,flush=True)
    async def start():
        api.proc=subprocess.Popen(api.proc.args,cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        async with asyncio.timeout(40):
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET','/api/health',api_prefix=False)).get('ok'):return
                except OSError:pass
                await asyncio.sleep(.1)
    killed=[]
    try:
        snapshot=json.loads(query('SELECT profile_json FROM native_requests WHERE id=?',(rid,))[0][0])
        image=Path(snapshot['image_root'])/'volume.ext4';archive=Path(snapshot['root'])/'recovery.zip'
        original=archive.read_bytes()
        assert image.resolve().parent.parent==(api.root/'files/.terminal-export').resolve()
        for stage in (('host_ready',) if ambiguous else ('created','native_ready')):
            (api.root/'image-preparation-fault.json').write_text(json.dumps({'stage':stage}))
            try:
                await call('POST',endpoint,body=body)
                raise AssertionError('API did not exit at '+stage)
            except (OSError,ConnectionError):pass
            assert await asyncio.to_thread(api.proc.wait,15)==81
            value=json.loads((api.root/'image-preparation-deaths.jsonl').read_text().splitlines()[-1])
            assert value['preparation']['stage']==stage
            owner,children=query('SELECT owner_identity_json,children_json FROM native_requests WHERE id=?',(value['request_id'],))[0]
            child=json.loads(children)['children'][value['child_id']]
            assert child['phase']=='preparing' and child['identity'] is None and child['stop'] is None
            birth=value['preparation']
            async with asyncio.timeout(45):
                while True:
                    observations={'owner':identity.probe(json.loads(owner)), 'host':identity.probe(birth['host'])}
                    if birth['native']:
                        observations['native']=await probe_native(birth['native']['distribution'],birth['native']['process'])
                    if all(item['state']=='exited' for item in observations.values()):break
                    await asyncio.sleep(.1)
            killed.append(value);report.setdefault('actual_exits',[]).append({'stage':stage,'observations':observations})
            await start()
            assert (await call('GET',endpoint))['status']=='pending'
            assert image.exists() and archive.read_bytes()==original
            record('actual_'+stage+'_owner_exit_preserves_unregistered_journal_and_image')
        if ambiguous:
            before=query("SELECT id,phase,version,children_json FROM native_requests WHERE kind='terminal.cleanup'")
            await call('POST',endpoint,body=body,expect=409)
            assert query("SELECT id,phase,version,children_json FROM native_requests WHERE kind='terminal.cleanup'")==before
            assert not query("SELECT id FROM durable_events WHERE type='terminal.image_cleanup_abandoned'")
            assert image.exists() and archive.read_bytes()==original
            record('host_ready_without_native_birth_remains_blocked_without_invented_exit')
        else:
            done=await call('POST',endpoint,body=body)
            assert done['status']=='completed' and done['deleted_this_request'] and not image.exists()
            proofs=query("SELECT payload_json FROM durable_events WHERE type='terminal.image_cleanup_abandoned'")
            assert len(proofs)==2
            values={json.loads(raw)['request_id']:json.loads(raw) for raw, in proofs}
            for killed_request in killed:
                document=values[killed_request['request_id']]
                assert set(document['launch_sha256'])=={killed_request['child_id']}
                expected=2 if killed_request['preparation']['stage']=='created' else 3
                assert len(document['observations'])==expected
                old=query('SELECT phase,children_json FROM native_requests WHERE id=?',(document['request_id'],))[0]
                assert old[0]=='failed'
                child=json.loads(old[1])['children'][killed_request['child_id']]
                assert child['phase']=='preparing' and child['identity'] is None and child['stop'] is None
            await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False)
            await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False)
            record('created_and_native_ready_evidence_fences_old_requests_without_forging_registration')
            identifier,raw=query("SELECT id,payload_json FROM durable_events WHERE type='terminal.image_launch_observed' ORDER BY seq LIMIT 1")[0]
            corrupt=json.loads(raw);corrupt['preparation']['observed_at']='2000-01-01T00:00:00+00:00'
            try:
                with sqlite3.connect(api.root/'test.sqlite') as db:
                    db.execute('UPDATE durable_events SET payload_json=? WHERE id=?',(json.dumps(corrupt),identifier))
                await call('GET',endpoint,expect=409)
                await call('POST',endpoint,body=body,expect=409)
                await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False,expect=409)
            finally:
                with sqlite3.connect(api.root/'test.sqlite') as db:
                    db.execute('UPDATE durable_events SET payload_json=? WHERE id=?',(raw,identifier))
            record('changed_startup_evidence_revokes_completion_and_admission','protocol_fault')
            assert await call('GET',endpoint)==done
            if continuation:
                report['llm_calls']=await continuation(api,sid,rid,proof)
                record('real_model_reads_snapshot_after_unregistered_startup_reconciliation','real_llm')
        report['passed']=True
        return report['llm_calls']
    finally:
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(row['kind']=='http' and row['path'].endswith('/chat/completions') for row in map(json.loads,trace.read_text().splitlines()))
        (api.root/'image-preparation-checks.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser();parser.add_argument('--ambiguous',action='store_true');args=parser.parse_args()
    async def acceptance(api,sid,rid,proof):return await after_retirement(api,sid,rid,proof,ambiguous=args.ambiguous)
    asyncio.run(verify(fixture_prefix=FIXTURE,after_retirement=acceptance))
