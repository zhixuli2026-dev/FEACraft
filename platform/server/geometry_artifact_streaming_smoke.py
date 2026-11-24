"""Actual historical geometry artifact downloads against FS and authenticated SeaweedFS."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import time
import uuid

import httpx

from latex_artifact_streaming_smoke import expected_bytes, response_hash, save_archive
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


STL = 'A-geo/exports/model.stl'
SIDE = 'A-geo/validation.json'
SIZE = 40 * 1024 * 1024
RUNTIME = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'

FIXTURE = r'''
import asyncio
from contextlib import closing
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time
import tracemalloc

from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import ArtifactPublication,ArtifactPublicationHead,Job,JobAttempt
from app.routers import geometry
from app.routers.workspaces import get_owned_case
from app.services import artifact_publication,attempt_workspace,file_read
from app.services import node_storage_budget,process_identity_native,s3_live_store

root=Path(__file__).parent
stl='A-geo/exports/model.stl'
side='A-geo/validation.json'
probe=ContextVar('geometry_download_measurement',default=None)
serve=geometry._geometry_artifact_response
original_chunks=file_read.FileRead.chunks
original_get=s3_live_store.Client.get_object

def observed_chunks(self,*args,**kwargs):
    state=probe.get()
    key=self.location['Key'] if self.remote else str(self.path).replace('\\','/')
    if state is None or not key.endswith('/files/'+stl):
        yield from original_chunks(self,*args,**kwargs)
        return
    state['body_streams']+=1
    with closing(original_chunks(self,*args,**kwargs)) as chunks:
        for block in chunks:
            state['body_bytes']+=len(block)
            state['max_chunk']=max(state['max_chunk'],len(block))
            if state['body_bytes']==len(block) and (root/'pause-verification').exists():
                (root/'first-file-block').touch()
                until=time.monotonic()+15
                while not (root/'release-verification').exists():
                    if time.monotonic()>=until:
                        raise TimeoutError('Fixture did not release the verified reader')
                    time.sleep(.02)
            yield block

def observed_get(self,**args):
    state=probe.get()
    if state is not None and args['Key'].endswith('/files/'+stl):
        state['s3_gets'].append({'range':args.get('Range'),'if_match':args.get('IfMatch')})
    return original_get(self,**args)

def measured(prefix,row,path,ranges,version,download):
    state={'publication_id':row.id,'head':download is None,'range':ranges,'body_streams':0,
           'body_bytes':0,'max_chunk':0,'s3_gets':[]}
    token=probe.set(state)
    tracemalloc.start()
    try:
        response=serve(prefix,row,path,ranges,version,download)
        state['status']=response.status_code
        return response
    finally:
        state.update(peak=tracemalloc.get_traced_memory()[1],
                     response_spool_reserved=download._reserved if download is not None else 0)
        tracemalloc.stop()
        probe.reset(token)
        with (root/'geometry-download-trace.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps(state)+'\n')

file_read.FileRead.chunks=observed_chunks
s3_live_store.Client.get_object=observed_get
geometry._geometry_artifact_response=measured

@app.get('/fixture/geometry/{case_id}/binding')
async def binding(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    return {'prefix':case.s3_prefix,'process':process_identity_native.observe(os.getpid())}

@app.post('/fixture/geometry/{case_id}/publish')
async def publish(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=True)
    assert body['source'] in {'history','current'}
    root_source=root/body['source']
    (root_source/side).write_text('{"summary":{"valid":true}}\n',encoding='utf-8')
    records=attempt_workspace.inventory(root_source,[stl,side])
    job=Job(case_id=case.id,type='geometry',command='geometry download fixture',status='succeeded',exit_code=0)
    db.add(job)
    await db.flush()
    attempt=JobAttempt(job_id=job.id,number=1,phase='succeeded',exit_code=0)
    db.add(attempt)
    await db.flush()
    descriptor={'handler':'geometry.v1','mode':'export','source':{}}
    row=await artifact_publication.reserve(db,case,attempt,kind='geometry',descriptor=descriptor)
    head=await db.get(ArtifactPublicationHead,(case.id,'geometry'))
    if head.active_publication_id:
        previous=await db.get(ArtifactPublication,head.active_publication_id)
        previous.status='superseded'
    row.manifest_sha256=await asyncio.to_thread(artifact_publication.prepare_bundle,
        case.s3_prefix,row,root_source,records,descriptor)
    row.status='committed'
    head.active_publication_id=row.id
    await db.commit()
    return {'id':row.id,'manifest_sha256':row.manifest_sha256,'archive':row.immutable_path,
            'file':{key:records[stl][key] for key in ('size','sha256')}}

@app.get('/fixture/geometry:capacity')
async def capacity(user=Depends(get_current_user)):
    return {'reserved_bytes':(await asyncio.to_thread(node_storage_budget.observe))['reserved_bytes']}

@app.post('/fixture/geometry:stop')
async def stop(body:dict,user=Depends(get_current_user)):
    assert body['pid']==os.getpid()
    os._exit(79)

class StopAtFirstBody:
    def __init__(self,app):
        self.app=app

    async def __call__(self,scope,receive,send):
        controlled=scope.get('path','').endswith('/geometry/artifact') and any(
            name==b'x-fixture-stop-after-first-chunk' for name,_ in scope.get('headers',[]))
        sent=False
        async def hold(message):
            nonlocal sent
            await send(message)
            if controlled and not sent and message['type']=='http.response.body' and message.get('body'):
                sent=True
                (root/'first-http-block').touch()
                until=time.monotonic()+12
                while not (root/'release-http').exists():
                    if time.monotonic()>=until:
                        raise TimeoutError('Fixture did not release the first HTTP block')
                    await asyncio.sleep(.02)
        await self.app(scope,receive,hold)

app=StopAtFirstBody(app)
'''


def make_stl(root, directory, facets):
    target=root/directory/STL
    target.parent.mkdir(parents=True,exist_ok=True)
    triangle=struct.pack('<12fH',0,0,1,0,0,0,1,0,0,0,1,0,0)
    with target.open('xb') as body:
        body.write(b'Isolated binary STL archive fixture'.ljust(80,b'\0'))
        body.write(struct.pack('<I',facets))
        group,remainder=divmod(facets,8192)
        for _ in range(group):
            body.write(triangle*8192)
        body.write(triangle*remainder)
    assert target.stat().st_size==84+len(triangle)*facets
    assert struct.unpack('<I',expected_bytes(target,80,84))[0]==facets
    return target


def wait_for(path):
    deadline=time.monotonic()+8
    while not path.exists():
        if time.monotonic()>=deadline:
            raise TimeoutError('Actual download did not reach '+path.name)
        time.sleep(.02)


def capacity(api):
    return api.call('GET','/fixture/geometry:capacity',api_prefix=False)['reserved_bytes']


def stop_api(api,identity):
    with httpx.Client(trust_env=False,timeout=10) as client:
        try:
            client.post(api.base+'/fixture/geometry:stop',json={'pid':identity['pid']},
                        headers={'Authorization':'Bearer '+api.token})
        except httpx.TransportError:
            pass
        else:
            raise AssertionError('Fixture did not stop its API')
    api.proc.wait(timeout=15)


def exercise(api,store,report):
    case=api.create_case('Owned historical geometry export')
    route=f'/fixture/geometry/{case["id"]}'
    binding=api.call('GET',route+'/binding',api_prefix=False)
    report['api_process']=binding['process']
    old_file=make_stl(api.root,'history',(SIZE-84+49)//50)
    make_stl(api.root,'current',10)
    headers={'Authorization':'Bearer '+api.token}
    with httpx.Client(trust_env=False,timeout=120) as client:
        def publish(source):
            result=client.post(api.base+route+'/publish',headers=headers,json={'source':source})
            assert result.status_code==200,result.text
            return result.json()
        old,new=publish('history'),publish('current')
        versions=api.call('GET',api.prefix+'/geometry/publications')
        assert {item['id']:item['current'] for item in versions}=={old['id']:False,new['id']:True}
        endpoint=api.base+'/api/v1'+api.prefix+'/geometry/artifact'
        params={'publication_id':old['id'],'path':STL}
        version=hashlib.sha256(json.dumps([old['id'],old['manifest_sha256'],STL,old['file']['sha256']],
            separators=(',',':')).encode()).hexdigest()
        head=client.head(endpoint,params=params,headers=headers)
        assert head.status_code==200 and head.content==b'' and head.headers['X-Vibeflow-File-Version']==version
        assert int(head.headers['Content-Length'])==old['file']['size'] and head.headers['Accept-Ranges']=='bytes'
        assert 'Content-Disposition' not in head.headers
        ranged={**headers,'X-Vibeflow-File-Version':version}
        assert response_hash(client,endpoint,params,ranged)==old['file']
        start=SIZE-170
        single=client.get(endpoint,params=params,headers={**ranged,'Range':f'bytes={start}-{start+119}'})
        assert single.status_code==206 and single.content==expected_bytes(old_file,start,start+120)
        assert single.headers['Content-Range']==f'bytes {start}-{start+119}/{old["file"]["size"]}'
        multi=client.get(endpoint,params=params,headers={**ranged,'Range':'bytes=100-115,0-7'})
        assert multi.status_code==206 and multi.headers['Content-Type'].startswith('multipart/byteranges;')
        boundary=multi.headers['Content-Type'].split('boundary=',1)[1].encode()
        parts=multi.content.split(b'--'+boundary)
        assert parts[1].endswith(expected_bytes(old_file,100,116)+b'\r\n')
        assert parts[2].endswith(expected_bytes(old_file,0,8)+b'\r\n')
        invalid=client.get(endpoint,params=params,headers={**ranged,'Range':'bytes=99999999-100000000'})
        assert invalid.status_code==416 and invalid.headers['Content-Range']==f'bytes */{old["file"]["size"]}'
        latest=client.head(endpoint,params={'path':STL,'publication_id':new['id']},headers=headers)
        assert latest.status_code==200 and int(latest.headers['Content-Length'])==new['file']['size']
        bad=client.get(endpoint,params=params,headers={**headers,'X-Vibeflow-File-Version':'0'*64})
        assert bad.status_code==409 and bad.json()['detail']['code']=='file_changed_during_read'
        assert client.get(endpoint,params={**params,'path':SIDE},headers=headers).status_code==404
        outsider=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other reader',
            'password':'isolated-test-123'})
        assert client.get(endpoint,params=params,headers={'Authorization':'Bearer '+outsider['token']}).status_code==404

        pause=api.root/'pause-verification'
        pause.touch()
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending=worker.submit(client.get,endpoint,params=params,headers={**ranged,'Range':'bytes=0-7'})
            try:
                wait_for(api.root/'first-file-block')
                api.put('Ref/parallel-save.txt','same case write during real download verification')
                assert not pending.done()
            finally:
                pause.unlink(missing_ok=True)
                (api.root/'release-verification').touch()
            read=pending.result(timeout=35)
            assert read.status_code==206 and read.content==expected_bytes(old_file,0,8)

        corrupted=api.root/'same-length-stl-replacement.bin'
        shutil.copyfile(old_file,corrupted)
        with corrupted.open('r+b') as body:
            body.seek(SIZE-16384)
            value=body.read(1)
            body.seek(-1,1)
            body.write(bytes([value[0]^0xff]))
        archive=old['archive']+'/files/'+STL
        try:
            save_archive(api,store,binding['prefix'],archive,corrupted)
            for offered in ({**ranged,'Range':'bytes=0-31'},ranged):
                refused=client.get(endpoint,params=params,headers=offered)
                assert refused.status_code==409 and refused.json()['detail']['code']=='evidence_invalid'
            assert capacity(api)==0
        finally:
            save_archive(api,store,binding['prefix'],archive,old_file)
            corrupted.unlink(missing_ok=True)
        try:
            with client.stream('GET',endpoint,params=params,headers={**ranged,'X-Fixture-Stop-After-First-Chunk':'1'}) as pending:
                assert pending.status_code==200
                assert next(pending.iter_bytes(65536))==expected_bytes(old_file,0,65536)
                wait_for(api.root/'first-http-block')
                held=capacity(api)
                assert held>=old['file']['size']
        finally:
            (api.root/'release-http').touch()
        deadline=time.monotonic()+10
        while capacity(api)!=0:
            if time.monotonic()>=deadline:
                raise TimeoutError('Disconnected geometry download kept node capacity')
            time.sleep(.05)
        restored=client.head(endpoint,params=params,headers=ranged)
        assert restored.status_code==200 and restored.headers['X-Vibeflow-File-Version']==version

    traces=[json.loads(line) for line in (api.root/'geometry-download-trace.jsonl').read_text(encoding='utf-8').splitlines()]
    successful=[row for row in traces if row['publication_id']==old['id'] and row.get('status') in {200,206,416}]
    assert len(successful)==8,successful
    for trace in successful:
        assert trace['body_streams']==1 and trace['body_bytes']==old['file']['size'],trace
        assert trace['max_chunk']<=65536 and trace['peak']<SIZE//4,trace
        if store:
            assert len(trace['s3_gets'])==1 and trace['s3_gets'][0]['range'] is None and trace['s3_gets'][0]['if_match']
    assert all(row['response_spool_reserved']==0 for row in successful if row['status']==206)
    if store:
        assert not (api.root/'files'/binding['prefix']/old['archive']).exists()
    report.update(previous=old,current=new,version=version,
        checks=['historical_and_current_HEAD','full_body_sha256','single_and_ordered_multipart_ranges','range_416',
                'version_409','restricted_file_and_cross_owner_404','scope_released_during_real_verification',
                'same_size_corruption_outside_range_rejected','failed_and_disconnected_reservations_released'],
        reserved_during_disconnect=held,successful_traces=successful)


def run(backend):
    from app.services import process_identity_native
    api=IsolatedServer('geometry-artifact-'+backend,app='geometry_artifact_fixture:app')
    context=ObjectStore(executable=RUNTIME,volume_max=16,authenticated=True) if backend=='s3' else nullcontext()
    report={'passed':False,'backend':backend,'real_model_calls':0,'mock_llm_calls':0}
    print('GEOMETRY_ARTIFACT_STREAMING '+str(api.root),flush=True)
    try:
        with context as store:
            module=api.root/'geometry_artifact_fixture.py'
            try:
                settings=store.settings() if store else {}
                module.write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf-8')
                with api:
                    try:
                        exercise(api,store,report)
                    finally:
                        if 'api_process' in report:
                            stop_api(api,report['api_process'])
            finally:
                module.unlink(missing_ok=True)
        if store is not None:
            assert store.proc.poll() is not None
            report.update(s3_stopped=True,s3_receipt=str(store.root/'store.json'))
        report['api_exit']=process_identity_native.probe(report['api_process'])
        assert report['api_exit']['state']=='exited'
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1400]}
        raise
    finally:
        names=['app/routers/geometry.py','app/routers/files.py','app/routers/reports.py','app/raw_download.py',
               'app/services/file_read.py','app/services/latex_publication.py',
               'latex_artifact_streaming_smoke.py','testsupport.py','s3testsupport.py']
        paths=[Path(__file__).resolve(),*(SERVER/name for name in names)]
        report['source_sha256']={path.relative_to(SERVER).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in paths}
        (api.root/'geometry-artifact-streaming.json').write_text(
            json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('GEOMETRY_ARTIFACT_STREAMING_OK '+str(api.root),flush=True)


if __name__=='__main__':
    run('fs')
    run('s3')
