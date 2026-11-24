"""Chunk completion survives SQL uncertainty, crashes and lost responses."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

import httpx

from file_transactions_smoke import FIXTURE as JOURNAL, interrupt, peer
from testsupport import IsolatedServer, SERVER

FIXTURE=JOURNAL+'''
from app.services import uploads,durable_events
original_prepare=uploads.prepare_completion
def preparing(prefix,upload_id):
    if mode()=='capacity':
        journal.MAX_BYTES=1024*1024
        journal.MAX_LOCAL_TRANSACTION_BYTES=2*journal.MAX_BYTES
    result=original_prepare(prefix,upload_id)
    if mode()=='stage_race':
        (uploads._session_dir(prefix,upload_id)/'data.part').write_bytes(b'EXTERNAL')
    return result
uploads.prepare_completion=preparing
capture=journal._capture
def capturing(path,destination=None,**kwargs):
    if mode()=='size_race' and path.name=='data.part':path.write_bytes(b'SHORT')
    return capture(path,destination,**kwargs)
journal._capture=capturing
original_cleanup=uploads.cleanup_completed
def cleaning(*args):
    if mode()=='cleanup_crash':hold()
    if mode()=='cleanup_failure':raise OSError('fixture cleanup unavailable')
    return original_cleanup(*args)
uploads.cleanup_completed=cleaning
append=durable_events.append_event
async def appending(*args,**kwargs):
    if mode()=='receipt_failure' and args[2]==uploads.COMPLETED_EVENT:
        raise OSError('fixture completion receipt failure')
    return await append(*args,**kwargs)
durable_events.append_event=appending
commit_with_barrier=AsyncSession.commit
async def uncertain(self):
    result=await commit_with_barrier(self)
    if self.info.get('local_file_transactions') and mode()=='response_lost':
        raise TimeoutError('fixture SQL commit response lost')
    return result
AsyncSession.commit=uncertain
# File content must use streaming capture; reading the staged object all at once
# would allow small tests to conceal a 1 GiB resident allocation in production.
read_bytes=Path.read_bytes
def reading_bytes(path):
    if path.name=='data.part':raise AssertionError('Staging must be streamed')
    return read_bytes(path)
Path.read_bytes=reading_bytes
'''

MODES=('before_apply','after_apply','before_commit','after_commit','cleanup_crash',
       'cleanup_failure','receipt_failure','response_lost','external','stage_corrupt','stage_race','normal',
       'copy','capacity','cleanup_foreign','size_race','missing_part')
DATA=bytes(range(256))*4096
DIGEST=hashlib.sha256(DATA).hexdigest()


def verify(database_url=None,modes=None):
    output=Path(tempfile.mkdtemp(prefix='upload-transactions-evidence-',dir=SERVER.parents[1]/'tmp'))
    checks=[]
    for mode in modes or MODES:
        api=IsolatedServer('upload-transaction-'+mode,app='upload_fixture:app',database_url=database_url)
        (api.root/'upload_fixture.py').write_text(FIXTURE,encoding='utf8')
        with api:
            case=api.create_case('Upload '+mode)
            target='Ref/chunk-target.bin'
            api.put(target,'x'*len(DATA) if mode=='capacity' else 'old upload')
            session=api.call('POST',api.prefix+'/uploads',{'path':target,'size':len(DATA),'sha256':None if mode=='size_race' else DIGEST,'overwrite':True})
            endpoint=api.prefix+'/uploads/'+session['id']
            root=next((api.root/'files/workspaces').glob('*/cases/'+str(case['id'])))
            stage=root/'.vibeflow/uploads'/session['id']
            headers={'Authorization':'Bearer '+api.token}
            with httpx.Client(base_url=api.base+'/api/v1',headers=headers,trust_env=False,timeout=40) as client:
                sent=client.put(endpoint+'?offset=0',content=DATA)
                assert sent.status_code==200,sent.text
            if mode=='copy':
                copied=api.call('POST',api.prefix+':copy',{'name':'No upload sessions in copies'})
                copied_root=next((api.root/'files/workspaces').glob('*/cases/'+str(copied['id'])))
                assert not (copied_root/'.vibeflow/uploads').exists()
                assert (stage/'data.part').read_bytes()==DATA
                api.call('POST',f"/cases/{copied['id']}/uploads/{session['id']}:complete",expect=404)
                assert api.call('GET',f"/cases/{copied['id']}/raw?path="+target,raw=True)==b'old upload'
            control='after_apply' if mode in {'external','stage_corrupt'} else mode
            if mode=='cleanup_foreign':control='cleanup_crash'
            (api.root/'mode').write_text(control)
            if mode in {'before_apply','after_apply','before_commit','after_commit','cleanup_crash','external','stage_corrupt','cleanup_foreign'}:
                interrupt(api,lambda:api.call('POST',endpoint+':complete'))
                assert (stage/'data.part').read_bytes()==DATA
                if mode=='external':(root/target).write_bytes(b'EXTERNAL')
                if mode=='stage_corrupt':(stage/'data.part').write_bytes(b'CORRUPT')
                if mode=='cleanup_foreign':(stage/'data.part').write_bytes(b'FOREIGN STAGING')
            elif mode in {'receipt_failure','response_lost'}:
                api.call('POST',endpoint+':complete',expect=500,raw=True)
                assert (stage/'data.part').read_bytes()==DATA
            elif mode=='missing_part':
                (stage/'data.part').unlink()
                api.call('POST',endpoint+':complete',expect=404)
                assert (root/target).read_bytes()==b'old upload'
            elif mode in {'stage_race','size_race'}:
                api.call('POST',endpoint+':complete',expect=409)
                assert (root/target).read_bytes()==b'old upload'
            else:
                first=api.call('POST',endpoint+':complete')
                assert first['sha256']==DIGEST and first['status']=='completed'
                if mode=='cleanup_failure':assert first['cleanup_pending'] and stage.exists()
                else:assert not stage.exists()
            with peer(api) as recovered:
                if mode=='external':
                    error=recovered.call('POST',endpoint+':complete',expect=409)
                    assert error['detail']['code']=='file_recovery_required'
                    assert (root/target).read_bytes()==b'EXTERNAL' and stage.exists()
                elif mode in {'stage_corrupt','stage_race','size_race','missing_part'}:
                    assert recovered.call('GET',api.prefix+'/raw?path='+target,raw=True)==b'old upload'
                    recovered.call('POST',endpoint+':complete',expect=404 if mode=='missing_part' else 409)
                    assert stage.exists()
                    assert recovered.call('GET',api.prefix+'/file-transactions')['status']=='idle'
                else:
                    committed=mode in {'after_commit','cleanup_crash','cleanup_failure','response_lost','normal','copy','capacity','cleanup_foreign'}
                    current=recovered.call('GET',api.prefix+'/raw?path='+target,raw=True)
                    assert current==(DATA if committed else b'old upload'),mode
                    status=recovered.call('GET',endpoint)
                    assert (status.get('status')=='completed')==committed,(mode,status)
                    completed=recovered.call('POST',endpoint+':complete')
                    assert completed['sha256']==DIGEST and completed['status']=='completed'
                    if mode=='cleanup_foreign':
                        assert completed['cleanup_pending']
                        assert (stage/'data.part').read_bytes()==b'FOREIGN STAGING'
                    else:assert not stage.exists()
                    assert recovered.call('GET',api.prefix+'/raw?path='+target,raw=True)==DATA
                    recovered.call('PUT',api.prefix+'/file',{'path':target,'content':'LATER EDIT'})
                    retry=recovered.call('POST',endpoint+':complete')
                    assert retry==completed,(retry,completed)
                    assert recovered.call('GET',api.prefix+'/raw?path='+target,raw=True)==b'LATER EDIT'
                    assert recovered.call('GET',endpoint)['sha256']==DIGEST
                    recovered.call('DELETE',endpoint,expect=409)
                    with httpx.Client(base_url=recovered.base+'/api/v1',headers=headers,trust_env=False) as client:
                        assert client.put(endpoint+'?offset=0',content=b'x').status_code==409
                    protected=recovered.call('PUT',api.prefix+'/file',{'path':'.vibeflow/uploads/foreign/meta.json','content':'{}'},expect=409)
                    assert protected['detail']['code']=='input_sealed'
                    assert recovered.call('GET',api.prefix+'/file-transactions')['status']=='idle'
            checks.append({'mode':mode,'api':str(api.root),'peer':str(recovered.root)})
            (output/'checks.json').write_text(json.dumps({'count':len(checks),'checks':checks,'complete':False},indent=2),encoding='utf8')
            print('PASS '+mode,flush=True)
    result={'count':len(checks),'checks':checks,'complete':True,'database':'postgresql' if database_url else 'sqlite'}
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'status':'passed','count':len(checks),'evidence':str(output)}),flush=True)
    return output,result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--modes',nargs='+')
    args=parser.parse_args()
    if not args.postgres:
        await asyncio.to_thread(verify,None,args.modes)
        return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    url=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
    name='tmp_vf_uploadjournal_'+uuid.uuid4().hex
    admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
    created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        temporary=url.set(database=name).render_as_string(hide_password=False)
        logs=Path(tempfile.mkdtemp(prefix='upload-migration-',dir=SERVER.parents[1]/'tmp'))
        await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
        output,result=await asyncio.to_thread(verify,temporary,args.modes)
    finally:
        if created:
            assert await admin.fetchval('SELECT current_database()')=='postgres'
            await admin.execute('DROP DATABASE "'+name+'"')
        await admin.close()
    result['temporary_database_removed']=True
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
