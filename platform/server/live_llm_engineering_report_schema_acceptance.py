"""Run real OpenFOAM, VTK and Pandoc through the engineering report routes."""
import asyncio
from functools import partial
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode
import zipfile


CHAPTERS = ['problem','assumptions','geometry','mesh','methods','results','verification','limitations','evidence']


def wait_for_s3_response(api, store):
    if store is not None:
        open_request = api.opener.open

        def open_with_wait(request, *, timeout):
            return open_request(request,timeout=max(timeout,100))

        api.opener.open = open_with_wait


def scenario(api, *, baseline, store=None, **_):
    from live_llm_remote_post_report_acceptance import scenario as run_report
    from sqlalchemy.engine import make_url

    wait_for_s3_response(api,store)
    selected = make_url(api.database_url) if api.database_url else None
    measured = asyncio.run(run_report(api,selected=selected))
    content = {}
    drafts = {}
    for output in ('E-report/report.md','E-report/report.generated.md'):
        draft = api.call('GET',api.prefix+'/report/compose-result?'+urlencode({'output':output}))
        assert draft['chapters']==CHAPTERS
        assert re.findall(r'^## .*? \{#([a-z]+)\}$',draft['content'],re.M)==CHAPTERS
        drafts[output] = draft
        content[output] = draft['content']
    plot = next(row for row in measured['artifacts'] if row['file']=='temperature.png')
    report = build(api)
    html, docx = download(api,report)
    if baseline:
        assert all('![图 ' not in item for item in content.values())
        assert all(plot['path'] in item for item in content.values())
        assert b'<img ' not in html
        with zipfile.ZipFile(io.BytesIO(docx)) as archive:
            assert not any(name.startswith('word/media/') for name in archive.namelist())
        evidence = {'source_mentions_plot':True,'inline_figure':False,'chapter_schema':None,
                    'plot_frozen_input':plot['path'] in report['manifest']['inputs']}
    else:
        png = (api.root/'temperature.png').read_bytes()
        assert hashlib.sha256(png).hexdigest()==plot['sha256']
        for draft in drafts.values():
            assert draft['template']['id']=='thermal' and draft['template']['analysis']=='steady'
            assert [chapter['id'] for chapter in draft['chapter_schema']]==CHAPTERS
            assert len(draft['chapter_schema'][5]['figures'])==1
            assert draft['chapter_schema'][5]['figures'][0]['path']==plot['path']
            assert f'](../{plot["path"]})' in draft['content']
            assert '温度场与热传递结果' in draft['content']
        verify_embedded(html,docx,png)
        assert report['manifest']['inputs'][plot['path']]['sha256']==plot['sha256']
        evidence = {'source_mentions_plot':True,'inline_figure':True,
                    'chapter_schema':drafts['E-report/report.md']['chapter_schema'],'plot_frozen_input':True}
        evidence['template_resources'] = verify_template_resources(api,plot,docx)
    evidence.update(passed=True,baseline=baseline,temperature_k=measured['mean_temperature_k'],
                    publication=report['publication']['id'],plot=plot)
    (api.root/'engineering-report-schema.json').write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n',
                                                          encoding='utf-8')
    return evidence


def verify_embedded(html,docx,png):
    from base64 import b64encode
    assert b'<img ' in html and b'data:image/png;base64,'+b64encode(png) in html
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        assert png in [archive.read(name) for name in archive.namelist() if name.startswith('word/media/')]


def verify_css(html):
    from base64 import b64decode
    pending = [html]
    for _ in range(32):
        if any(b'rgb(17, 53, 96)' in item for item in pending):
            return
        pending = [b64decode(value) for item in pending
                   for value in re.findall(rb'data:text/css;charset=utf-8;base64,([A-Za-z0-9+/=]+)',item)]
        if not pending:
            break
    raise AssertionError('Nested CSS rule was not in the self-contained HTML')


def compose(api, output, *, expect=200):
    return api.call('POST',api.prefix+'/report:compose?wait=false',{'output':output,'overwrite':True},expect=expect)


def verify_template_resources(api, plot, template_docx):
    import httpx
    from live_llm_job_repeat_acceptance import put, write
    dictionary = api.prefix+'/caseDict/block'
    output = 'E-report/report.md'
    root_css = 'E-report/styles/report.css'
    child_css = 'E-report/styles/nested/colors.css'
    template_path = 'E-report/templates/reference.docx'
    settings = {'template':'general','css':'styles/report.css','referenceDoc':'templates/reference.docx'}
    invalid = api.call('PUT',dictionary,{'block':'report','entries':{'template':'unknown'}},expect=422)
    assert 'report.template' in str(invalid)
    write(api,'PUT',dictionary,{'block':'report','entries':settings})
    refusal = compose(api,output,expect=400)
    assert 'report.css' in refusal['detail']
    put(api,root_css,'@import "nested/colors.css";\n')
    child_content = 'h1 { color: rgb(17, 53, 96); }\n'
    put(api,child_css,child_content)
    with httpx.Client(timeout=60,trust_env=False) as client:
        response = client.post(api.base+'/api/v1'+api.prefix+'/upload',
            params={'path':template_path},headers={'Authorization':'Bearer '+api.token},
            files={'file':('reference.docx',template_docx,'application/octet-stream')})
    assert response.status_code==200, response.text[:250]
    template_sha = hashlib.sha256(template_docx).hexdigest()
    frozen = {}
    for path in (output,'E-report/report.generated.md'):
        receipt = compose(api,path)
        job = api.wait_job(receipt['job_id'],timeout=240)
        previous = api.call('GET',api.prefix+'/report/compose-result?'+urlencode({'output':path}))
        assert previous['template']['id']=='general' and previous['template']['analysis']=='steady'
        assert '后处理结果' in previous['content'] and previous['publication']['applied']
        inputs = job['attempt']['workspace']['files']
        assert inputs[child_css]['sha256']==hashlib.sha256(child_content.encode()).hexdigest()
        assert inputs[plot['path']]['sha256']==plot['sha256']
        assert inputs[template_path]['sha256']==template_sha
        assert root_css in inputs
        frozen[path] = receipt['publication_id']
    report = build(api)
    html,docx = download(api,report,prefix='schema-template')
    verify_css(html)
    verify_embedded(html,docx,(api.root/'temperature.png').read_bytes())
    assert {root_css,child_css,plot['path'],template_path} <= report['manifest']['inputs'].keys()
    assert report['manifest']['inputs'][template_path]['sha256']==template_sha
    put(api,child_css,'h1 { color: rgb(96, 53, 17); }\n')
    stale = api.call('GET',api.prefix+'/report/compose-result?'+urlencode({'publication_id':frozen[output]}))
    assert stale['publication']['stale']
    old = api.call('GET',api.prefix+'/report/results?'+urlencode({'publication_id':report['publication']['id']}))
    assert old['publication']['stale']
    assert download(api,old,prefix='schema-prior')==(html,docx)
    return {'missing_refused':True,'invalid_template_refused':True,'both_drafts_frozen':frozen,
            'template_docx_sha256':template_sha,'nested_css_change_invalidates':True,
            'old_export_unchanged':True,'compiled_publication':report['publication']['id']}


def registered(api, *, store=None, **_):
    from live_llm_case_artifact_trace_acceptance import registered_chain
    from live_llm_job_repeat_acceptance import put

    wait_for_s3_response(api,store)
    put(api,'E-report/styles/report.css','@import "nested/colors.css";\n')
    put(api,'E-report/styles/nested/colors.css','h1 { color: rgb(17, 53, 96); }\n')
    def configure(blocks):
        blocks['report'] = {'template':'thermal','css':'styles/report.css'}
        post = blocks['postProcessing']
        post['dataExtraction']['lineSamples'] = {'profile':{'fields':['T'],'start':[.05,.5,.5],
            'end':[.95,.5,.5],'samples':19,'times':[1]}}
        post['artifacts'] = {'plots':{'temperature':{'source':'profile','field':'T','formats':['png','svg'],
                                                      'title':'"Actual temperature profile"'}}}
    workflow = registered_chain(api,configure=configure)
    version = workflow['traces']['report']['publication']['id']
    report = api.call('GET',api.prefix+'/report/results?'+urlencode({'publication_id':version}))
    engineering = report['manifest']['engineering_report']
    assert engineering['chapter_schema_version']==1 and engineering['template']['id']=='thermal'
    assert [chapter['id'] for chapter in engineering['chapter_schema']]==CHAPTERS
    figure, = engineering['chapter_schema'][5]['figures']
    data = api.call('GET',api.prefix+'/raw?'+urlencode({'path':figure['path']}),raw=True)
    assert hashlib.sha256(data).hexdigest()==figure['sha256']
    assert report['manifest']['inputs'][figure['path']]['sha256']==figure['sha256']
    assert 'E-report/styles/nested/colors.css' in report['manifest']['inputs']
    html,docx = download(api,report)
    verify_css(html)
    verify_embedded(html,docx,data)
    result = {'passed':True,'registered_workflow':workflow['real_registered_workflow'],'publication':version,
              'engineering_report':engineering,'registered_template_inputs':list(report['manifest']['inputs'])}
    (api.root/'engineering-report-schema.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',
                                                          encoding='utf-8')
    return result


def build(api):
    job = api.call('POST',api.prefix+'/report:build',{})
    api.wait_job(job['job_id'],timeout=240)
    return api.call('GET',api.prefix+'/report/results?'+urlencode({'publication_id':job['publication_id']}))


def download(api, report, *, prefix='schema-report'):
    def artifact(path):
        query = urlencode({'publication_id':report['publication']['id'],'path':'E-report/build/report.'+path})
        data = api.call('GET',api.prefix+'/report/artifact?'+query,raw=True)
        (api.root/(prefix+'.'+path)).write_bytes(data)
        return data
    return artifact('html'),artifact('docx')


def verify_browser(path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as toolkit:
        browser = toolkit.chromium.launch(channel='msedge')
        page = browser.new_page(viewport={'width':1300,'height':940})
        external = []
        page.on('request',lambda request: external.append(request.url) if request.url.startswith(('http://','https://')) else None)
        page.goto(path.as_uri(),wait_until='load')
        image = page.locator('figure img').first
        image.wait_for(state='visible')
        report = image.evaluate("""image => ({width:image.naturalWidth,height:image.naturalHeight,alt:image.alt})""")
        assert report['width']>100 and report['height']>100, report
        assert 'Actual temperature profile' in report['alt'], report
        assert page.locator('h1').first.evaluate('(node) => getComputedStyle(node).color')=='rgb(17, 53, 96)'
        heading = page.locator('h2#results')
        assert '温度场与热传递结果' in heading.inner_text()
        rendered_widths = {}
        for width in (1300,390):
            page.set_viewport_size({'width':width,'height':940})
            box = image.bounding_box()
            assert box and box['x'] >= 0 and box['x']+box['width']<=width+1, box
            rendered_widths[width] = box['width']
            heading.evaluate("(node) => node.scrollIntoView({block:'start'})")
            page.screenshot(path=str(path.with_name(f'engineering-report-{width}-edge.png')))
        assert not external, external
        report.update(passed=True,rendered_widths=rendered_widths,external_requests=0,edge=browser.version)
        path.with_name('engineering-report-figure-edge.json').write_text(
            json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        browser.close()


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--browser-report':
        verify_browser(Path(sys.argv[2]).resolve(strict=True))
        sys.exit()
    from live_llm_job_repeat_acceptance import main
    from s3testsupport import ObjectStore, SERVER

    baseline = '--baseline' in sys.argv
    if baseline:
        sys.argv.remove('--baseline')
    selected = partial(scenario,baseline=baseline)
    if '--registered' in sys.argv:
        sys.argv.remove('--registered')
        selected = registered
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=selected,
                     store_factory=lambda:ObjectStore(executable=binary,volume_max=8,authenticated=True)))
