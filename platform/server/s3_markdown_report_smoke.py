"""Real S3 Markdown resource preparation and Pandoc publication across caches."""
import io
import json
import zipfile

from ordinary_document_smoke import BLUE
from professional_launch_smoke import settled
from s3_typed_publication_smoke import api_for,RUNTIME
from s3testsupport import ObjectStore


def main():
    checks=[]
    with ObjectStore(executable=RUNTIME) as store:
        with api_for(store) as api:
            api.create_case('Remote Markdown report')
            api.put('Ref/plot.svg',BLUE)
            markdown='# Remote native document\n\n![figure](../Ref/plot.svg)\n\n$x^2$\n'
            api.put('E-report/report.md',markdown)
            submitted=api.call('POST',api.prefix+'/report:build',{})
            job=settled(api,submitted['job_id'])
            assert job['status']=='succeeded',job
            assert job['attempt']['workspace']['storage']['backend']=='s3'
            assert job['attempt']['outputs']['publication']['status']=='committed'
            html=api.call('GET',api.prefix+'/raw?path=E-report/build/report.html',raw=True)
            docx=api.call('GET',api.prefix+'/raw?path=E-report/build/report.docx',raw=True)
            assert b'Remote native document' in html and b'data:image/svg+xml' in html and b'<math' in html
            with zipfile.ZipFile(io.BytesIO(docx)) as archive:
                assert any(b'fill="blue"' in archive.read(name) for name in archive.namelist() if name.startswith('word/media/'))
            assert api.call('GET',api.prefix+'/raw?path=E-report/report.md',raw=True)==markdown.encode()
            checks.append('remote_Markdown_native_HTML_DOCX_math_and_relative_image_resource')
            result=api.call('GET',api.prefix+'/report/results')
            assert not result['publication']['stale']
            with api_for(store,peer=api) as reader:
                assert not reader.call('GET',api.prefix+'/report/results')['publication']['stale']
                ref={'command':'echo REMOTE_MARKDOWN_REFERENCE','input_publications':[{
                    'publication_id':result['publication']['id'],'paths':['E-report/build/report.html'],'require_current':True}]}
                consumed=reader.call('POST',api.prefix+'/jobs',ref)
                assert settled(reader,consumed['id'])['status']=='succeeded'
                checks.append('fresh_cache_reads_and_consumes_current_Markdown_publication')
                api.put('Ref/plot.svg',BLUE.replace('blue','red'))
                assert reader.call('GET',api.prefix+'/report/results')['publication']['stale']
                reader.call('POST',api.prefix+'/jobs',ref,expect=409)
                ref['input_publications'][0]['require_current']=False
                consumed=reader.call('POST',api.prefix+'/jobs',ref)
                assert settled(reader,consumed['id'])['status']=='succeeded'
                assert api.call('GET',api.prefix+'/raw?path=E-report/build/report.html',raw=True)==html
                checks.append('changed_image_invalidates_current_reference_and_preserves_historical_document')
            views=api.root/'files/.case-read-views'
            assert not views.exists() or not list(views.iterdir())
            result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(store.root),'api':str(api.root)}
    (store.root/'markdown-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('S3_MARKDOWN_REPORT_OK '+json.dumps(result),flush=True)


if __name__=='__main__':main()
