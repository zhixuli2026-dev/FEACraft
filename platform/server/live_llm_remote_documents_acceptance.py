"""Real Windows-submitted PDF/PPT workers executed on Linux with shared PG/S3."""
import asyncio,hashlib,io,json,shlex,time
from pathlib import Path
import asyncpg,httpx
from PIL import Image
from pypdf import PdfReader,PdfWriter
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
import live_llm_remote_worker_acceptance as baseline


def upload(api,path,data,mime):
    deadline=time.monotonic()+45
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=30) as client:
        while True:
            response=client.post(api.prefix+'/upload',params={'path':path},files={'file':(Path(path).name,data,mime)})
            if response.status_code==200:return
            detail=response.json().get('detail')
            if response.status_code==409 and isinstance(detail,dict) and detail.get('code') in {'storage_quota_busy','validation_busy'} and time.monotonic()<deadline:
                time.sleep(.2);continue
            raise AssertionError('Actual document upload refused: '+str(response.status_code))


async def scenario(api,worker,selected):
    result={'passed':False,'mock_llm_calls':0,'checks':[],'jobs':[]}
    def save():(api.root/'remote-documents.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    def record(text):result['checks'].append(text);save();print('REMOTE_DOCUMENT_PASS '+text,flush=True)
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    async def wait(identifier):
        deadline=time.monotonic()+120
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':return job
            if job['status'] in {'failed','cancelled','interrupted'}:
                result['failed_job']=job;save();raise AssertionError('Remote document failed')
            assert time.monotonic()<deadline,'Remote document did not settle'
            await asyncio.sleep(.25)
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database=selected.database,timeout=10)
    try:
        writer=PdfWriter()
        for _ in range(3):writer.add_blank_page(width=300,height=400)
        pdf=io.BytesIO();writer.write(pdf);original=pdf.getvalue()
        picture=io.BytesIO();Image.new('RGB',(32,24),'orange').save(picture,format='PNG');png=picture.getvalue()
        await asyncio.to_thread(upload,api,'Ref/source.pdf',original,'application/pdf')
        await asyncio.to_thread(upload,api,'Ref/slide.png',png,'image/png')
        items=[('pdf.number','/pdf:number',{'input':'Ref/source.pdf','output':'E-report/build/remote-numbered.pdf'}),
               ('ppt.build','/ppt:build',{'output':'E-report/build/remote.pptx','title':'Actual Linux presentation','template':None,
                'slides':[{'title':'Remote professional worker','bullets':['Actual editable slide'],'image':{'path':'Ref/slide.png'}}]})]
        for kind,endpoint,parameters in items:
            response=await call('POST',api.prefix+endpoint,body=parameters)
            identifier=response['job_id']
            before=await connection.fetchrow('SELECT input_manifest_json FROM job_attempts WHERE job_id=$1',identifier)
            job=await wait(identifier);result['jobs'].append(job)
            after=await connection.fetchrow('SELECT input_manifest_json FROM job_attempts WHERE job_id=$1',identifier)
            assert before['input_manifest_json']==after['input_manifest_json']
            document=json.loads(before['input_manifest_json']);binding=document['handler']['environment']
            assert document['handler']['key']==kind and ':' in document['execution_spec']['argv'][0]
            events=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',
                job['attempt']['id'],'attempt.execution_environment')
            assert len(events)==1
            actual=json.loads(events[0]['payload_json'])
            assert actual['declaration']==binding and actual['host_id']==worker.observed['host_id']
            assert actual['host_id']!=binding['origin_host_id']
            assert actual['resolved_argv'][0]==baseline.PYTHON and actual['resolved_argv'][1].startswith('/mnt/d/')
            assert actual['resolved_argv'][2:]==document['execution_spec']['argv'][2:]
            assert job['attempt']['process_identity']['host']['platform']=='linux'
            assert job['attempt']['process_stop']['verified']
            data=await call('GET',api.prefix+'/raw?path='+parameters['output'],raw=True)
            (api.root/Path(parameters['output']).name).write_bytes(data)
            if kind=='pdf.number':
                pages=PdfReader(io.BytesIO(data)).pages;assert len(pages)==3
                for index,page in enumerate(pages,1):assert f'{index} / 3' in page.extract_text()
                assert await call('GET',api.prefix+'/raw?path=Ref/source.pdf',raw=True)==original
            else:
                deck=Presentation(io.BytesIO(data));assert len(deck.slides)==1
                pictures=[s for slide in deck.slides for s in slide.shapes if s.shape_type==MSO_SHAPE_TYPE.PICTURE]
                assert len(pictures)==1 and pictures[0].image.blob==png
                assert any(s.has_text_frame and 'Remote professional worker' in s.text for slide in deck.slides for s in slide.shapes)
            result.setdefault('environments',[]).append(actual)
            result.setdefault('artifacts',[]).append({'path':parameters['output'],'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
            record(kind+' preserves its Windows submission and runs the matching registered Linux program')
        code="from pathlib import Path;import hashlib;data=Path('E-report/build/remote-numbered.pdf').read_bytes()+Path('E-report/build/remote.pptx').read_bytes();Path('Ref/proof.txt').write_text('RETIREMENT_'+hashlib.sha256(data).hexdigest()+'\\n')"
        fresh=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','timeout_seconds':120,
            'command':'python3 -c '+shlex.quote(code)})
        result['jobs'].append(await wait(fresh['id']))
        record('actual Linux task hashes both published document artifacts for real model readback')
        result['passed']=True;save()
    except BaseException as error:
        result['failure_type']=type(error).__name__;save();raise
    finally:await connection.close();save()


async def verify(configuration,selected,admin,observed):
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenario)


if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_remote_documents_acceptance.py',verifier=verify))
