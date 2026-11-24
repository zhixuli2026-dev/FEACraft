"""S3 engineering evidence flows into compose, Markdown and native LaTeX documents."""
from io import BytesIO
from urllib.parse import quote

from pypdf import PdfReader
from s3_core_workflow_smoke import main as core
from s3_document_workflows_smoke import completed,raw


def main():
    selected={}
    def documents(api,store,phase):
        if phase=='stale':
            latex=api.call('GET',api.prefix+'/latex/results?main='+quote(selected['main'],safe=''))
            compose=api.call('GET',api.prefix+'/report/compose-result?publication_id='+selected['compose'])
            assert latex['publication']['stale'] and compose['publication']['stale']
            assert api.call('GET',api.prefix+'/latex/artifact?publication_id='+selected['latex']+
                '&path='+selected['pdf_path'],raw=True)==selected['pdf']
            return ['geometry_change_stales_compose_and_LaTeX_through_recursive_engineering_lineage']
        checks=[]
        post=api.call('GET',api.prefix+'/postprocess/results')
        post_id=post['publication']['id']
        metric=next(item for item in post['metrics']['metrics'] if item['name']=='mean.T@1')
        created=api.call('POST',api.prefix+'/report:compose?wait=false',{'overwrite':True})
        job=completed(api,created)
        assert any(item['publication_id']==post_id for item in job['attempt']['workspace']['publications'])
        draft=api.call('GET',api.prefix+'/report/compose-result')
        assert draft['publication']['applied'] and str(metric['value']) in draft['content']
        selected['compose']=draft['publication_id']
        checks.append('independent_composition_reads_pinned_engineering_metrics_and_quality')
        created=api.call('POST',api.prefix+'/report:build',{});job=completed(api,created)
        pins={item['publication_id'] for item in job['attempt']['workspace']['publications']}
        assert {post_id,draft['publication_id']}<=pins
        assert str(metric['value']).encode() in raw(api,'E-report/build/report.html')
        checks.append('generated_engineering_Markdown_build_pins_both_composition_and_postprocess')
        plot=next(item['path'] for item in post['manifest']['artifacts'] if item['path'].endswith('.png'))
        main='E-report/latex/engineering.tex'
        api.put(main,r'\documentclass{article}\usepackage{graphicx}\begin{document}Verified thermal results: '+
            str(metric['value'])+r'.\includegraphics[width=100pt]{../../'+plot+r'}\end{document}')
        created=api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'pdflatex'});job=completed(api,created)
        assert any(item['publication_id']==post_id for item in job['attempt']['workspace']['publications'])
        result=api.call('GET',api.prefix+'/latex/results?main='+quote(main,safe=''))
        assert result['publication']['aliases_applied'] and not result['publication']['stale']
        assert plot in result['manifest']['source_files']
        pdf=raw(api,result['pdf']);assert 'Verified thermal results' in PdfReader(BytesIO(pdf)).pages[0].extract_text()
        selected.update(main=main,latex=result['publication']['id'],pdf_path=result['pdf'],pdf=pdf)
        checks.append('native_LaTeX_freezes_engineering_plot_with_actual_postprocess_publication')
        return checks
    core(extra=documents)


if __name__=='__main__':main()
