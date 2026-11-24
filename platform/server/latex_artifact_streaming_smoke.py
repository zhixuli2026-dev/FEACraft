"""Actual historical LaTeX PDF downloads against an FS and an authenticated S3 API."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

import httpx
from pypdf import PdfReader

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


MAIN = 'E-report/latex/main.tex'
PDF = 'E-report/latex/build/main.pdf'
SIZE = 40*1024*1024
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
from app.models import ArtifactPublication, ArtifactPublicationHead, Job, JobAttempt
from app.routers import reports
from app.routers.workspaces import get_owned_case
from app.services import artifact_publication, attempt_workspace, file_read, latex_artifacts
from app.services import node_storage_budget, process_identity_native, s3_live_store

root = Path(__file__).parent
probe = ContextVar('latex_download_measurement',default=None)
serve = reports._latex_artifact_response
original_chunks = file_read.FileRead.chunks
original_get = s3_live_store.Client.get_object

def observe_chunks(self,*args,**kwargs):
    state=probe.get()
    key=self.location['Key'] if self.remote else str(self.path).replace('\\','/')
    if state is None or '/files/' not in key or not key.endswith('/main.pdf'):
        yield from original_chunks(self,*args,**kwargs)
        return
    state['body_streams']+=1
    with closing(original_chunks(self,*args,**kwargs)) as chunks:
        for item in chunks:
            state['body_bytes']+=len(item)
            state['max_chunk']=max(state['max_chunk'],len(item))
            yield item

def observe_get(self,**args):
    state=probe.get()
    key=args['Key']
    if state is not None and '/files/' in key and key.endswith('/main.pdf'):
        state['s3_gets'].append({'range':args.get('Range'),'if_match':args.get('IfMatch')})
    return original_get(self,**args)

def measured(prefix,row,main,path,ranges,version,download):
    state={'body_streams':0,'body_bytes':0,'max_chunk':0,'s3_gets':[],
           'publication_id':row.id,'range':ranges,'head':download is None}
    token=probe.set(state)
    tracemalloc.start()
    try:
        response=serve(prefix,row,main,path,ranges,version,download)
        state['status']=response.status_code
        return response
    finally:
        state.update(peak=tracemalloc.get_traced_memory()[1],
                     response_spool_reserved=download._reserved if download is not None else 0)
        tracemalloc.stop()
        probe.reset(token)
        with (root/'download-trace.jsonl').open('a',encoding='utf-8') as log:
            log.write(json.dumps(state)+'\n')

file_read.FileRead.chunks=observe_chunks
s3_live_store.Client.get_object=observe_get
reports._latex_artifact_response=measured

@app.get('/fixture/latex/{case_id}/binding')
async def binding(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    return {'prefix':case.s3_prefix,'process':process_identity_native.observe(os.getpid())}

@app.post('/fixture/latex/{case_id}/publish')
async def publish(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=True)
    assert body['source'] in {'history','current'}
    source=root/body['source']
    main='E-report/latex/main.tex'
    names=latex_artifacts.paths(main)
    for name in ('manifest','toolchain','log'):
        (source/names[name]).parent.mkdir(parents=True,exist_ok=True)
    pdf=attempt_workspace.digest_file(source/names['pdf'])
    (source/names['log']).write_text('Owned LaTeX receipt fixture\n',encoding='utf-8')
    (source/names['manifest']).write_text(json.dumps({'main':main,'artifacts':{names['pdf']:pdf,
        names['log']:attempt_workspace.digest_file(source/names['log'])}}),encoding='utf-8')
    (source/names['toolchain']).write_text(json.dumps({'recipe':'pdflatex'}),encoding='utf-8')
    records=attempt_workspace.inventory(source, [names[name] for name in ('pdf','manifest','toolchain','log')])
    job=Job(case_id=case.id,type='latex',command='historical download fixture',status='succeeded',exit_code=0)
    db.add(job)
    await db.flush()
    attempt=JobAttempt(job_id=job.id,number=1,phase='succeeded',exit_code=0)
    db.add(attempt)
    await db.flush()
    descriptor={'handler':'latex.v1','main':main,'recipe':'pdflatex','source':{}}
    kind=latex_artifacts.channel(main)
    row=await artifact_publication.reserve(db,case,attempt,kind=kind,descriptor=descriptor)
    head=await db.get(ArtifactPublicationHead,(case.id,kind))
    if head.active_publication_id:
        previous=await db.get(ArtifactPublication,head.active_publication_id)
        previous.status='superseded'
    row.manifest_sha256=await asyncio.to_thread(artifact_publication.prepare_bundle,
        case.s3_prefix,row,source,records,descriptor)
    row.status='committed'
    head.active_publication_id=row.id
    await db.commit()
    return {'id':row.id,'manifest_sha256':row.manifest_sha256,'archive':row.immutable_path,'file':pdf}

@app.get('/fixture/latex:capacity')
async def capacity(user=Depends(get_current_user)):
    result=await asyncio.to_thread(node_storage_budget.observe)
    return {'reserved_bytes':result['reserved_bytes']}

@app.post('/fixture/latex:stop')
async def stop(body:dict,user=Depends(get_current_user)):
    assert body['pid']==os.getpid()
    os._exit(79)

class StopAtFirstBody:
    def __init__(self,app):
        self.app=app

    async def __call__(self,scope,receive,send):
        controlled = scope.get('path','').endswith('/latex/artifact') and any(
            key==b'x-fixture-stop-after-first-chunk' for key,_ in scope.get('headers',[]))
        sent=False
        async def hold(message):
            nonlocal sent
            await send(message)
            if controlled and not sent and message['type']=='http.response.body' and message.get('body'):
                sent=True
                (root/'first-body').touch()
                deadline=time.monotonic()+12
                while not (root/'release-first-body').exists():
                    if time.monotonic()>=deadline:
                        raise TimeoutError('Fixture client did not release first HTTP body block')
                    await asyncio.sleep(.02)
        await self.app(scope,receive,hold)

app=StopAtFirstBody(app)
'''


def make_pdf(root, directory, image_size):
    path = root/directory/PDF
    path.parent.mkdir(parents=True,exist_ok=True)
    content = b'q 96 0 0 96 20 20 cm /Im0 Do Q\n'
    offsets = [0]
    with path.open('xb') as out:
        out.write(b'%PDF-1.4\n%\xff\xfe\xfd\xfc\n')

        def start():
            offsets.append(out.tell())
            out.write(f'{len(offsets)-1} 0 obj\n'.encode())

        for value in (
            b'<</Type /Catalog /Pages 2 0 R>>',
            b'<</Type /Pages /Kids [3 0 R] /Count 1>>',
            b'<</Type /Page /Parent 2 0 R /MediaBox [0 0 150 150] /Resources <</XObject <</Im0 5 0 R>>>> /Contents 4 0 R>>',
        ):
            start()
            out.write(value+b'\nendobj\n')
        start()
        out.write(f'<</Length {len(content)}>>\nstream\n'.encode()+content+b'endstream\nendobj\n')
        start()
        out.write((f'<</Type /XObject /Subtype /Image /Width 4096 /Height {image_size//4096} '
                   f'/ColorSpace /DeviceGray /BitsPerComponent 8 /Length {image_size}>>\nstream\n').encode())
        left=image_size
        while left:
            amount=min(left,1024*1024)
            out.write(os.urandom(amount))
            left-=amount
        out.write(b'\nendstream\nendobj\n')
        xref=out.tell()
        out.write(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
        for offset in offsets[1:]:
            out.write(f'{offset:010d} 00000 n \n'.encode())
        out.write(f'trailer\n<</Size {len(offsets)} /Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n'.encode())
    assert len(PdfReader(path,strict=True).pages)==1
    return path


def response_hash(http, url, params, headers):
    digest, size = hashlib.sha256(), 0
    with http.stream('GET',url,params=params,headers=headers) as response:
        assert response.status_code==200
        for chunk in response.iter_bytes(65536):
            size+=len(chunk)
            digest.update(chunk)
        assert int(response.headers['Content-Length'])==size
    return {'size':size,'sha256':digest.hexdigest()}


def expected_bytes(path,start,stop):
    with path.open('rb') as stream:
        stream.seek(start)
        return stream.read(stop-start)


def save_archive(api, store, prefix, relative, source):
    if store is not None:
        with source.open('rb') as body:
            store.client.put_object(Bucket=store.bucket,Key=prefix+'/'+relative,Body=body)
        return
    root=(api.root/'files'/prefix).resolve()
    target=(root/relative).resolve(strict=True)
    assert root.is_relative_to(api.root.resolve()) and target.is_relative_to(root)
    scratch=target.with_suffix('.owned-replacement')
    shutil.copyfile(source,scratch)
    scratch.replace(target)


def state(api):
    return api.call('GET','/fixture/latex:capacity',api_prefix=False)['reserved_bytes']


def stop_api(api, identity):
    with httpx.Client(trust_env=False,timeout=10) as client:
        try:
            client.post(api.base+'/fixture/latex:stop',json={'pid':identity['pid']},
                        headers={'Authorization':'Bearer '+api.token})
        except httpx.TransportError:
            pass
        else:
            raise AssertionError('Fixture did not stop the bound API')
    api.proc.wait(timeout=15)


def exercise(api, store, report):
    case=api.create_case('Owned historical LaTeX download')
    case_id=case['id']
    route='/fixture/latex/'+str(case_id)
    binding=api.call('GET',route+'/binding',api_prefix=False)
    report['api_process']=binding['process']
    old_file=make_pdf(api.root,'history',SIZE)
    make_pdf(api.root,'current',65536)
    headers={'Authorization':'Bearer '+api.token}
    with httpx.Client(trust_env=False,timeout=120) as client:
        def publish(source):
            response=client.post(api.base+route+'/publish',headers=headers,json={'source':source})
            assert response.status_code==200, response.text
            return response.json()
        previous, current = publish('history'), publish('current')
    versions=api.call('GET',api.prefix+'/latex/publications?main='+MAIN)
    assert {item['id']:item['current'] for item in versions}=={current['id']:True,previous['id']:False}
    endpoint=api.base+'/api/v1'+api.prefix+'/latex/artifact'
    old={'path':PDF,'main':MAIN,'publication_id':previous['id']}
    key=previous['archive']+'/files/'+PDF
    signed=hashlib.sha256(json.dumps([previous['id'],previous['manifest_sha256'],PDF,
        previous['file']['sha256']],separators=(',',':')).encode()).hexdigest()
    ranged={**headers,'X-Vibeflow-File-Version':signed}
    with httpx.Client(trust_env=False,timeout=75) as client:
        head=client.head(endpoint,params=old,headers=headers)
        assert head.status_code==200 and head.content==b'' and head.headers['X-Vibeflow-File-Version']==signed
        assert int(head.headers['Content-Length'])==previous['file']['size'] and head.headers['Accept-Ranges']=='bytes'
        assert 'attachment' in head.headers['Content-Disposition']
        assert response_hash(client,endpoint,old,ranged)==previous['file']
        start=SIZE-101
        part=client.get(endpoint,params=old,headers={**ranged,'Range':f'bytes={start}-{start+63}'})
        assert part.status_code==206 and part.content==expected_bytes(old_file,start,start+64)
        assert part.headers['Content-Range']==f'bytes {start}-{start+63}/{previous["file"]["size"]}'
        multi=client.get(endpoint,params=old,headers={**ranged,'Range':'bytes=1024-1039,0-7'})
        assert multi.status_code==206 and multi.headers['Content-Type'].startswith('multipart/byteranges;')
        marker=multi.headers['Content-Type'].split('boundary=',1)[1].encode()
        parts=multi.content.split(b'--'+marker)
        assert parts[1].endswith(expected_bytes(old_file,1024,1040)+b'\r\n')
        assert parts[2].endswith(expected_bytes(old_file,0,8)+b'\r\n')
        invalid=client.get(endpoint,params=old,headers={**ranged,'Range':'bytes=90000000-90000001'})
        assert invalid.status_code==416 and invalid.headers['Content-Range']==f'bytes */{previous["file"]["size"]}'
        latest=client.head(endpoint,params={'path':PDF,'main':MAIN},headers=headers)
        assert latest.status_code==200 and int(latest.headers['Content-Length'])==current['file']['size']
        bad_version=client.get(endpoint,params=old,headers={**headers,'X-Vibeflow-File-Version':'0'*64})
        assert bad_version.status_code==409 and bad_version.json()['detail']['code']=='file_changed_during_read'
        assert client.get(endpoint,params={**old,'main':'E-report/latex/other.tex'},headers=headers).status_code==404
        assert client.get(endpoint,params={**old,'path':'E-report/latex/build/main.latex-toolchain.json'},
                          headers=headers).status_code==404
        outsider=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other reader',
            'password':'isolated-test-123'})
        assert client.get(endpoint,params=old,headers={'Authorization':'Bearer '+outsider['token']}).status_code==404

        damaged=api.root/'same-size-damaged.pdf'
        shutil.copyfile(old_file,damaged)
        with damaged.open('r+b') as stream:
            stream.seek(SIZE-8192)
            byte=stream.read(1)
            stream.seek(-1,1)
            stream.write(bytes([byte[0]^0xff]))
        try:
            save_archive(api,store,binding['prefix'],key,damaged)
            rejected=client.get(endpoint,params=old,headers={**ranged,'Range':'bytes=0-7'})
            assert rejected.status_code==409 and rejected.json()['detail']['code']=='evidence_invalid'
            rejected_full=client.get(endpoint,params=old,headers=ranged)
            assert rejected_full.status_code==409 and rejected_full.json()['detail']['code']=='evidence_invalid'
            assert state(api)==0
        finally:
            save_archive(api,store,binding['prefix'],key,old_file)
            damaged.unlink(missing_ok=True)
        release=api.root/'release-first-body'
        try:
            with client.stream('GET',endpoint,params=old,headers={**ranged,'X-Fixture-Stop-After-First-Chunk':'1'}) as pending:
                assert pending.status_code==200
                assert next(pending.iter_bytes(65536))==expected_bytes(old_file,0,65536)
                deadline=time.monotonic()+6
                while not (api.root/'first-body').exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                assert (api.root/'first-body').exists()
                held=state(api)
                assert held>=previous['file']['size'],held
        finally:
            release.touch()
        deadline=time.monotonic()+10
        while state(api)!=0:
            if time.monotonic()>=deadline:
                raise TimeoutError('Disconnected publication download retained a node reservation')
            time.sleep(.05)
        assert client.head(endpoint,params=old,headers=ranged).headers['X-Vibeflow-File-Version']==signed

    traces=[json.loads(line) for line in (api.root/'download-trace.jsonl').read_text(encoding='utf-8').splitlines()]
    successful=[trace for trace in traces if trace['publication_id']==previous['id'] and trace.get('status') in {200,206,416}]
    assert len(successful)==7, successful
    for trace in successful:
        assert trace['body_streams']==1 and trace['max_chunk']<=65536,trace
        assert trace['body_bytes']==previous['file']['size'] and trace['peak']<SIZE//4,trace
        if store is not None:
            assert len(trace['s3_gets'])==1 and trace['s3_gets'][0]['range'] is None and trace['s3_gets'][0]['if_match']
    selected=[trace for trace in successful if trace['status']==206]
    assert len(selected)==2 and all(trace['response_spool_reserved']==0 for trace in selected)
    if store is not None:
        local=api.root/'files'/binding['prefix']/previous['archive']
        assert not local.exists()
    report.update({'previous':previous,'current':current,'version':signed,'expectation_checks':{
        'byte_pinned_HEAD_200':'passed','full_GET_200_sha256':'passed','historical_single_and_ordered_multipart_206':'passed',
        'default_current_and_wrong_main_404':'passed','invalid_range_416':'passed','cross_owner_404':'passed',
        'wrong_version_409':'passed','corruption_outside_range_409_and_zero_reservations':'passed',
        'actual_disconnect_reservation_released':'passed'},
        'reserved_during_disconnect':held,'successful_traces':successful})


def run(backend):
    from app.services import process_identity_native
    api=IsolatedServer('latex-artifact-'+backend,app='latex_artifact_fixture:app')
    context=ObjectStore(executable=RUNTIME,volume_max=16,authenticated=True) if backend=='s3' else nullcontext()
    report={'passed':False,'backend':backend,'real_model_calls':0,'mock_llm_calls':0}
    print('LATEX_ARTIFACT_STREAMING '+str(api.root),flush=True)
    try:
        with context as store:
            module=api.root/'latex_artifact_fixture.py'
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
            report.update({'s3_stopped':True,'s3_receipt':str(store.root/'store.json')})
        report['api_exit']=process_identity_native.probe(report['api_process'])
        assert report['api_exit']['state']=='exited'
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1400]}
        raise
    finally:
        names=['app/routers/files.py','app/routers/reports.py','app/services/file_read.py',
               'app/services/latex_publication.py','app/raw_download.py','testsupport.py','s3testsupport.py']
        paths=[Path(__file__).resolve(),*(SERVER/name for name in names)]
        report['source_sha256']={path.relative_to(SERVER).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in paths}
        (api.root/'latex-artifact-streaming.json').write_text(
            json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('LATEX_ARTIFACT_STREAMING_OK '+str(api.root),flush=True)


if __name__=='__main__':
    run('fs')
    run('s3')
