"""Real S3 composition, LaTeX, numbered PDF and editable PPT publications."""
import argparse
import hashlib
from io import BytesIO
import json
from urllib.parse import quote

from pypdf import PdfReader
from pptx import Presentation
from latex_handler_smoke import MAIN,BIB,COMPLEX
from pdf_publication_smoke import pdf
from ppt_publication_smoke import PNG,template
from professional_launch_smoke import settled
from s3_core_workflow_smoke import workflow_api
from s3_typed_publication_smoke import RUNTIME
from s3testsupport import ObjectStore


def raw(api,path):return api.call('GET',api.prefix+'/raw?path='+quote(path,safe='/'),raw=True)


def new_case(api,name):
    case=api.create_case(name)
    return {**case,'workspace_id':api.call('GET','/workspaces')[0]['id']}


def seed(store,case,path,data):
    prefix=f"workspaces/{case['workspace_id']}/cases/{case['id']}"
    store.client.put_object(Bucket=store.bucket,Key=prefix+'/'+path,Body=data)


def completed(api,submission):
    job=settled(api,submission['job_id'])
    assert job['status']=='succeeded',job
    assert job['attempt']['workspace']['storage']['backend']=='s3'
    assert job['attempt']['outputs']['publication']['status']=='committed'
    return job


def reference(api,publication,*,current=True,expect=200):
    created=api.call('POST',api.prefix+'/jobs',{'command':'echo DOCUMENT_REFERENCE','input_publications':[{
        'publication_id':publication,'require_current':current}]},expect=expect)
    if expect==200:assert settled(api,created['id'])['status']=='succeeded'


def compose(api,store,checks):
    case=new_case(api,'Remote report drafts')
    old=b'old draft\xff\x00';seed(store,case,'E-report/report.md',old)
    api.call('POST',api.prefix+'/report:compose?wait=false',{},expect=409)
    request={'overwrite':True,'expected_sha256':hashlib.sha256(old).hexdigest()}
    headers={'Idempotency-Key':'remote-compose'}
    submission=api.call('POST',api.prefix+'/report:compose?wait=false',request,extra_headers=headers)
    completed(api,submission)
    result=api.call('GET',api.prefix+'/report/compose-result')
    assert result['publication']['applied'] and not result['publication']['stale']
    assert raw(api,result['backup'])==old
    assert raw(api,'E-report/report.md')==result['content'].encode()
    assert api.call('POST',api.prefix+'/report:compose?wait=false',request,extra_headers=headers)['job_id']==submission['job_id']
    checks.append('compose_preserves_exact_non_UTF8_backup_CAS_and_idempotency')
    build=api.call('POST',api.prefix+'/report:build',{});job=completed(api,build)
    assert any(item['publication_id']==result['publication_id'] for item in job['attempt']['workspace']['publications'])
    report=api.call('GET',api.prefix+'/report/results');assert not report['publication']['stale']
    checks.append('generated_draft_is_pinned_input_to_real_HTML_DOCX_build')
    generated=api.call('POST',api.prefix+'/report:compose?wait=false',{'output':'E-report/report.generated.md'})
    completed(api,generated)
    draft=api.call('GET',api.prefix+'/report/compose-result?output=E-report/report.generated.md')
    assert draft['publication']['applied'] and raw(api,'E-report/report.md')==result['content'].encode()
    checks.append('two_composition_channels_have_independent_heads_and_targets')
    with workflow_api(store,peer=api) as reader:
        got=reader.call('GET',api.prefix+'/report/compose-result');assert got['publication']['applied']
        reference(reader,result['publication_id'])
        api.put('E-report/report.md','# User edit\n')
        got=reader.call('GET',api.prefix+'/report/compose-result?publication_id='+result['publication_id'])
        assert got['publication']['stale'] and not got['publication']['applied']
        assert reader.call('GET',api.prefix+'/report/compose-artifact?publication_id='+result['publication_id']+
            '&path='+quote(result['source_path'],safe='/'),raw=True)==result['content'].encode()
        reference(reader,result['publication_id'],current=False)
        reference(reader,result['publication_id'],expect=409)
        checks.append('fresh_cache_composition_application_and_historical_draft_survive_user_edit')


def pdfppt(api,store,checks):
    case=new_case(api,'Remote PDF and PPT')
    original=pdf();seed(store,case,'E-report/source.pdf',original)
    body={'input':'E-report/source.pdf'};headers={'Idempotency-Key':'remote-number'}
    created=api.call('POST',api.prefix+'/pdf:number',body,extra_headers=headers);job=completed(api,created)
    numbered_path=created['output'];numbered=raw(api,numbered_path);doc=PdfReader(BytesIO(numbered))
    assert len(doc.pages)==2 and '1 / 2' in doc.pages[0].extract_text() and raw(api,body['input'])==original
    assert api.call('POST',api.prefix+'/pdf:number',body,extra_headers=headers)['job_id']==created['job_id']
    api.call('POST',api.prefix+'/pdf:number',body,expect=409)
    pdfid=job['attempt']['outputs']['publication']['id']
    checks.append('remote_PDF_has_actual_page_labels_preserves_source_and_refuses_overwrite')
    seed(store,case,'template.pptx',template());seed(store,case,'pixel.png',PNG)
    body={'title':'Remote deck','output':'E-report/build/deck.pptx','template':'template.pptx',
        'slides':[{'title':'Editable remote slide','bullets':['Alpha','Beta'],'text':'Remote body',
            'table':{'headers':['Name','Value'],'rows':[['A',1]]},'image':{'path':'pixel.png'}}]}
    created=api.call('POST',api.prefix+'/ppt:build',body);job=completed(api,created)
    prs=Presentation(BytesIO(raw(api,created['output'])));assert len(prs.slides)==2
    assert any(shape.has_table for shape in prs.slides[1].shapes)
    assert '.vibeflow/prepared/ppt.json' in job['attempt']['workspace']['generated_inputs']
    pptid=job['attempt']['outputs']['publication']['id']
    api.call('POST',api.prefix+'/ppt:build',body,expect=409)
    checks.append('remote_PPT_keeps_template_image_table_and_private_request_configuration')
    created=api.call('POST',api.prefix+'/ppt:build',{'output':'E-report/build/text.pptx',
        'title':'Text only','slides':[{'title':'No resources','text':'Generated config only'}]})
    completed(api,created);assert len(Presentation(BytesIO(raw(api,created['output']))).slides)==1
    checks.append('text_only_PPT_seals_generated_inputs_with_no_live_resource_paths')
    with workflow_api(store,peer=api) as reader:
        reference(reader,pdfid);reference(reader,pptid)
        seed(store,case,'E-report/source.pdf',original+b'\n% changed input')
        reference(reader,pdfid,expect=409);reference(reader,pdfid,current=False)
        assert raw(reader,numbered_path)==numbered
        checks.append('fresh_cache_document_references_reject_changed_sources_and_preserve_history')


def latex(api,store,checks):
    case=new_case(api,'Remote native LaTeX');main='E-report/latex/main.tex'
    api.put(main,MAIN)
    first=None
    for recipe in ('pdflatex','xelatex','lualatex'):
        created=api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':recipe})
        completed(api,created)
        result=api.call('GET',api.prefix+'/latex/results?main='+quote(main,safe=''))
        assert result['publication']['aliases_applied'] and not result['publication']['stale']
        assert 'UniqueResultText' in PdfReader(BytesIO(raw(api,result['pdf']))).pages[1].extract_text()
        if first is None:first=result
        checks.append('remote_'+recipe+'_PDF_SyncTeX_and_pinned_source_publication')
    paper='E-report/latex/paper.tex'
    api.put(paper,COMPLEX);api.put('E-report/shared/refs.bib',BIB)
    api.put('E-report/latex/chapters/detail.tex','Dynamic remote chapter and $E=mc^2$.\n')
    seed(store,case,'E-report/assets/plot.png',PNG)
    created=api.call('POST',api.prefix+'/latex:build',{'main':paper,'recipe':'xelatex'})
    completed(api,created)
    result=api.call('GET',api.prefix+'/latex/results?main='+quote(paper,safe=''))
    assert result['publication']['aliases_applied']
    assert b'demo' in raw(api,created['pdf'].replace('.pdf','.bbl'))
    assert {'E-report/shared/refs.bib','E-report/assets/plot.png','E-report/latex/chapters/detail.tex'}<=set(result['manifest']['source_files'])
    checks.append('remote_biber_shared_image_and_macro_selected_chapter_are_frozen')
    with workflow_api(store,peer=api) as reader:
        archived=first['publication']['id']
        query={'pdf':first['pdf'],'publication_id':archived,'page':2,'x':134,'y':157}
        location=reader.call('POST',api.prefix+'/synctex:query',query)
        assert location['file']==main and location['line']==7 and location['source_current_matches'],location
        api.put(main,'\n\n'+MAIN)
        location=reader.call('POST',api.prefix+'/synctex:query',query)
        assert not location['source_current_matches'] and location['line']==7
        assert reader.call('GET',api.prefix+'/latex/source?publication_id='+archived+'&path='+main,raw=True)==MAIN.encode()
        checks.append('fresh_cache_historical_SyncTeX_keeps_original_source_after_live_edit')
    # A failed subsequent compile must preserve the last valid PDF and history.
    previous=raw(api,first['pdf']);api.put(main,'\\documentclass{article}\n\\begin{document}\n\\undefinedcommand\n\\end{document}')
    bad=api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'pdflatex'})
    failed=settled(api,bad['job_id']);assert failed['status']=='failed'
    diagnostics=api.call('GET',api.prefix+'/latex/errors?job_id='+str(bad['job_id']))
    assert any('Undefined control sequence' in item['message'] for item in diagnostics['items']),diagnostics
    assert raw(api,first['pdf'])==previous
    checks.append('native_compile_failure_preserves_previous_remote_PDF')


def latex_retirement(api,store,checks):
    case=new_case(api,'Remote bibliography retirement');main='E-report/latex/main.tex'
    document=('\\documentclass{article}\n\\begin{document}\nCitation \\cite{demo}.\n'
        '\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n')
    api.put(main,document);api.put('E-report/latex/refs.bib',BIB)
    created=api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'pdflatex'})
    completed(api,created)
    pdf_path=created['pdf'];bbl=pdf_path.replace('.pdf','.bbl')
    previous_pdf=raw(api,pdf_path);previous_bbl=raw(api,bbl)
    assert b'demo' in previous_bbl
    api.put(main,MAIN);seed(store,case,bbl,b'User-edited bibliography')
    created=api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'pdflatex'})
    held=completed(api,created)
    assert held['attempt']['outputs']['completion']['alias_error'] is not None
    assert raw(api,bbl)==b'User-edited bibliography' and raw(api,pdf_path)==previous_pdf
    result=api.call('GET',api.prefix+'/latex/results?main='+quote(main,safe=''))
    assert not result['publication']['aliases_applied']
    frozen=api.call('GET',api.prefix+'/latex/artifact?publication_id='+result['publication']['id']+'&path='+pdf_path,raw=True)
    assert len(PdfReader(BytesIO(frozen)).pages)==2
    checks.append('edited_remote_bibliography_preserves_live_PDF_and_records_unapplied_committed_version')
    seed(store,case,bbl,previous_bbl)
    created=api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'pdflatex'})
    completed(api,created)
    api.call('GET',api.prefix+'/raw?path='+bbl,expect=404)
    result=api.call('GET',api.prefix+'/latex/results?main='+quote(main,safe=''))
    assert result['publication']['aliases_applied'] and not result['publication']['stale']
    checks.append('unchanged_retired_bibliography_is_conditionally_deleted_after_successful_rebuild')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--only',choices=['compose','pdfppt','latex','latex-retirement']);args=parser.parse_args()
    checks=[];apis=[]
    with ObjectStore(executable=RUNTIME) as store:
        for name,verify in [('compose',compose),('pdfppt',pdfppt),('latex',latex),('latex-retirement',latex_retirement)]:
            if args.only and args.only!=name:continue
            with workflow_api(store) as api:
                before=len(checks);verify(api,store,checks);apis.append(str(api.root))
                for check in checks[before:]:print('PASS '+check,flush=True)
            (api.root/'document-section-result.json').write_text(json.dumps({'passed':True,
                'section':name,'count':len(checks)-before,'checks':checks[before:],'api':str(api.root)},indent=2),encoding='utf8')
        result={'passed':True,'count':len(checks),'checks':checks,'apis':apis,'evidence':str(store.root),'only':args.only}
    (store.root/'documents-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('S3_DOCUMENT_WORKFLOWS_OK '+json.dumps(result),flush=True)


if __name__=='__main__':main()
