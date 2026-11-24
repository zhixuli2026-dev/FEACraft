"""Actual preparing-task cancellation with a still-live API owner; no fake model."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine
from app.models import NativeRequest, DurableEvent
from app.services import process_identity_native as identity
from sandbox_retirement_checks import verify

FIXTURE = '''
import asyncio,json,os
from pathlib import Path
from app.services import sandbox_image_launch as launch
_observed=launch.observed
async def observed(owner,child_id,value):
 await _observed(owner,child_id,value)
 armed=Path('cancel-image-created')
 if value['stage']=='created' and armed.exists():
  armed.unlink()
  Path('cancelled-image-created.json').write_text(json.dumps({'request_id':owner.id,'child_id':child_id,'preparation':value,'api_pid':os.getpid()}))
  asyncio.current_task().cancel()
  await asyncio.sleep(0)
launch.observed=observed
'''


async def after_retirement(api,sid,rid,proof,continuation=None,expect_blocked=False):
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'checks':[],
        'database':'postgresql' if api.database_url else 'sqlite','expect_old_blocker':expect_blocked}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    endpoint='/terminal/sessions/'+sid+'/image-cleanup';body={'recovery_id':rid,'confirm':True}
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    async def row(identifier):
        async with engine.connect() as db:
            return dict((await db.execute(select(NativeRequest.__table__).where(NativeRequest.id==identifier))).mappings().one())
    async def events():
        async with engine.connect() as db:
            return list((await db.execute(select(DurableEvent.id,DurableEvent.payload_json).where(
                DurableEvent.type=='terminal.image_cleanup_abandoned'))).all())
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind});print('IMAGE_LIVE_OWNER_PASS '+name,flush=True)
    def claims():
        parent=api.root/'files/.node-storage-budget'
        return {path.name:json.loads(path.read_bytes())['bytes'] for path in parent.glob('*.json')}
    try:
        selected=json.loads((await row(rid))['profile_json'])
        image=Path(selected['image_root'])/'volume.ext4';archive=Path(selected['root'])/'recovery.zip';raw=archive.read_bytes()
        assert image.resolve().parent.parent==(api.root/'files/.terminal-export').resolve()
        launcher_pid=api.proc.pid
        before_claims=claims()
        (api.root/'cancel-image-created').write_text('cancel the actual native task after created observation')
        await call('POST',endpoint,body=body,expect=409)
        created=json.loads((api.root/'cancelled-image-created.json').read_text())
        old=await row(created['request_id']);children=json.loads(old['children_json'])['children']
        child=children[created['child_id']]
        assert old['phase'] in {'failed','cancelled'} and old['ended_at'] is not None
        assert child['identity'] is None and child['stop'] is None
        assert old['error'] and child['error']
        owner=json.loads(old['owner_identity_json']);host=created['preparation']['host']
        assert owner['pid']==created['api_pid'] and identity.probe(owner)['state']=='match'
        assert identity.probe(host)['state']=='exited'
        report['actual_observations']={'owner':identity.probe(owner),'created_host':identity.probe(host)}
        assert image.exists() and archive.read_bytes()==raw
        record('actual_created_task_cancelled_host_exited_api_owner_still_live')
        if expect_blocked:
            await call('POST',endpoint,body=body,expect=409)
            assert await row(old['id'])==old and not await events()
            assert image.exists()
            record('reproduced_old_terminal_request_blocked_by_live_api_owner')
        else:
            report['node_claims']={'before':before_claims,'after_cancel':claims()}
            assert not (set(claims())-set(before_claims)),'Known stopped preparation leaked its node claim'
            record('known_preparation_exit_releases_owned_node_capacity_before_retry')
            done=await call('POST',endpoint,body=body)
            assert done['status']=='completed' and not image.exists() and done['deleted_this_request']
            assert api.proc.pid==launcher_pid and api.proc.poll() is None
            assert identity.probe(owner)['state']=='match'
            after=await row(old['id'])
            assert after['children_json']==old['children_json'] and after['result_json']==old['result_json']
            assert after['phase']==old['phase'] and after['version']==old['version']+1
            values=await events();assert len(values)==1
            event_id,payload=values[0];document=json.loads(payload)
            assert document['owner_fence']=='terminal_request'
            assert document['observations'][0]['observation']['state']=='match'
            assert all(item['observation']['state']=='exited' for item in document['observations'][1:])
            assert document['original']['phase']==old['phase']
            await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False)
            await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False)
            record('terminal_request_fence_allows_retry_without_claiming_api_owner_exited')
            corrupted=json.loads(payload);corrupted['owner_fence']='owner_exited'
            try:
                async with engine.begin() as db:
                    await db.execute(update(DurableEvent).where(DurableEvent.id==event_id).values(payload_json=json.dumps(corrupted)))
                await call('GET',endpoint,expect=409)
                await call('POST',endpoint,body=body,expect=409)
                await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False,expect=409)
            finally:
                async with engine.begin() as db:
                    await db.execute(update(DurableEvent).where(DurableEvent.id==event_id).values(payload_json=payload))
            record('claiming_live_owner_exited_invalidates_reconciliation','protocol_fault')
            assert await call('GET',endpoint)==done
            assert archive.read_bytes()==raw
            assert not (set(claims())-set(before_claims))
            if continuation:
                report['llm_calls']=await continuation(api,sid,rid,proof)
                record('real_model_reads_restored_file_after_preparation_cancel_retry','real_llm')
        report['passed']=True
        return report['llm_calls']
    finally:
        await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(row['kind']=='http' and row['path'].endswith('/chat/completions') for row in map(json.loads,trace.read_text().splitlines()))
        (api.root/'image-live-owner-checks.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser();parser.add_argument('--expect-blocked',action='store_true');args=parser.parse_args()
    async def acceptance(api,sid,rid,proof):return await after_retirement(api,sid,rid,proof,expect_blocked=args.expect_blocked)
    asyncio.run(verify(fixture_prefix=FIXTURE,after_retirement=acceptance))
