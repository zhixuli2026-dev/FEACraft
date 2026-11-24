"""Real remote creation recovery with a fresh API cache and unchanged third-party objects."""
import json,tempfile
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME
from case_provision_smoke import FIXTURE,crash


def boot(settings,label):
    api=IsolatedServer(label,app='s3_provision_fixture:app')
    (api.root/'s3_provision_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    return api


def run():
    output=Path(tempfile.mkdtemp(prefix='s3-case-provision-evidence-',dir=SERVER.parents[1]/'tmp'))
    checks=[]
    with ObjectStore(executable=RUNTIME) as store:
        settings={**store.settings(),'VIBEFLOW_DATABASE_URL':'sqlite+aiosqlite:///'+(output/'test.sqlite').as_posix()}
        for mode in ['after_file','before_commit','after_commit','external','committed_external']:
            with boot(settings,'s3-provision-'+mode) as api:
                case=api.create_case('Initial '+mode)
                assert api.call('POST','/case-provisions:recover')['recovery_required']==0
                ws=api.call('GET','/workspaces')[0]['id'];(api.root/'mode').write_text(mode)
                owner=api.call('GET','/auth/me')['id']
                with httpx.Client(base_url=api.base,headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=45) as client:
                    crash(api,lambda:client.post(f'/api/v1/workspaces/{ws}/cases',json={'name':'Interrupted '+mode}))
                keys=[row['Key'] for page in store.client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket)
                      for row in page.get('Contents',[]) if row['Key'].startswith(f'workspaces/{owner}/.case-provisions/pending/')]
                assert len(keys)==1,keys
                pending=keys[0];raw=store.client.get_object(Bucket=store.bucket,Key=pending)['Body'].read();plan=json.loads(raw)
                prefix=plan['binding']['prefix'];known=next(iter(plan['files']))
                if mode in {'external','committed_external'}:
                    store.client.put_object(Bucket=store.bucket,Key=prefix+'/'+known,Body=b'EXTERNAL')
                peer_settings={**settings,'VIBEFLOW_JWT_SECRET':api.env['VIBEFLOW_JWT_SECRET']}
                with boot(peer_settings,'s3-provision-peer') as peer:
                    peer.token=api.token
                    assert not list((peer.root/'files').rglob('pending/*.json'))
                    result=peer.call('POST','/case-provisions:recover')
                    row=result['results'][0]
                    expected='recovery_required' if mode=='external' else 'committed' if mode in {'after_commit','committed_external'} else 'rolled_back'
                    assert row['status']==expected,result
                    current=store.client.list_objects_v2(Bucket=store.bucket,Prefix=prefix+'/').get('Contents',[])
                    if expected=='rolled_back':assert not current
                    else:assert current
                    if mode in {'external','committed_external'}:
                        assert store.client.get_object(Bucket=store.bucket,Key=prefix+'/'+known)['Body'].read()==b'EXTERNAL'
                    checks.append({'mode':mode,'api':str(api.root),'peer':str(peer.root),'status':expected})
            print('PASS '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'storage':'real SeaweedFS 4.52','database':'sqlite'}
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'count':len(checks),'evidence':str(output)}),flush=True)


if __name__=='__main__':run()
