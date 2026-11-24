"""Actual PDF/PPT output collisions, sealed retries, current reruns and real model readback."""
import asyncio,hashlib,io,json,time,uuid
from pathlib import Path
import httpx
from pypdf import PdfReader,PdfWriter
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from PIL import Image
from live_llm_job_repeat_acceptance import main,repeat,write

FIXTURE=r'''
from pathlib import Path
from app import storage
from app.db import SessionLocal
from app.models import Case
from app.services import job_execution as document_execution
from app.services.attempt_workspace import canonical_path
document_test_root=Path(__file__).parent
document_original_release=document_execution.release_process
async def document_collision_release(owner,process):
 mode_path=document_test_root/'document-collision-mode'
 mode=mode_path.read_text(encoding='utf8') if mode_path.exists() else 'none'
 if mode in {'pdf.number','ppt.build'}:
  async with SessionLocal() as db:
   job,attempt=await document_execution._owned(db,owner)
   record=json.loads(attempt.input_manifest_json)
   if record.get('handler',{}).get('key')==mode:
    case=await db.get(Case,job.case_id)
    root=storage._fs_path(case.s3_prefix,record['workspace']['run_path']).resolve()
    descriptor=json.loads(attempt.publication_json)['descriptor']
    relative=canonical_path(descriptor['output']);job_id=job.id;attempt_id=attempt.id
   else:root=None
  if root is not None:
   target=(root/relative).resolve()
   assert target.is_relative_to(root) and target!=root
   target.parent.mkdir(parents=True,exist_ok=True)
   with target.open('xb') as stream:stream.write(b'ACTUAL_PREEXISTING_PRIVATE_OUTPUT')
   (document_test_root/('actual-document-collision-'+str(job_id)+'.json')).write_text(json.dumps(
    {'job_id':job_id,'attempt_id':attempt_id,'handler':mode,'path':relative,'actual_file_created':True}),encoding='utf8')
 return await document_original_release(owner,process)
document_execution.release_process=document_collision_release
'''


def upload(api,path,data,mime):
    deadline=time.monotonic()+30
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=30) as client:
        while True:
            response=client.post(api.prefix+'/upload',params={'path':path},files={'file':(Path(path).name,data,mime)})
            if response.status_code==200:return response.json()
            value=response.json();detail=value.get('detail')
            if response.status_code==409 and isinstance(detail,dict) and detail.get('code') in {'storage_quota_busy','validation_busy'}:
                if time.monotonic()>deadline:raise TimeoutError('Actual binary upload admission did not drain')
                time.sleep(.1);continue
            raise AssertionError('Actual binary upload refused: '+str(response.status_code)+' '+response.text[:1000])


def pdf_bytes(pages):
    writer=PdfWriter()
    for _ in range(pages):writer.add_blank_page(width=300,height=400)
    out=io.BytesIO();writer.write(out);return out.getvalue()


def png_bytes(color):
    out=io.BytesIO();Image.new('RGB',(32,24),color).save(out,format='PNG');return out.getvalue()


def verify_pdf(data,pages):
    reader=PdfReader(io.BytesIO(data));assert len(reader.pages)==pages
    for number,page in enumerate(reader.pages,1):assert str(number)+' / '+str(pages) in (page.extract_text() or '')


def verify_ppt(data,image):
    deck=Presentation(io.BytesIO(data));assert len(deck.slides)==1
    pictures=[shape for slide in deck.slides for shape in slide.shapes if shape.shape_type==MSO_SHAPE_TYPE.PICTURE]
    assert len(pictures)==1 and pictures[0].image.blob==image
    assert any(shape.has_text_frame and 'Frozen section' in shape.text for slide in deck.slides for shape in slide.shapes)


def scenario(api,**_):
    checks=[];mode=api.root/'document-collision-mode'
    for kind in ('pdf.number','ppt.build'):
        api.create_case('Actual document '+kind+' repeat')
        if kind=='pdf.number':
            input_path='Ref/source.pdf';before=pdf_bytes(2);after=pdf_bytes(3);mime='application/pdf'
            parameters={'input':input_path,'output':'E-report/build/original-numbered.pdf'};endpoint='/pdf:number'
            fresh_parameters={**parameters,'output':'E-report/build/current-numbered.pdf'}
            validate_old=lambda data:verify_pdf(data,2);validate_new=lambda data:verify_pdf(data,3)
        else:
            input_path='Ref/slide.png';before=png_bytes('orange');after=png_bytes('blue');mime='image/png'
            parameters={'output':'E-report/build/original.pptx','title':'Frozen document','template':None,
                'slides':[{'title':'Frozen section','bullets':['Actual editable slide'],'image':{'path':input_path}}]};endpoint='/ppt:build'
            fresh_parameters={**parameters,'output':'E-report/build/current.pptx'}
            validate_old=lambda data:verify_ppt(data,before);validate_new=lambda data:verify_ppt(data,after)
        upload(api,input_path,before,mime)
        mode.write_text(kind,encoding='utf8')
        initial=write(api,'POST',api.prefix+endpoint,parameters);identifier=initial['job_id']
        failed=api.wait_job(identifier,expected='failed')
        assert failed['attempt']['handler']['key']==kind and failed['attempt']['process_stop']['verified']
        assert failed['exit_code']!=0 and 'FileExistsError' in failed['log_backlog']
        fault=json.loads((api.root/('actual-document-collision-'+str(identifier)+'.json')).read_text(encoding='utf8'))
        assert fault['actual_file_created'] and fault['attempt_id']==failed['attempt']['id']
        case_root=next((api.root/'files/workspaces').glob('*/cases/'+str(api.case_id)))
        private=case_root/failed['attempt']['workspace']['run_path']
        assert (private/parameters['output']).read_bytes()==b'ACTUAL_PREEXISTING_PRIVATE_OUTPUT'
        mode.write_text('none',encoding='utf8')
        choice=api.call('GET','/jobs/'+str(identifier)+'/operations')
        assert choice['retry']['available'] and not choice['resume']['available']
        request={'action':'retry','source_attempt_id':choice['source_attempt_id'],'source_version':choice['source_version']}
        retry_key=uuid.uuid4().hex;retried=repeat(api,identifier,request,retry_key);done=api.wait_job(retried['id'])
        assert done['attempt']['number']==2 and done['attempt']['root_job_id']==identifier
        assert done['attempt']['handler']['key']==kind and done['attempt']['workspace']['files']==failed['attempt']['workspace']['files']
        assert done['attempt']['outputs']['publication']['status']=='committed'
        output=api.call('GET',api.prefix+'/raw?path='+parameters['output'],raw=True);validate_old(output)
        bundle=case_root/'.vibeflow/publications'/done['attempt']['outputs']['publication']['id']/'files'/parameters['output']
        historical=bundle.read_bytes();assert historical==output
        choice=api.call('GET','/jobs/'+str(identifier)+'/operations')
        occupied={'action':'rerun','source_attempt_id':choice['source_attempt_id'],'source_version':choice['source_version'],
            'expected_revision':choice['expected_revision'],'expected_event_sequence':choice['expected_event_sequence'],'parameters':parameters}
        refused=repeat(api,identifier,occupied,uuid.uuid4().hex,expect=409)
        assert refused['detail']['code']=='output_exists'
        upload(api,input_path,after,mime)
        choice=api.call('GET','/jobs/'+str(identifier)+'/operations')
        rerun={'action':'rerun','source_attempt_id':choice['source_attempt_id'],'source_version':choice['source_version'],
            'expected_revision':choice['expected_revision'],'expected_event_sequence':choice['expected_event_sequence'],'parameters':fresh_parameters}
        fresh=repeat(api,identifier,rerun,uuid.uuid4().hex);fresh=api.wait_job(fresh['id'])
        assert fresh['attempt']['number']==1 and fresh['attempt']['root_job_id']==fresh['id']
        assert fresh['attempt']['handler']['key']==kind and fresh['attempt']['outputs']['publication']['status']=='committed'
        assert fresh['attempt']['workspace']['files'][input_path]['sha256']==hashlib.sha256(after).hexdigest()
        validate_new(api.call('GET',api.prefix+'/raw?path='+fresh_parameters['output'],raw=True))
        assert bundle.read_bytes()==historical
        replay=repeat(api,identifier,request,retry_key);assert replay['id']==retried['id'] and not replay['created']
        assert api.call('GET','/jobs/'+str(identifier))['status']=='failed'
        checks.append({'handler':kind,'source_job':identifier,'retry_job':retried['id'],'rerun_job':fresh['id'],
            'actual_worker_failed_on_private_output_collision':True,'retry_same_sealed_files':True,'retry_same_root':True,
            'retry_formal_publication':True,'rerun_current_resource_sha256':hashlib.sha256(after).hexdigest(),
            'rerun_new_root':True,'rerun_formal_publication':True,'actual_document_content_verified':True,
            'occupied_output_refused':True,'old_immutable_document_preserved':True,'old_failure_preserved':True,'idempotent_retry':True})
    report={'passed':True,'mock_llm_calls':0,'actual_document_jobs':6,'actual_pdf_and_ppt_libraries':True,'checks':checks}
    (api.root/'document-repeat.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
