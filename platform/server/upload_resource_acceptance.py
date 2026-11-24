"""Actual upload node peaks, bounded HTTP bodies and two-API/crash interleavings."""
import argparse,asyncio,ast,hashlib,json,os,re,sys,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import httpx
from testsupport import IsolatedServer,SERVER

SOURCES=['app/services/uploads.py','app/routers/files.py','app/services/upload_storage.py',
    'app/services/upload_chunk_credit.py','app/services/owner_storage_budget.py','app/services/node_storage_budget.py']
def hashes():return {n:hashlib.sha256((SERVER/n).read_bytes()).hexdigest() for n in SOURCES}

def service():
    root=Path(tempfile.mkdtemp(prefix='upload-resource-service-',dir=SERVER.parents[1]/'tmp'))
    os.environ.update(VIBEFLOW_STORAGE_BACKEND='fs',VIBEFLOW_STORAGE_FS_ROOT=str(root/'public'),
        VIBEFLOW_SKILLS_STORAGE_ROOT=str(root/'skills'),VIBEFLOW_CODEX_STATE_ROOT=str(root/'cli'),
        VIBEFLOW_CODEX_WSL_STATE_ROOT='',VIBEFLOW_NODE_STORAGE_LIMIT_MB='1',VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0')
    from app.config import get_settings
    get_settings.cache_clear()
    from app.services import uploads,node_storage_budget as node
    from app.services.case_write import CaseWriteConflict
    prefix='workspaces/1/cases/1';base=root/'public'/prefix;base.mkdir(parents=True)
    filler=root/'public/pressure.bin';checks=[]
    result={'passed':False,'kind':'actual_FS_node_claims','model_calls':0,'mock_llm_calls':0,'checks':checks}
    def refused(operation):
        try:operation()
        except CaseWriteConflict as error:assert error.code=='node_storage_full';return
        raise AssertionError('Full actual node accepted an upload allocation')
    try:
        filler.write_bytes(b'P'*(1200*1024))
        refused(lambda:uploads.create_session(prefix,'Ref/refused.bin',262144,None,False))
        assert not (base/'.vibeflow/uploads').exists();checks.append({'check':'creation_refused_before_session_directory'})
        filler.unlink();made=uploads.create_session(prefix,'Ref/actual.bin',262144,None,False)
        directory=base/'.vibeflow/uploads'/made['id'];before=(directory/'meta.json').read_bytes()
        filler.write_bytes(b'P'*(1200*1024))
        refused(lambda:uploads.write_chunk(prefix,made['id'],0,b'D'*262144))
        assert (directory/'data.part').stat().st_size==0 and (directory/'meta.json').read_bytes()==before
        checks.append({'check':'append_and_metadata_refused_before_either_write'});filler.unlink()
        seen=[];original=uploads._write_meta
        def writing(directory,meta):
            active=[json.loads(p.read_text()) for p in (root/'public/.node-storage-budget').glob('*.json')]
            assert active and sum(r['bytes'] for r in active)>=len(uploads._encoded_meta(meta))
            seen.append({'actual_metadata_bytes':len(uploads._encoded_meta(meta)),'held_bytes':sum(r['bytes'] for r in active)})
            return original(directory,meta)
        with patch.object(uploads,'_write_meta',writing):
            uploads.write_chunk(prefix,made['id'],0,b'D'*262144)
            fresh=uploads.create_session(prefix,'Ref/next.bin',1,None,False)
        assert len(seen)==2 and seen[0]['held_bytes']>=262144+seen[0]['actual_metadata_bytes']
        assert (directory/'data.part').read_bytes()==b'D'*262144
        assert not list((root/'public/.node-storage-budget').glob('*.json'))
        checks.append({'check':'actual_append_and_create_hold_credit_through_metadata','observations':seen})
        for path,size in [('Ref/a.bin',1),('Ref/unicode-\u4e2d\u6587.bin',1024**3)]:
            bound=uploads.creation_metadata_peak(path,size)
            for stamp in [0.0,time.time(),-sys.float_info.max]:
                meta={'id':'0'*32,'path':path,'size':size,'sha256':'f'*64,'target_sha256':'a'*64,'overwrite':False,'created_at':stamp,'updated_at':stamp}
                assert len(uploads._encoded_meta(meta))<=bound
        checks.append({'check':'protocol_metadata_bound_covers_digests_and_finite_timestamps'})
        result['passed']=True
    finally:
        result['source_hashes']=hashes();(root/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    print('UPLOAD_RESOURCE_SERVICE_OK '+str(root)+' checks='+str(len(checks)),flush=True)


FIXTURE=r'''
import asyncio,hashlib,json,os,threading
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app import storage
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models import Case,Plan,Workspace,UploadStorageReservation,DurableEvent
from app.routers.workspaces import get_owned_case
from app.services import uploads,upload_storage,owner_storage_budget,storage_usage
root=Path(__file__).parent
entered=threading.Event();release=threading.Event();selected=None;mode=None;tasks={};observations=[]
original_chunk=uploads.write_chunk;original_meta=uploads._write_meta;original_hold=owner_storage_budget.hold_case
def writing_meta(directory,meta):
 if get_settings().storage_backend=='fs':
  claims=[json.loads(p.read_text()) for p in storage._fs_root().joinpath('.node-storage-budget').glob('*.json')]
  assert claims and sum(r['bytes'] for r in claims)>=len(uploads._encoded_meta(meta))
  observations.append({'kind':'actual_metadata_IO','claim_bytes':sum(r['bytes'] for r in claims)})
  if selected==meta['id'] and mode=='exit_chunk':os._exit(79)
 return original_meta(directory,meta)
uploads._write_meta=writing_meta
def chunk(prefix,identifier,offset,data):
 value=original_chunk(prefix,identifier,offset,data)
 if selected==identifier:
  if mode=='exit_chunk':os._exit(79)
  if mode=='pause_chunk':
   entered.set();assert release.wait(20),'Actual upload worker was not released'
 return value
uploads.write_chunk=chunk
@asynccontextmanager
async def held(db,case,*,chunk=None):
 identifier=chunk['upload_id'] if chunk else None
 if identifier:tasks[identifier]=asyncio.current_task()
 try:
  async with original_hold(db,case,chunk=chunk) as value:yield value
 finally:
  if identifier:tasks.pop(identifier,None)
owner_storage_budget.hold_case=held
@app.post('/api/v1/testing/upload-control')
async def control(body:dict,user=Depends(get_current_user)):
 global selected,mode
 if body['action']=='release':release.set()
 elif body['action']=='cancel_task':
  task=tasks.get(body['upload_id']);assert task is not None and not task.done();task.cancel()
 else:
  assert body['action'] in {'pause_chunk','exit_chunk'}
  selected=body['upload_id'];mode=body['action'];entered.clear();release.clear()
 return {'entered':entered.is_set(),'pid':os.getpid(),'actual_worker_observations':len(observations)}
@app.get('/api/v1/testing/upload-control')
async def control_state(user=Depends(get_current_user)):
 return {'entered':entered.is_set(),'pid':os.getpid(),'observations':observations}
@app.post('/api/v1/testing/upload-limits')
async def limits(body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 plan=await db.scalar(select(Plan).where(Plan.code=='free'));values=json.loads(plan.quotas)
 if 'storage_mb' in body:values['storage_mb']=body['storage_mb'];plan.quotas=json.dumps(values);await db.commit()
 if 'node_mb' in body:get_settings().node_storage_limit_mb=body['node_mb']
 return {'changed':True}
@app.post('/api/v1/testing/upload-pressure/{case_id}')
async def pressure(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db);assert get_settings().storage_backend=='fs'
 path=(storage._fs_path(case.s3_prefix,'Ref/quota-pressure.bin') if body.get('personal') else storage._fs_root()/'upload-node-pressure.bin')
 size=body['bytes'];assert type(size) is int and 0<=size<=2*1024*1024
 if size:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'P'*size)
 else:path.unlink(missing_ok=True)
 return {'actual_bytes':size}
@app.get('/api/v1/testing/upload-ledger/{case_id}')
async def ledger(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db)
 owner=await owner_storage_budget.owner_for_case(db,case)
 rows=list(await db.scalars(select(UploadStorageReservation).where(UploadStorageReservation.case_id==case.id)))
 usage=await storage_usage.measure_user(owner.id)
 events=list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case.id,DurableEvent.type==uploads.COMPLETED_EVENT)))
 return {'rows':[{'id':r.id,'user_id':r.user_id,'size':r.size,'remaining':r.remaining_bytes,'state':r.state} for r in rows],
  'bytes':usage.bytes,'complete':usage.complete,'owner_id':owner.id,'prefix':case.s3_prefix,
  'completed_upload_ids':[json.loads(e.payload_json)['result']['id'] for e in events]}
'''


def api_check(database_url=None,store=None):
    from app.services import process_identity_native as identity
    api=IsolatedServer('upload-resource-api',app='upload_resource_fixture:app',database_url=database_url)
    peer=IsolatedServer('upload-resource-peer',app='upload_resource_peer:app',database_url=database_url)
    settings=store.settings() if store else {}
    (api.root/'upload_resource_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    result={'passed':False,'model_calls':0,'mock_llm_calls':0,'database':'postgresql' if database_url else 'sqlite','storage':'s3' if store else 'fs','checks':[]}
    checks=result['checks'];births={};main_exited=False
    print('UPLOAD_RESOURCE_API '+str(api.root),flush=True)
    def call(server,method,path,**kw):
        return httpx.request(method,server.base+'/api/v1'+path,headers={'Authorization':'Bearer '+server.token},trust_env=False,timeout=40,**kw)
    def ok(response,status=200):assert response.status_code==status,(response.status_code,response.text[:800]);return response.json()
    def create(path,data):return api.call('POST',api.prefix+'/uploads',{'path':path,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    def ledger(server=api):return server.call('GET','/testing/upload-ledger/'+str(api.case_id))
    def row(server,identifier):return next(r for r in ledger(server)['rows'] if r['id']==identifier)
    try:
        with api:
            api.create_case('Actual upload resource interleavings')
            births['main']=identity.observe(api.call('GET','/testing/upload-control')['pid'])
            shared={k:v for k,v in api.env.items() if k.startswith('VIBEFLOW_')};shared.update(settings)
            (peer.root/'upload_resource_peer.py').write_text('import os\nos.environ.update('+repr(shared)+')\n'+FIXTURE,encoding='utf8');peer.token=api.token
            api.call('POST','/testing/upload-limits',{'storage_mb':64,'node_mb':64})
            with peer:
                births['peer']=identity.observe(peer.call('GET','/testing/upload-control')['pid'])
                peer.call('POST','/testing/upload-limits',{'node_mb':64})
                data=b'B'*(8*1024*1024);upload=create('Ref/stream.bin',data);path=api.prefix+'/uploads/'+upload['id']
                def too_large():
                    for _ in range(129):yield b'B'*65536
                ok(call(api,'PUT',path+'?offset=0',content=too_large()),413)
                assert api.call('GET',path)['offset']==0 and row(api,upload['id'])['remaining']==len(data)
                ok(call(api,'PUT',path+'?offset=0',content=(data[i:i+65536] for i in range(0,len(data),65536))))
                assert api.call('GET',path)['offset']==len(data)
                api.call('DELETE',path);api.call('DELETE',path)
                checks.append({'check':'actual_chunked_HTTP_oversize_refused_before_mutation_and_exact_8MiB_accepted','maximum_chunk':len(data)})
                if store is None:
                    payload=b'N'*16384;ready=create('Ref/node.bin',payload);target=api.prefix+'/uploads/'+ready['id']
                    api.call('POST','/testing/upload-limits',{'node_mb':1})
                    api.call('POST','/testing/upload-pressure/'+str(api.case_id),{'bytes':1200*1024})
                    value=ok(call(api,'POST',api.prefix+'/uploads',json={'path':'Ref/refused.bin','size':8}),409)
                    assert value['detail']['code']=='node_storage_full'
                    value=ok(call(api,'PUT',target+'?offset=0',content=payload),409);assert value['detail']['code']=='node_storage_full'
                    assert api.call('GET',target)['offset']==0
                    api.call('POST','/testing/upload-pressure/'+str(api.case_id),{'bytes':0})
                    api.call('POST','/testing/upload-limits',{'node_mb':64})
                    api.call('DELETE',target)
                    checks.append({'check':'actual_full_node_refuses_creation_and_append_no_progress'})
                    api.call('POST','/testing/upload-limits',{'storage_mb':1});state=ledger()
                    near=1024*1024-state['bytes']-1;assert near>0
                    api.call('POST',api.prefix+'/uploads',{'path':'Ref/no-meta-room.bin','size':near},expect=429)
                    small=create('Ref/quota.bin',b'Q'*4096);state=ledger();reserved=sum(r['remaining'] for r in state['rows'] if r['state']=='active')
                    amount=1024*1024-state['bytes']-reserved-1;assert amount>0
                    api.call('POST','/testing/upload-pressure/'+str(api.case_id),{'bytes':amount,'personal':True})
                    ok(call(api,'PUT',api.prefix+'/uploads/'+small['id']+'?offset=0',content=b'Q'*4096),429)
                    assert api.call('GET',api.prefix+'/uploads/'+small['id'])['offset']==0
                    api.call('POST','/testing/upload-pressure/'+str(api.case_id),{'bytes':0,'personal':True})
                    api.call('DELETE',api.prefix+'/uploads/'+small['id']);api.call('POST','/testing/upload-limits',{'storage_mb':64})
                    checks.append({'check':'personal_quota_includes_creation_and_replacement_metadata_peak'})
                left=os.urandom(32768);right=os.urandom(16384);upload=create('Ref/race.bin',left+right);target=api.prefix+'/uploads/'+upload['id']
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(call,server,'PUT',target+'?offset=0',content=left) for server in (api,peer)]
                    responses=[f.result() for f in futures]
                statuses=[r.status_code for r in responses];assert sorted(statuses)==[200,409],statuses
                assert api.call('GET',target)['offset']==len(left)
                ok(call(peer,'PUT',target+'?offset='+str(len(left)),content=right))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses=[f.result() for f in [pool.submit(call,server,'POST',target+':complete',json={}) for server in (api,peer)]]
                assert any(r.status_code==200 for r in responses) and all(r.status_code in {200,409} for r in responses)
                completed=peer.call('POST',target+':complete',{});assert completed['sha256']==hashlib.sha256(left+right).hexdigest()
                assert peer.call('GET',api.prefix+'/raw?path=Ref/race.bin',raw=True)==left+right
                assert ledger(peer)['completed_upload_ids'].count(upload['id'])==1
                peer.call('DELETE',target,expect=409)
                checks.append({'check':'actual_two_API_same_offset_and_completion_serialized_once','chunk_statuses':statuses})
                upload=create('Ref/cancel.bin',left+right);target=api.prefix+'/uploads/'+upload['id']
                api.call('POST','/testing/upload-control',{'action':'pause_chunk','upload_id':upload['id']})
                with ThreadPoolExecutor(max_workers=2) as pool:
                    writing=pool.submit(call,api,'PUT',target+'?offset=0',content=left)
                    until=time.monotonic()+15
                    while not api.call('GET','/testing/upload-control')['entered']:
                        assert not writing.done() and time.monotonic()<until;time.sleep(.03)
                    cancelling=pool.submit(call,peer,'DELETE',target)
                    time.sleep(.1)
                    if cancelling.done():assert cancelling.result().status_code==409
                    for _ in range(3):api.call('POST','/testing/upload-control',{'action':'cancel_task','upload_id':upload['id']})
                    assert not writing.done()
                    api.call('POST','/testing/upload-control',{'action':'release'})
                    cancelled_writer=writing.result();cancel_response=cancelling.result()
                assert cancelled_writer.status_code==500,cancelled_writer.status_code
                assert cancel_response.status_code in {200,409},(cancel_response.status_code,cancel_response.text[:800])
                peer.call('DELETE',target);peer.call('DELETE',target)
                assert row(peer,upload['id'])['state']=='cancelled' and row(peer,upload['id'])['remaining']==0
                peer.call('GET',api.prefix+'/raw?path=Ref/cancel.bin',raw=True,expect=404)
                checks.append({'check':'actual_worker_drain_three_task_cancellations_and_peer_cancel','writer_status':cancelled_writer.status_code,'peer_cancel_status':cancel_response.status_code})
                upload=create('Ref/crash.bin',left+right);target=api.prefix+'/uploads/'+upload['id']
                api.call('POST','/testing/upload-control',{'action':'exit_chunk','upload_id':upload['id']})
                try:response=call(api,'PUT',target+'?offset=0',content=left)
                except httpx.TransportError as error:result['actual_crash_transport']=type(error).__name__
                else:raise AssertionError('Actual abrupt API exit returned an HTTP result '+str(response.status_code))
                until=time.monotonic()+10
                while identity.probe(births['main'])['state']!='exited':assert time.monotonic()<until;time.sleep(.03)
                main_exited=True;result['actual_crash_birth_exit']=identity.probe(births['main'])
                assert peer.call('GET',target)['offset']==len(left)
                ok(call(peer,'PUT',target+'?offset='+str(len(left)),content=right))
                peer.call('POST',target+':complete',{});peer.call('POST',target+':complete',{})
                assert peer.call('GET',api.prefix+'/raw?path=Ref/crash.bin',raw=True)==left+right
                assert ledger(peer)['completed_upload_ids'].count(upload['id'])==1 and row(peer,upload['id'])['remaining']==0
                assert not list((api.root/'files/.node-storage-budget').glob('*.json'))
                checks.append({'check':'actual_API_exit79_after_payload_new_API_resumes_same_upload_and_completes_once','boundary':'before_metadata' if store is None else 'after_remote_metadata'})
                result['functional_passed']=True
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error)[:1600]};raise
    finally:
        result['births']=births;result['exit_probes']={}
        for name,birth in births.items():
            until=time.monotonic()+10
            while True:
                probe=identity.probe(birth)
                if probe['state']=='exited' or time.monotonic()>=until:break
                time.sleep(.03)
            result['exit_probes'][name]=probe
        result['passed']=bool(result.get('functional_passed') and len(births)==2 and all(p['state']=='exited' for p in result['exit_probes'].values()))
        result['peer_root']=str(peer.root);result['source_hashes']=hashes()
        (api.root/'resource-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    assert result['passed'];print('UPLOAD_RESOURCE_API_OK '+str(api.root)+' checks='+str(len(checks)),flush=True)
    return api.root


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--service-only',action='store_true');parser.add_argument('--api-only',action='store_true');parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    if not args.api_only:service()
    if args.service_only:return
    def run(url=None):
        if not args.s3:return api_check(url)
        from s3testsupport import ObjectStore
        store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed');receipt={'passed':False}
        print('UPLOAD_RESOURCE_S3 '+str(store.root),flush=True)
        try:
            with store:
                path=api_check(url,store);receipt.update(passed=True,api_root=str(path));return path
        finally:
            receipt['store_stopped']=store.proc is not None and store.proc.poll() is not None
            (store.root/'upload-resource-result.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf8')
    if not args.postgres:await asyncio.to_thread(run);return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    u=make_url(Settings(_env_file=SERVER/'.env').database_url);assert u.drivername=='postgresql+asyncpg' and u.host in {'localhost','127.0.0.1','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='upload-resource-postgres-',dir=SERVER.parents[1]/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False,'model_calls':0,'mock_llm_calls':0}
    print('UPLOAD_RESOURCE_POSTGRES '+str(root),flush=True)
    conn=await asyncpg.connect(host=u.host,port=u.port or 5432,user=u.username,password=u.password,database='postgres',timeout=10);created=False
    try:
        await conn.execute('CREATE DATABASE "'+name+'"');created=True;url=u.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head');report['api_root']=str(await asyncio.to_thread(run,url));report['passed']=True
    finally:
        try:
            if created:await conn.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await conn.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')

if __name__=='__main__':asyncio.run(main())
