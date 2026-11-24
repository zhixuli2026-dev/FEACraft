"""Real chunk credit, constrained quota, lost S3 responses and API death."""
import argparse
import asyncio
import hashlib
import json
import sys
import httpx
from testsupport import IsolatedServer,SERVER
from publication_read_capacity_checks import FIXTURE
from mixed_read_capacity_checks import postgres

OBSERVE = '''
import os
from botocore.client import BaseClient
from botocore.exceptions import ReadTimeoutError
from app.models import UploadChunkCredit,UploadStorageReservation
from app.services import s3_uploads,owner_storage_budget
original_quotas=quota.quotas_for
async def bounded(db,user):
 value=await original_quotas(db,user)
 return {**value,'storage_mb':int((root/'limit-mb').read_text())} if (root/'limit-mb').exists() else value
quota.quotas_for=bounded
def mode():return (root/'mode').read_text() if (root/'mode').exists() else ''
def clear():(root/'mode').write_text('')
original_call=BaseClient._make_api_call
def request(self,operation,parameters):
 payload=operation=='PutObject' and '/chunks/' in parameters.get('Key','') and parameters['Key'].endswith('/data') and parameters.get('ContentLength',0)>0
 if payload:
  pool=owner_storage_budget.current()
  assert pool is not None and pool.chunk_credit is not None
  assert pool.chunk_credit['key']==parameters['Key']
  if mode()=='before-data-loss':
   clear();raise ReadTimeoutError(endpoint_url='isolated-before-upload-payload')
 result=original_call(self,operation,parameters)
 if payload:
  with (root/'payload-puts.jsonl').open('a') as f:f.write(json.dumps({'key':parameters['Key'],'size':parameters['ContentLength']})+'\\n')
  if mode()=='after-data-loss':
   clear();raise ReadTimeoutError(endpoint_url='isolated-after-upload-payload')
  if mode()=='crash-after-data':
   (root/'crashed').write_text('actual payload PUT acknowledged; API exits before chunk metadata')
   os._exit(79)
 return result
BaseClient._make_api_call=request
original_meta=s3_uploads._save_meta
def save_meta(prefix,meta,etag):
 if meta['offset'] and mode()=='before-meta':
  clear();raise OSError('intentional failure before upload metadata')
 value=original_meta(prefix,meta,etag)
 if meta['offset'] and mode()=='after-meta':
  clear();raise OSError('intentional failure after upload metadata')
 return value
s3_uploads._save_meta=save_meta
@app.get('/api/v1/fixture/chunk-ledger')
async def ledger(user=Depends(get_current_user)):
 async with SessionLocal() as db:
  pools=list(await db.scalars(select(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user.id)))
  uploads=list(await db.scalars(select(UploadStorageReservation).where(UploadStorageReservation.user_id==user.id)))
  credits=list(await db.scalars(select(UploadChunkCredit).join(OwnerStorageReservation,UploadChunkCredit.id==OwnerStorageReservation.id).where(OwnerStorageReservation.user_id==user.id).order_by(UploadChunkCredit.created_at,UploadChunkCredit.id)))
  return {'pools':[{'id':r.id,'state':r.state,'remaining':r.remaining_bytes,'receipt':json.loads(r.receipt_json)} for r in pools],
          'uploads':[{'id':r.id,'size':r.size,'remaining':r.remaining_bytes,'state':r.state} for r in uploads],
          'credits':[{'id':r.id,'upload_id':r.upload_id,'offset':r.offset,'size':r.size,'amount':r.amount,'state':r.state,'receipt':json.loads(r.receipt_json)} for r in credits]}
'''


def verify(store,database_url=None,group='chunk'):
    api=IsolatedServer('upload-chunk-credit',app='chunk_fixture:app',database_url=database_url)
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    (api.root/'chunk_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+OBSERVE,encoding='utf8')
    result={'passed':False,'database':'postgresql' if database_url else 'sqlite','llm_calls':0,'checks':[]}
    def save():(api.root/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    def check(name,**fields):
        result['checks'].append({'name':name,**fields});save();print('CHUNK_CREDIT_PASS '+name,flush=True)
    def state():return api.call('GET','/fixture/chunk-ledger')
    def settled():
        value=state()
        assert all(r['state']=='settled' for r in value['pools']),value
        assert all(r['state']=='settled' for r in value['credits']),value
        return value
    def send(target,data,offset=0,expected=200):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
            response=client.put(target+'?offset='+str(offset),content=data)
            assert response.status_code==expected,(response.status_code,response.text[:1500])
            return response.json() if expected==200 else response.content
    def start(name,size):
        api.create_case(name)
        return api.call('POST',api.prefix+'/uploads',{'path':'Ref/chunk.bin','size':size})
    print('CHUNK_CREDIT_EVIDENCE '+str(api.root),flush=True)
    try:
        if database_url is None:
            from workflow_migration_smoke import alembic
            alembic('sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix(),api.root,'upgrade','head')
        with api:
            api.create_case('Constrained chunk credit')
            before=api.call('GET','/fixture/credit?measure=true')['bytes']
            mb=before//(1024*1024)+1
            (api.root/'limit-mb').write_text(str(mb))
            size=mb*1024*1024-before-150000
            payload=b'P'*size
            created=api.call('POST',api.prefix+'/uploads',{'path':'Ref/chunk.bin','size':size})
            target=api.prefix+'/uploads/'+created['id']
            assert send(target,payload)['offset']==size
            value=settled();credit=value['credits'][-1]
            assert credit['amount']==size and value['uploads'][-1]['remaining']==0,value
            physical=api.call('GET','/fixture/credit?measure=true')['bytes']
            assert physical<=mb*1024*1024,(physical,mb)
            check('reserved_payload_transfers_with_only_metadata_headroom',size=size,physical=physical,limit=mb*1024*1024)
            (api.root/'limit-mb').unlink()
            api.call('POST',target+':complete',{})
            assert api.call('GET',api.prefix+'/raw?path=Ref/chunk.bin',raw=True)==payload
            settled();check('transferred_upload_completes_with_actual_bytes')
            for fault in ('before-meta','after-meta','after-data-loss','before-data-loss','crash-after-data'):
                data=(fault.encode()+b'!')*4096
                created=start('Chunk '+fault,len(data));target=api.prefix+'/uploads/'+created['id']
                email=api.call('GET','/auth/me')['email']
                (api.root/'mode').write_text(fault)
                if fault=='crash-after-data':
                    try:send(target,data)
                    except httpx.TransportError:pass
                    else:raise AssertionError('API death did not break its HTTP request')
                    assert api.proc.wait(timeout=10)==79
                    (api.root/'crash-api.log').write_text((api.root/'api.log').read_text(encoding='utf8'),encoding='utf8')
                    api.log.close();(api.root/'mode').write_text('')
                    api.__enter__()
                    api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
                    crash_state=state()
                    assert any(r['state']=='active' and r['receipt']=={} for r in crash_state['pools']),crash_state
                    assert any(r['state']=='active' for r in crash_state['credits']),crash_state
                    check('api_death_retains_durable_pool_and_transfer')
                else:send(target,data,expected=500)
                failed=state()
                if fault in {'after-data-loss','before-data-loss'}:
                    pending=[r for r in failed['pools'] if r['state']=='active']
                    assert pending and any(r['remaining']==len(data) and r['receipt'].get('pending') for r in pending),failed
                    check(fault+'_unknown_request_capacity_retained')
                if fault=='after-meta':
                    assert failed['uploads'][-1]['remaining']==0,failed
                    send(target,data,expected=409)
                    assert api.call('GET',target)['offset']==len(data)
                else:
                    if fault in {'before-meta','after-data-loss'}:
                        assert failed['uploads'][-1]['remaining']==0,failed
                        send(target,b'X'*len(data),expected=409)
                    assert send(target,data)['offset']==len(data)
                    assert api.call('GET',target)['offset']==len(data)
                final=settled()
                if fault in {'before-meta','after-data-loss','crash-after-data'}:
                    assert final['credits'][-1]['amount']==0,final
                api.call('POST',target+':complete',{})
                assert api.call('GET',api.prefix+'/raw?path=Ref/chunk.bin',raw=True)==data
                check(fault+'_recovery_actual_bytes_and_settled_credits',credits=len(final['credits']))
            puts=[json.loads(line) for line in (api.root/'payload-puts.jsonl').read_text().splitlines()]
            assert len({row['key'] for row in puts})==len(puts)==6,puts
            check('one_actual_payload_put_per_immutable_chunk',count=len(puts))
            result['passed']=True;save()
        # This populated temporary database must refuse destructive downgrade.
        from workflow_migration_smoke import alembic
        try:alembic(api.env['VIBEFLOW_DATABASE_URL'],api.root,'downgrade','d1b6d82957fe')
        except AssertionError:
            assert 'Downgrade would discard upload payload transfer' in (api.root/'alembic.log').read_text(encoding='utf8')
        else:raise AssertionError('Populated chunk-credit migration downgraded')
        check('migration_refuses_to_discard_transfer_history')
    except BaseException as error:
        result['passed']=False;result['failure']={'type':type(error).__name__,'detail':str(error)[:2000]};save()
        print('CHUNK_CREDIT_FAILED '+json.dumps(result['failure']),flush=True);return 1
    print('CHUNK_CREDIT_OK '+str(api.root/'checks.json'),flush=True);return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',choices=('sqlite','postgresql'),default='sqlite')
    args=parser.parse_args()
    from s3testsupport import ObjectStore
    with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
        code=asyncio.run(postgres(store,'chunk',verify)) if args.database=='postgresql' else verify(store)
    sys.exit(code)
