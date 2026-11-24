"""Real completed/cancelled upload keys reject late conditional payload PUTs."""
import argparse,asyncio,json,sys
import httpx
from botocore.exceptions import ClientError
from testsupport import IsolatedServer,SERVER
from publication_read_capacity_checks import FIXTURE
from upload_chunk_credit_checks import OBSERVE
from mixed_read_capacity_checks import postgres
from workflow_migration_smoke import alembic


def verify(store,database_url=None,group='fence'):
    api=IsolatedServer('upload-chunk-fence',app='fence_fixture:app',database_url=database_url)
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    (api.root/'fence_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+OBSERVE,encoding='utf8')
    result={'passed':False,'database':'postgresql' if database_url else 'sqlite','llm_calls':0,'checks':[]}
    def save():(api.root/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    def check(name,**fields):
        result['checks'].append({'name':name,**fields});save();print('CHUNK_FENCE_PASS '+name,flush=True)
    print('CHUNK_FENCE_EVIDENCE '+str(api.root),flush=True)
    try:
        if database_url is None:
            alembic('sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix(),api.root,'upgrade','head')
        with api:
            for mode in ('complete','cancel','cancel-missing-payload'):
                api.create_case('Permanent chunk '+mode)
                data=b'ACTUAL UPLOAD DATA'*1024
                upload=api.call('POST',api.prefix+'/uploads',{'path':'Ref/data.bin','size':len(data)})
                target=api.prefix+'/uploads/'+upload['id']
                suffix='/.vibeflow/uploads/'+upload['id']+'/meta.json'
                keys=[r['Key'] for p in store.client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket) for r in p.get('Contents',[]) if r['Key'].endswith(suffix)]
                assert len(keys)==1,keys
                key=keys[0].removesuffix('/meta.json')+'/chunks/0000000000/data'
                if mode=='cancel-missing-payload':(api.root/'mode').write_text('before-data-loss')
                with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
                    response=client.put(target+'?offset=0',content=data)
                    assert response.status_code==(500 if mode=='cancel-missing-payload' else 200),response.text
                if mode=='complete':
                    api.call('POST',target+':complete',{})
                    assert api.call('GET',api.prefix+'/raw?path=Ref/data.bin',raw=True)==data
                else:
                    api.call('DELETE',target);api.call('GET',target,expect=404)
                    api.call('DELETE',target)
                observed=store.raw_client.get_object(Bucket=store.bucket,Key=key)
                try:assert observed['ContentLength']==0 and observed['Body'].read()==b''
                finally:observed['Body'].close()
                try:store.raw_client.put_object(Bucket=store.bucket,Key=key,Body=data,IfNoneMatch='*')
                except ClientError as error:assert error.response['ResponseMetadata']['HTTPStatusCode']==412
                else:raise AssertionError('Late upload payload recreated a closed key')
                ledger=api.call('GET','/fixture/chunk-ledger')
                assert all(r['state']=='settled' for r in ledger['pools']),ledger
                assert all(r['state']=='settled' for r in ledger['credits']),ledger
                check(mode+'_permanent_zero_head_rejects_late_put_and_settles')
        try:alembic(api.env['VIBEFLOW_DATABASE_URL'],api.root,'downgrade','d1b6d82957fe')
        except AssertionError:
            assert 'Downgrade would discard upload payload transfer' in (api.root/'alembic.log').read_text(encoding='utf8')
        else:raise AssertionError('Populated chunk-credit history was downgraded')
        check('actual_migration_and_populated_downgrade_guard')
        result['passed']=True;save()
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error)[:1800]};save()
        print('CHUNK_FENCE_FAILED '+json.dumps(result['failure']),flush=True);return 1
    print('CHUNK_FENCE_OK '+str(api.root/'checks.json'),flush=True);return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',choices=('sqlite','postgresql'),default='sqlite')
    args=parser.parse_args()
    from s3testsupport import ObjectStore
    with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
        code=asyncio.run(postgres(store,'fence',verify)) if args.database=='postgresql' else verify(store)
    sys.exit(code)
