"""Public upload quota claims across API peers, rollback and process death."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import tempfile
import time
import uuid
import httpx
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore
from storage_usage_smoke import RUNTIME

FIXTURE='''
import asyncio,time
from pathlib import Path
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.services import quota,uploads
from app.services.case_write import coordinate
from app.routers.workspaces import get_owned_case
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
async def quotas(db,user):return {**quota.DEFAULT_QUOTAS,'storage_mb':1}
quota.quotas_for=quotas
@app.get('/api/v1/fixture/usage')
async def usage(db=Depends(get_db),user=Depends(get_current_user)):return await quota.usage(db,user)
@app.post('/api/v1/fixture/legacy/{case_id}')
async def legacy(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 case=await get_owned_case(case_id,user,db)
 async with coordinate(db,case):
  result=await asyncio.to_thread(uploads.create_session,case.s3_prefix,'Ref/legacy.bin',65536,None,False)
  await asyncio.to_thread(uploads.write_chunk,case.s3_prefix,result['id'],0,b'l'*32768)
 await db.commit()
 return result
create=uploads.create_session
def creating(*args):
 if mode()=='hold_create':
  (root/'ready').write_text('ready')
  deadline=time.monotonic()+25
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('test release missing')
   time.sleep(.02)
 return create(*args)
uploads.create_session=creating
chunk=uploads.write_chunk
def writing(*args):
 result=chunk(*args)
 if mode()=='rollback_chunk':
  (root/'mode').write_text('normal');raise OSError('injected after durable payload before SQL progress')
 if mode()=='kill_chunk':
  (root/'ready').write_text('ready')
  time.sleep(25)
 return result
uploads.write_chunk=writing
cancel=uploads.cancel_session
def cancelling(*args):
 result=cancel(*args)
 if mode()=='rollback_cancel':
  (root/'mode').write_text('normal');raise OSError('injected after removal before settlement')
 return result
uploads.cancel_session=cancelling
commit=AsyncSession.commit
async def committing(self):
 result=await commit(self)
 if mode()=='lost_commit':
  (root/'mode').write_text('normal');raise TimeoutError('injected lost commit response')
 return result
AsyncSession.commit=committing
'''


def server(url=None,store=None,peer=None):
    api=IsolatedServer('storage-admission',app='admission_fixture:app',database_url=url)
    settings=store.settings() if store else {}
    if peer:
        settings.update({key:peer.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET','VIBEFLOW_INDEX_ROOT')})
        api.token=peer.token
    (api.root/'admission_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    (api.root/'mode').write_text('normal')
    return api


def upload(api,prefix,name,size):return api.call('POST',prefix+'/uploads',{'path':'Ref/'+name,'size':size})
def used(api):return api.call('GET','/fixture/usage')
def chunk(api,endpoint,offset,data,expect=200):
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=35) as client:
        response=client.put(endpoint,params={'offset':offset},content=data)
        assert response.status_code==expect,(response.status_code,response.text[:500])
        return response.json() if expect==200 else None


def wait_ready(api):
    deadline=time.monotonic()+15
    while not (api.root/'ready').exists():
        assert api.proc.poll() is None and time.monotonic()<deadline
        time.sleep(.03)


def verify(url=None,store=None):
    checks=[];api=server(url,store)
    with api:
        case=api.create_case('Quota claims');prefix=api.prefix
        workspace=api.call('GET','/workspaces')[0]
        other=api.call('POST',f"/workspaces/{workspace['id']}/cases",{'name':'Other case same owner'})
        other_prefix=f"/cases/{other['id']}"
        with server(url,store,api) as peer:
            first=upload(api,prefix,'one.bin',900*1024);endpoint=prefix+'/uploads/'+first['id']
            assert used(peer)['storage_bytes_reserved']==900*1024
            peer.call('POST',other_prefix+'/uploads',{'path':'Ref/two.bin','size':900*1024},expect=429)
            peer.call('PUT',other_prefix+'/file',{'path':'Ref/over.txt','content':'x'*(2*1024*1024)},expect=429)
            checks.append('declared_capacity_is_durable_across_cases_peers_and_text_writes')
            peer.call('POST',other_prefix+'/uploads/'+first['id']+':complete',expect=404)
            peer.call('DELETE',other_prefix+'/uploads/'+first['id'],expect=404)
            assert used(peer)['storage_bytes_reserved']==900*1024
            checks.append('foreign_case_cannot_consume_or_release_reservation')
            peer.call('DELETE',endpoint);peer.call('DELETE',endpoint)
            assert used(peer)['storage_bytes_reserved']==0
            checks.append('cancel_releases_future_payload_and_replays_idempotently')

            (api.root/'mode').write_text('hold_create')
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(upload,api,prefix,'race-one.bin',900*1024)
                wait_ready(api)
                b=pool.submit(peer.call,'POST',other_prefix+'/uploads',{'path':'Ref/race-two.bin','size':900*1024},expect=(409,429))
                time.sleep(.2)
                (api.root/'release').write_text('release')
                winner=a.result(timeout=30);b.result(timeout=30)
            (api.root/'mode').write_text('normal');(api.root/'ready').unlink()
            assert used(peer)['storage_bytes_reserved']==900*1024
            peer.call('POST',other_prefix+'/uploads',{'path':'Ref/race-two.bin','size':900*1024},expect=429)
            peer.call('DELETE',prefix+'/uploads/'+winner['id'])
            checks.append('two_API_writers_cannot_admit_more_than_one_oversubscribed_upload')

            small=upload(api,prefix,'small.bin',65536);small_endpoint=prefix+'/uploads/'+small['id']
            (api.root/'mode').write_text('rollback_chunk')
            chunk(api,small_endpoint,0,b'a'*32768,expect=500)
            assert used(peer)['storage_bytes_reserved']==65536
            assert peer.call('GET',small_endpoint)['offset']==32768
            chunk(peer,small_endpoint,32768,b'b'*32768)
            assert used(peer)['storage_bytes_reserved']==0
            done=peer.call('POST',small_endpoint+':complete')
            assert peer.call('POST',small_endpoint+':complete')==done
            assert peer.call('GET',prefix+'/raw?path=Ref/small.bin',raw=True)==b'a'*32768+b'b'*32768
            assert not peer.call('GET',prefix+'/uploads')
            checks.append('rollback_after_payload_reconciles_offset_without_double_credit')
            checks.append('completion_settlement_and_replay_share_committed_receipt')

            pending=upload(api,prefix,'cancel-crash.bin',65536);pending_endpoint=prefix+'/uploads/'+pending['id']
            (api.root/'mode').write_text('rollback_cancel')
            api.call('DELETE',pending_endpoint,expect=500,raw=True)
            assert used(peer)['storage_bytes_reserved']==65536
            peer.call('DELETE',pending_endpoint)
            assert used(peer)['storage_bytes_reserved']==0
            checks.append('cancel_rollback_releases_only_after_verified_retry')

            (api.root/'mode').write_text('lost_commit')
            api.call('POST',prefix+'/uploads',{'path':'Ref/lost-reply.bin','size':65536},expect=500,raw=True)
            recovered=peer.call('GET',prefix+'/uploads')
            assert len(recovered)==1 and recovered[0]['remaining_bytes']==65536 and recovered[0]['storage_binding_valid']
            peer.call('DELETE',prefix+'/uploads/'+recovered[0]['id'])
            assert used(peer)['storage_bytes_reserved']==0
            checks.append('lost_create_commit_reply_is_discoverable_and_not_forgotten')

            legacy=peer.call('POST',f"/fixture/legacy/{case['id']}")
            legacy_endpoint=prefix+'/uploads/'+legacy['id']
            chunk(peer,legacy_endpoint,32768,b'l'*32768)
            assert used(peer)['storage_bytes_reserved']==0
            peer.call('POST',legacy_endpoint+':complete')
            checks.append('legacy_partial_upload_adopts_only_verified_remaining_payload')

            dying=upload(api,prefix,'killed-writer.bin',65536);dying_endpoint=prefix+'/uploads/'+dying['id']
            (api.root/'mode').write_text('kill_chunk')
            with ThreadPoolExecutor(max_workers=1) as pool:
                operation=pool.submit(chunk,api,dying_endpoint,0,b'k'*32768)
                wait_ready(api);api.proc.terminate();api.proc.wait(timeout=10)
                try:operation.result(timeout=10)
                except httpx.TransportError:pass
                else:raise AssertionError('Killed request returned success')
            assert used(peer)['storage_bytes_reserved']==65536
            assert peer.call('GET',dying_endpoint)['offset']==32768
            chunk(peer,dying_endpoint,32768,b'r'*32768)
            assert used(peer)['storage_bytes_reserved']==0
            peer.call('POST',dying_endpoint+':complete')
            checks.append('process_death_preserves_claim_and_peer_resumes_actual_stored_offset')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'admission-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('PASS '+json.dumps(result),flush=True)
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='storage-admission-evidence-',dir=SERVER.parents[1]/'tmp'))
    from contextlib import nullcontext
    with ObjectStore(executable=RUNTIME,volume_max=24) if args.s3 else nullcontext() as store:
        results={'sqlite':await asyncio.to_thread(verify,None,store)}
    if args.postgres:
            import asyncpg
            from sqlalchemy.engine import make_url
            from app.config import Settings
            from workflow_migration_smoke import alembic
            url=make_url(Settings(_env_file=SERVER/'.env').database_url)
            assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
            name='tmp_vf_storageadmission_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_storageadmission_[0-9a-f]{32}',name)
            admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
            created=False
            try:
                await admin.execute('CREATE DATABASE "'+name+'"');created=True
                target=url.set(database=name).render_as_string(hide_password=False)
                await asyncio.to_thread(alembic,target,report,'upgrade','head')
                with ObjectStore(executable=RUNTIME,volume_max=24) if args.s3 else nullcontext() as store:
                    results['postgresql']=await asyncio.to_thread(verify,target,store)
            finally:
                if created:
                    assert await admin.fetchval('SELECT current_database()')=='postgres'
                    await admin.execute('DROP DATABASE "'+name+'"')
                await admin.close()
            results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('STORAGE_ADMISSION_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
