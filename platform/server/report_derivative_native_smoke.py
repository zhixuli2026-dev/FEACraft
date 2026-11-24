"""Real draft, numbered PDF, PPT and report history through fs/S3 independent APIs."""
import argparse
from contextlib import nullcontext
import hashlib
from io import BytesIO
import json
from urllib.parse import quote

import httpx
from pypdf import PdfReader
from pptx import Presentation
from latex_history_native_smoke import api_for
from pdf_publication_smoke import pdf
from ppt_publication_smoke import template
from s3_typed_publication_smoke import RUNTIME
from s3testsupport import ObjectStore


def upload(api,path,data):
    with httpx.Client(trust_env=False,timeout=120) as http:
        response=http.post(api.base+'/api/v1'+api.prefix+'/upload',params={'path':path},
            headers={'Authorization':'Bearer '+api.token},files={'file':('artifact',data)})
        assert response.status_code==200,response.text[:400]


def native_history(api):
    checks=[];records=[];api.create_case('native derivative history')
    def build(endpoint,body):
        created=api.call('POST',api.prefix+endpoint,body)
        job=api.wait_job(created['job_id'],timeout=180)
        identifier=created.get('publication_id') or job['attempt']['outputs']['publication']['id']
        return identifier
    def capture(identifier):
        query=api.prefix+'/report/derivative?publication_id='+identifier
        document=api.call('GET',query);original=document['files'][0]['path']
        artifact=api.prefix+'/report/derivative-artifact?publication_id='+identifier+'&path='+quote(original)
        data=api.call('GET',artifact,raw=True)
        assert hashlib.sha256(data).hexdigest()==document['files'][0]['sha256']
        assert document['publication']['current'] and not document['publication']['stale']
        records.append({'id':identifier,'query':query,'artifact':artifact,'bytes':data,'family':document['family']})
        return document,data
    upload(api,'Ref/source.pdf',pdf());upload(api,'Ref/template.pptx',template())
    for family,endpoint,body in [('pdf-number','/pdf:number',{'input':'Ref/source.pdf','output':'E-report/numbered.pdf'}),
            ('ppt','/ppt:build',{'title':'History deck','template':'Ref/template.pptx','output':'E-report/deck.pptx',
                'slides':[{'title':'Original slide','text':'Original body'}]})]:
        first=build(endpoint,body);doc,old=capture(first)
        assert api.call('GET',api.prefix+'/raw?path='+quote(doc['output']),raw=True)==old
        if family=='pdf-number':
            parsed=PdfReader(BytesIO(old));assert len(parsed.pages)==2 and '1 / 2' in parsed.pages[0].extract_text()
        else:
            slide=Presentation(BytesIO(old)).slides[-1]
            assert next(shape for shape in slide.shapes if shape.name=='VibeFlowTitle').text=='Original slide'
        api.call('DELETE',api.prefix+'/file?path='+quote(doc['output']))
        second=build(endpoint,body);capture(second)
        assert second!=first
        # Sources changing makes the active head stale while both generations stay readable.
        upload(api,body.get('input') or body['template'],b'changed source after publication')
        for identifier in (first,second):
            current=api.call('GET',api.prefix+'/report/derivative?publication_id='+identifier)
            assert current['publication']['stale'] and current['publication']['current']==(identifier==second)
        api.put(doc['output'],'manual alias replacement')
        assert api.call('GET',records[-2]['artifact'],raw=True)==old
        checks.append(f'{family}_two_native_versions_old_aliases_and_stale_inputs_do_not_change_history')
    # Ordinary composition is supported without fabricating engineering evidence.
    for output in ('E-report/report.md','E-report/report.generated.md'):
        first=build('/report:compose?wait=false',{'output':output});doc,old=capture(first)
        assert api.call('GET',api.prefix+'/raw?path='+quote(output),raw=True)==old
        second=build('/report:compose?wait=false',{'output':output,'overwrite':True})
        doc,new=capture(second)
        assert len(doc['files'])==2 and doc['files'][1]['role']=='previous-draft'
        previous=api.call('GET',api.prefix+'/report/derivative-artifact?publication_id='+second+'&path='+quote(doc['files'][1]['path']),raw=True)
        assert previous==old
        api.put(output,'manual draft after generation')
        state=api.call('GET',api.prefix+'/report/derivative?publication_id='+second)['publication']
        assert not state['current'] and state['stale'] and not state['applied'],state
        assert api.call('GET',records[-2]['artifact'],raw=True)==old
    checks.append('both_draft_channels_keep_originals_exact_backups_and_unapplied_status')
    # The existing HTML/DOCX reader must also retain versions across a fresh S3 cache.
    report_records=[]
    for index in (1,2):
        api.put('E-report/report.md',f'# Native report version {index}\n\nImmutable content {index}.\n')
        identifier=build('/report:build',{})
        document=api.call('GET',api.prefix+'/report/results?publication_id='+identifier)
        for path in ('E-report/build/report.html','E-report/build/report.docx'):
            artifact=api.prefix+'/report/artifact?publication_id='+identifier+'&path='+quote(path)
            report_records.append({'query':api.prefix+'/report/results?publication_id='+identifier,
                'artifact':artifact,'id':identifier,'bytes':api.call('GET',artifact,raw=True)})
    api.put('E-report/report.md','Edited after both native reports')
    checks.append('two_native_HTML_DOCX_versions_captured_before_source_edit')
    return checks,records,report_records


def verify_reads(reader,records,report_records):
    checks=[]
    with httpx.Client(trust_env=False,timeout=120) as http:
        headers={'Authorization':'Bearer '+reader.token}
        for row in records:
            document=reader.call('GET',row['query']);assert document['publication']['id']==row['id']
            assert document['publication']['stale']
            url=reader.base+'/api/v1'+row['artifact']
            head=http.head(url,headers=headers);assert head.status_code==200 and not head.content
            version=head.headers['x-vibeflow-file-version']
            body=http.get(url,headers=headers);assert body.status_code==200 and body.content==row['bytes']
            part=http.get(url,headers={**headers,'Range':'bytes=0-63','X-Vibeflow-File-Version':version})
            assert part.status_code==206 and part.content==row['bytes'][:64]
            assert http.get(url,headers={**headers,'X-Vibeflow-File-Version':'0'*64}).status_code==409
            assert http.get(url.split('&path=')[0]+'&path=.vibeflow/private.json',headers=headers).status_code==404
        checks.append('all_derivatives_exact_full_HEAD_ranges_and_wrong_version_or_private_path_rejected')
    for row in report_records:
        document=reader.call('GET',row['query']);assert document['publication']['id']==row['id'] and document['publication']['stale']
        assert reader.call('GET',row['artifact'],raw=True)==row['bytes']
    checks.append('all_HTML_DOCX_versions_stale_and_exact_original_bytes')
    return checks


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
        settings=store.settings() if store else {}
        with api_for(settings) as api:
            checks,records,reports=native_history(api)
            checks+=verify_reads(api,records,reports)
            if store:
                with api_for(settings,peer=api) as reader:
                    assert reader.root!=api.root
                    checks+=['fresh_API_'+check for check in verify_reads(reader,records,reports)]
            result={'passed':True,'storage':'s3' if store else 'fs','database':'sqlite','count':len(checks),'checks':checks,
                'scope':'Actual workers, 8 derivative generations plus 2 native HTML/DOCX reports; independent API for S3'}
        (api.root/'derivative-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print('DERIVATIVE_NATIVE_OK '+json.dumps({'evidence':str(api.root),**result}),flush=True)


if __name__=='__main__':main()
