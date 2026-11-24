"""Actual default local document jobs and real model readback."""
import argparse,asyncio,hashlib,io,json,sys
from pathlib import Path
from PIL import Image
from pypdf import PdfReader,PdfWriter
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from live_llm_acceptance import FIXTURE,source
from live_llm_sandbox_recovery_acceptance import model_read
from live_llm_remote_documents_acceptance import upload
from testsupport import IsolatedServer,SERVER


def verify(configuration):
    api=IsolatedServer('local-documents',app='local_documents_fixture:app')
    (api.root/'local_documents_fixture.py').write_text("import os\nos.environ.update(VIBEFLOW_DISTRIBUTED_JOBS='false',VIBEFLOW_JOB_EXECUTION_HOSTS='{}')\n"+FIXTURE,'utf8')
    result={'passed':False,'mock_llm_calls':0,'jobs':[]}
    try:
        with api:
            api.create_case('Actual local documents')
            writer=PdfWriter()
            for _ in range(3):writer.add_blank_page(width=300,height=400)
            pdf=io.BytesIO();writer.write(pdf)
            png=io.BytesIO();Image.new('RGB',(32,24),'orange').save(png,format='PNG')
            upload(api,'Ref/source.pdf',pdf.getvalue(),'application/pdf');upload(api,'Ref/slide.png',png.getvalue(),'image/png')
            items=[('pdf.number','/pdf:number',{'input':'Ref/source.pdf','output':'E-report/build/local-numbered.pdf'}),
                ('ppt.build','/ppt:build',{'output':'E-report/build/local.pptx','title':'Actual local presentation','template':None,
                    'slides':[{'title':'Local professional worker','bullets':['Actual editable slide'],'image':{'path':'Ref/slide.png'}}]})]
            for kind,endpoint,parameters in items:
                job=api.wait_job(api.call('POST',api.prefix+endpoint,parameters)['job_id'])
                assert job['attempt']['handler']['key']==kind and 'environment' not in job['attempt']['handler']
                assert job['attempt']['process_stop']['verified'] and job['attempt']['process_identity']['host']['platform']=='windows'
                data=api.call('GET',api.prefix+'/raw?path='+parameters['output'],raw=True)
                if kind=='pdf.number':
                    pages=PdfReader(io.BytesIO(data)).pages;assert len(pages)==3
                    for i,page in enumerate(pages,1):assert f'{i} / 3' in page.extract_text()
                else:
                    deck=Presentation(io.BytesIO(data));assert len(deck.slides)==1
                    pictures=[s for slide in deck.slides for s in slide.shapes if s.shape_type==MSO_SHAPE_TYPE.PICTURE]
                    assert len(pictures)==1 and pictures[0].image.blob==png.getvalue()
                result['jobs'].append(job);print('LOCAL_DOCUMENT_PASS '+kind,flush=True)
            api.put('Ref/read-documents.py',"from pathlib import Path\nimport hashlib\ndata=Path('E-report/build/local-numbered.pdf').read_bytes()+Path('E-report/build/local.pptx').read_bytes()\nPath('Ref/proof.txt').write_text('RETIREMENT_'+hashlib.sha256(data).hexdigest()+'\\n')\n")
            job=api.call('POST',api.prefix+'/jobs',{'type':'convert','runtime':'windows','command':'python Ref/read-documents.py'})
            result['jobs'].append(api.wait_job(job['id']))
            content=api.call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)
            proof=content.decode().strip()
            result['real_model_calls']=model_read(api,'','',proof,configuration,already_restored=True,
                path='Ref/proof.txt',expected_content=content)
            result['passed']=True
    except BaseException as error:result['failure_type']=type(error).__name__;raise
    finally:(api.root/'local-documents.json').write_text(json.dumps(result,indent=2)+'\n','utf8')


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text('utf8'))
    assert review['entry']=='platform/server/live_llm_local_documents_acceptance.py'
    for name,digest in review['sources'].items():assert hashlib.sha256((SERVER.parents[1]/name).read_bytes()).hexdigest()==digest,name
    await asyncio.to_thread(verify,await source(args.source_profile_id))
if __name__=='__main__':asyncio.run(main())
