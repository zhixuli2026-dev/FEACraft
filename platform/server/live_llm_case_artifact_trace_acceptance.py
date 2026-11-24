"""Run real engineering jobs and check how their publications trace to caseDict."""
import asyncio
from functools import partial
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def scenario(api, *, baseline, browser_dist, store=None, **_):
    from live_llm_remote_post_report_acceptance import scenario as solve_and_report
    from sqlalchemy.engine import make_url
    dictionary_path = api.prefix+'/caseDict'
    origin = api.call('GET', dictionary_path)
    provenance = origin['parsed']['provenance']
    if not baseline:
        genesis = api.call('GET', api.prefix+'/provenance')['events'][0]
        assert genesis['source']=='case-created' and genesis['actor_id']>0 and genesis['revision']==0
        assert genesis['casedict_sha256']==hashlib.sha256(origin['content'].encode()).hexdigest()
        assert genesis['provenance']['artifactTrace']['registry']=='casePublications'
        assert provenance['createdBy']==str(genesis['actor_id']) and provenance['createdAt']
    declared = {**provenance,
        'sources': {**provenance.get('sources',{}),'reference':{'kind':'analytic','title':'Thermal reference'}},
        'assumptions':{'constantMaterial':'Uniform'},
        'decisions':{'referenceChoice':'Use analytic result'},
        'artifactTrace':{**provenance.get('artifactTrace',{}),'legacyNote':'Keep the declared notes'},
    }
    api.call('PUT', dictionary_path+'/block', {'block':'provenance','entries':declared})
    if not baseline:
        managed = api.call('GET',dictionary_path)['parsed']['provenance']
        assert managed['createdBy']==provenance['createdBy'] and managed['createdAt']==provenance['createdAt']
        assert managed['updatedAt']>provenance['updatedAt']
    database = make_url(api.database_url) if api.database_url is not None else None
    actual = asyncio.run(solve_and_report(api, selected=database))
    drafts = [job for job in actual['jobs'] if job['attempt']['outputs']['publication']['kind'] in
              {'report-source','report-source-generated'}]
    assert len(drafts)==2
    selected = drafts[0]['attempt']
    binding = selected['outputs']['publication']
    dictionary = selected['workspace']['files']['0-caseDict/caseDict']
    history = api.call('GET',api.prefix+'/provenance')
    events = api.call('GET',api.prefix+'/workflow-events?limit=200')['events']
    publish, = [event for event in events if event['type']=='artifact.published'
                and event['payload']['publication_id']==binding['id']]
    report = {'passed':True,'baseline':baseline,'actual_temperature_k':actual['mean_temperature_k'],
              'report_publication_id':binding['id'],'actual_dictionary_sha256':dictionary['sha256']}
    if baseline:
        assert 'provenance' not in history['events'][0]
        assert 'provenance' not in publish['payload']
        api.call('GET',api.prefix+'/provenance/artifacts',expect=404)
        report.update(declared_sources_in_revision=False, automatic_artifact_trace=False)
    else:
        assert history['events'][0]['provenance']['sources']['reference']['kind']=='analytic'
        assert history['events'][0]['actor_id']==genesis['actor_id']
        trace = api.call('GET',api.prefix+'/provenance/artifacts/'+binding['id'])
        assert trace['publication']['id']==binding['id']
        assert trace['publication']['manifest_sha256']==binding['manifest_sha256']
        assert trace['source']['case_dict']['sha256']==dictionary['sha256']
        upstream, = trace['source']['input_publications']
        assert upstream['kind']=='postprocess'
        upstream_trace = api.call('GET',api.prefix+'/provenance/artifacts/'+upstream['publication_id'])
        assert upstream_trace['publication']['manifest_sha256']==upstream['manifest_sha256']
        assert trace['proof']['event_id']==publish['event_id']
        assert trace['source']==publish['payload']['provenance']
        revision = api.call('GET',trace['case_revision_query'])['matched_revision']
        assert revision['casedict_sha256']==dictionary['sha256']
        assert revision['revision']==selected['workspace']['source']['dict_revision']
        assert revision['provenance']['decisions']['referenceChoice']=='Use analytic result'
        wrong = trace['case_revision_query'].replace(dictionary['sha256'], '0'*64)
        assert api.call('GET',wrong)['matched_revision'] is None
        seen, cursor = set(), None
        while True:
            lookup = api.prefix+'/provenance/artifacts?limit=2'
            page = api.call('GET',lookup+(f'&before_id={cursor}' if cursor else ''))
            assert not seen.intersection(item['id'] for item in page['items'])
            seen.update(item['id'] for item in page['items'])
            cursor = page['next_before_id']
            if cursor is None:
                break
        assert binding['id'] in seen and upstream['publication_id'] in seen and len(seen)==len(actual['jobs'])
        workspace = api.call('GET',api.prefix)['workspace_id']
        foreign_case = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Other artifact trace case'})
        foreign_lookup = f"/cases/{foreign_case['id']}/provenance/artifacts"
        api.call('GET', foreign_lookup+'/'+binding['id'], expect=404)
        api.call('GET', foreign_lookup+'?before_id='+binding['id'], expect=404)
        unaffiliated = api.call('POST','/auth/register',{'email':'trace-outsider@example.com',
            'name':'Trace outsider','password':'isolated-test-123'})
        credentials = {'Authorization':'Bearer '+unaffiliated['token']}
        api.call('GET',api.prefix+'/provenance/artifacts',expect=404,extra_headers=credentials)
        organization = api.call('GET', '/org/mine')['org']['id']
        api.call('POST',f'/org/{organization}/members',{'email':'trace-outsider@example.com','role':'viewer'},expect=201)
        api.call('POST',f'/org/{organization}/invitation:accept',{},extra_headers=credentials)
        share = {'org_id':organization,'role':'viewer','enabled':True}
        api.call('PUT',f'/workspaces/{workspace}/sharing',share)
        assert api.call('GET',api.prefix+'/provenance/artifacts/'+binding['id'],
                        extra_headers=credentials)==trace
        assert api.call('GET',trace['case_revision_query'],extra_headers=credentials)['matched_revision']==revision
        api.call('PUT',f'/workspaces/{workspace}/sharing',{**share,'enabled':False})
        api.call('GET',api.prefix+'/provenance/artifacts/'+binding['id'],expect=404,extra_headers=credentials)

        verify_archive_corruption(api, store, binding['id'])
        assert api.call('GET',api.prefix+'/provenance/artifacts/'+binding['id'])==trace
        original_dict = api.call('GET',dictionary_path)
        edited = {**declared, 'decisions':{'referenceChoice':'Keep the verified comparison'}}
        change = api.call('PUT',dictionary_path+'/block',
            {'block':'provenance','entries':edited,'expected_revision':original_dict['dict_revision']})
        draft = api.call('POST',api.prefix+'/report:compose?wait=false',
                         {'output':'E-report/report.md','overwrite':True},expect=(200,202))
        newer = api.wait_job(draft['job_id'],timeout=180)
        new_id = newer['attempt']['outputs']['publication']['id']
        replacement = api.call('GET',api.prefix+'/provenance/artifacts/'+new_id)
        previous = api.call('GET',api.prefix+'/provenance/artifacts/'+binding['id'])
        assert previous['source']==trace['source'] and previous['proof']==trace['proof']
        assert not previous['publication']['active'] and replacement['publication']['active']
        assert replacement['publication']['generation']==previous['publication']['generation']+1
        assert api.call('GET',previous['case_revision_query'])['matched_revision']==revision
        new_revision = api.call('GET',replacement['case_revision_query'])['matched_revision']
        assert new_revision['revision']==change['dict_revision']
        assert new_revision['provenance']['decisions']['referenceChoice']=='Keep the verified comparison'
        if browser_dist is not None:
            native = ['C:/ProgramData/anaconda3/python.exe','-B','-X','utf8',
                      str(Path(__file__).resolve()),'--trace-browser']
            browser_settings = {'base':api.base,'token':api.token,'name':api.call('GET',api.prefix)['name'],
                'old_job':drafts[0]['id'],'dictionary_sha256':dictionary['sha256'],
                'alternate_name':foreign_case['name'],'root':str(api.root)}
            subprocess.run(native,input=json.dumps(browser_settings),text=True,timeout=100,check=True)
            from live_llm_failure_fallback_acceptance import kill_owned_api
            from live_llm_outbox_restart_acceptance import restart
            old_pid = int((api.root/'trace-api-pid').read_text(encoding='utf-8'))
            kill_owned_api(api,old_pid)
            restart(api)
            assert int((api.root/'trace-api-pid').read_text(encoding='utf-8')) != old_pid
            assert api.call('GET',api.prefix+'/provenance/artifacts/'+binding['id'])==previous
            assert api.call('GET',previous['case_revision_query'])['matched_revision']==revision
        report.update(declared_sources_in_revision=True, automatic_artifact_trace=True,
                      genesis_verified=True, exact_revision_match=True, case_scope_verified=True,
                      manifest_rejected_and_restored=True, historical_generation_verified=True,
                      shared_viewer_verified=True, browser_preview=bool(browser_dist),
                      restart_verified=bool(browser_dist), published_items=len(seen)+1)
    (api.root/'case-artifact-trace.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


def verify_archive_corruption(api, store, identifier):
    prefix = api.call('GET',api.prefix)['storage_prefix']
    relative = prefix.rstrip('/')+'/.vibeflow/publications/'+identifier+'/manifest.json'
    if store is None:
        target = (api.root/'files'/relative).resolve(strict=True)
        assert target.is_relative_to(api.root.resolve())
        original = target.read_bytes()
        write = target.write_bytes
    else:
        client = store.raw_client
        output = client.get_object(Bucket=store.bucket,Key=relative)
        with output['Body'] as stream:
            original = stream.read()

        def write(data):
            client.put_object(Bucket=store.bucket,Key=relative,Body=data)
    changed = original+b' '
    try:
        write(changed)
        rejected = api.call('GET',api.prefix+'/provenance/artifacts/'+identifier,expect=409)
        assert rejected['detail']['code']=='evidence_invalid'
    finally:
        write(original)


def verify_legacy_receipt(baseline_root):
    import shutil
    import sqlite3
    from testsupport import IsolatedServer, SERVER

    baseline_root = baseline_root.resolve(strict=True)
    assert baseline_root.is_relative_to(SERVER.parents[1]/'tmp')
    baseline = json.loads((baseline_root/'case-artifact-trace.json').read_text(encoding='utf-8'))
    assert baseline['passed'] and baseline['baseline']
    identifier = baseline['report_publication_id']
    api = IsolatedServer('case-artifact-legacy')
    with sqlite3.connect((baseline_root/'test.sqlite').as_uri()+'?mode=ro',uri=True) as original:
        case_id, prefix, email = original.execute(
            'SELECT c.id,c.s3_prefix,u.email FROM artifact_publications a JOIN cases c ON c.id=a.case_id '
            'JOIN workspaces w ON w.id=c.workspace_id JOIN users u ON u.id=w.user_id WHERE a.id=?',
            (identifier,)).fetchone()
        with sqlite3.connect(api.root/'test.sqlite') as temporary:
            original.backup(temporary)
    source = (baseline_root/'files'/prefix).resolve(strict=True)
    destination = (api.root/'files'/prefix).resolve()
    assert source.is_relative_to(baseline_root/'files') and destination.is_relative_to(api.root/'files')
    for relative in ('0-caseDict', '.vibeflow/publications/'+identifier):
        shutil.copytree(source/relative,destination/relative)
    with api:
        account = api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})
        api.token = account['token']
        trace = api.call('GET',f'/cases/{case_id}/provenance/artifacts/{identifier}')
        assert trace['proof'] and not trace['proof']['source_recorded']
        assert trace['source']['case_dict']['sha256']==baseline['actual_dictionary_sha256']
        matched = api.call('GET',trace['case_revision_query'])['matched_revision']
        assert matched['casedict_sha256']==baseline['actual_dictionary_sha256']
    evidence = {'passed':True,'archived_baseline':baseline_root.name,'original_artifact':identifier,
                'original_commit_event':trace['proof']['event_id'],'legacy_trace_verified':True,
                'model_calls':0,'mock_llm_calls':0,'api_exited':api.proc.poll() is not None}
    (api.root/'legacy-artifact-trace.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
    print('LEGACY_ARTIFACT_TRACE_OK',api.root,flush=True)


def registered_chain(api, *, configure=None, **_):
    from app.services.casedict import set_block
    from cae_benchmarks import thermal_files
    from live_llm_job_repeat_acceptance import put, write

    run_dir = 'C-run/conduction'
    dictionary = api.prefix+'/caseDict'
    saved = api.call('GET',dictionary)
    actor = api.call('GET',api.prefix+'/provenance')['events'][0]['actor_id']
    content = saved['content']
    blocks = {
        'caseMeta': {'discipline':'thermal','analysisType':'steady','dimensions':'3D',
                     'units':{'length':'m','temperature':'K'}},
        'geometry': {'generator':'cadquery','parameters':{'lx':1,'ly':1,'lz':1}},
        'mesh': {'source':'blockMesh','qualityTargets':{'policy':'fail','maxNonOrtho':70,
                                                       'maxSkewness':4,'maxAspectRatio':100}},
        'numericalMethod': {'solver':'laplacianFoam'},
        'postProcessing': {'runDir':run_dir,'fields':{'T':{'unit':'K'}},
            'dataExtraction':{'volumeAverages':{'mean':{'field':'T','time':1}}},
            'derivedQuantities':{'referenceValues':{'linear_solution':{'metric':'"mean.T@1"',
                'value':350,'unit':'K','kind':'verification','source':'"Analytic linear solution"','l2_max':1e-6}}}},
    }
    if configure is not None:
        configure(blocks)
    for key, value in blocks.items():
        content = set_block(content,key,value)
    write(api,'PUT',dictionary,{'content':content,'expected_revision':saved['dict_revision']})
    for path, data in thermal_files().items():
        put(api,path,data.replace('(20 2 2)','(10 4 2)') if path.endswith('/blockMeshDict') else data)
    geometry = "import cadquery as cq\ndef build_geometry(ctx):\n return cq.Workplane('XY').box(1,1,1,centered=False)\n"
    saved_geometry = put(api,'A-geo/geometry.py',geometry)
    if saved_geometry.get('auto_preview_job'):
        api.wait_job(saved_geometry['auto_preview_job'])
    workflow = write(api,'POST',api.prefix+'/workflow-runs',{'target':'report','run_dir':run_dir,
        'parameters':{'geometry_mode':'export','report_mode':'compose'},
        'expected_revision':api.call('GET',dictionary)['dict_revision'],
        'budget':{'timeout_seconds':240,'max_log_bytes':16777216}})
    url = '/workflow-runs/'+workflow['id']
    confirmations = []
    until = time.monotonic()+560
    while time.monotonic()<until:
        state = api.call('GET',url)
        if state['status']=='succeeded':
            break
        assert state['status'] not in {'failed','cancelled','interrupted'}, state['status']
        ready = next((step for step in state['steps'] if step['status']=='awaiting_confirmation'),None)
        if ready:
            for step in state['steps']:
                if step['status']=='succeeded' and step['attempts']:
                    api.wait_job(step['attempts'][-1]['job_id'])
            write(api,'POST',dictionary+':advance',{'state':ready['confirmation_state'],'confirmed':True,
                'expected_revision':ready['confirmation_revision'],'workflow_step_id':ready['id'],
                'expected_step_version':ready['version']})
            confirmations.append(ready['confirmation_state'])
        time.sleep(.2)
    else:
        raise TimeoutError('Registered trace workflow did not settle')
    assert confirmations==['B','C','D','E']
    traces = {}
    for step in state['steps']:
        if not step['attempts']:
            continue
        job = api.wait_job(step['attempts'][-1]['job_id'])
        native = job['attempt']
        publication = native['outputs']['publication']
        trace = api.call('GET',api.prefix+'/provenance/artifacts/'+publication['id'])
        assert trace['proof']['source_recorded'] and trace['publication']['manifest_sha256']==publication['manifest_sha256']
        archive = native['workspace']
        assert trace['source']['case_dict']['revision']==archive['source']['dict_revision']
        if '0-caseDict/caseDict' in archive['files']:
            assert trace['source']['case_dict']['sha256']==archive['files']['0-caseDict/caseDict']['sha256']
            match = api.call('GET',trace['case_revision_query'])['matched_revision']
            assert match['actor_id']==actor
        traces[step['key']] = trace
    ordered = ['geometry','mesh','solve','postprocess','report']
    assert set(traces)==set(ordered)
    for parent, child in zip(ordered,ordered[1:]):
        upstream = next(row for row in traces[child]['source']['input_publications']
                        if row['publication_id']==traces[parent]['publication']['id'])
        assert upstream['manifest_sha256']==traces[parent]['publication']['manifest_sha256']
    quality = json.loads(api.call('GET',api.prefix+'/raw?path=B-mesh/quality.json',raw=True))
    assert quality['mesh_ok'] and quality['status']=='pass'
    report = {'passed':True,'real_registered_workflow':workflow['id'],'confirmations':confirmations,
              'traces':traces}
    (api.root/'registered-artifact-trace.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


async def trace_browser(settings):
    import re
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as toolkit:
        browser = await toolkit.chromium.launch(channel='msedge')
        context = await browser.new_context(viewport={'width':1500,'height':950},locale='zh-CN')
        origin, token = (json.dumps(settings[key]) for key in ('base','token'))
        await context.add_init_script(f"if (location.origin === {origin}) localStorage.setItem('vf_token', {token});")
        page = await context.new_page()
        expect.set_options(timeout=25000)
        await page.goto(settings['base'])
        await page.locator('[data-case-name='+json.dumps(settings['name'])+']').click()
        panel = page.locator('details.case-artifact-trace')
        if not await panel.is_visible():
            tab = page.locator('.dv-tab:visible').filter(has_text=re.compile('^caseDict$'))
            if await tab.is_visible():
                await tab.click()
        await expect(panel).to_be_visible()
        await panel.locator('summary').focus()
        await panel.locator('summary').press('Space')
        draft = panel.get_by_role('button',name=re.compile(rf"^报告草稿 · 作业 #{settings['old_job']} ·"))
        await expect(draft).to_contain_text('历史版本')
        await draft.click()
        detail = panel.get_by_role('region',name='产物来源')
        await expect(detail).to_contain_text(settings['dictionary_sha256'])
        await detail.get_by_role('button',name='核对算例修订').click()
        await expect(detail).to_contain_text('Use analytic result')
        await detail.locator('details > summary').click()
        await expect(detail).to_contain_text('/report.md')
        await detail.screenshot(path=str(Path(settings['root'])/'case-artifact-trace-report.png'))
        await page.set_viewport_size({'width':1040,'height':780})
        await expect(detail).to_contain_text(settings['dictionary_sha256'])
        assert await detail.evaluate('(node) => node.scrollWidth <= node.clientWidth + 1')
        await detail.get_by_role('button',name=re.compile('^后处理 · 版本')).click()
        await expect(detail.get_by_text('后处理 · 版本 1',exact=True)).to_be_visible()
        await detail.get_by_role('button',name='返回上一产物').click()
        await expect(detail).to_contain_text(settings['dictionary_sha256'])
        await page.locator('[data-case-name='+json.dumps(settings['alternate_name'])+']').click()
        await expect(panel.locator('summary')).to_be_visible()
        await panel.locator('summary').click()
        await expect(panel).to_contain_text('算例产生已发布的产物后')
        await browser.close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--trace-browser']:
        asyncio.run(trace_browser(json.load(sys.stdin)))
        sys.exit()
    if len(sys.argv)==3 and sys.argv[1]=='--trace-legacy':
        verify_legacy_receipt(Path(sys.argv[2]))
        sys.exit()
    from live_llm_job_repeat_acceptance import main
    from s3testsupport import ObjectStore, SERVER
    baseline = '--trace-baseline' in sys.argv
    if baseline:
        sys.argv.remove('--trace-baseline')
    registered = '--trace-registered' in sys.argv
    if registered:
        sys.argv.remove('--trace-registered')
    browser_dist = None
    fixture = "\nimport os\n(root/'trace-api-pid').write_text(str(os.getpid()),encoding='utf-8')\n"
    if '--trace-browser-dist' in sys.argv:
        index = sys.argv.index('--trace-browser-dist')
        browser_dist = Path(sys.argv[index+1]).resolve(strict=True)
        assert (browser_dist/'index.html').is_file()
        del sys.argv[index:index+2]
        fixture += ('\nfrom fastapi.staticfiles import StaticFiles\n'
                   f"app.mount('/', StaticFiles(directory={str(browser_dist)!r}, html=True))\n")
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    selected = registered_chain if registered else partial(scenario,baseline=baseline,browser_dist=browser_dist)
    asyncio.run(main(selected_override=selected,fixture_override=fixture,
                     store_factory=lambda:ObjectStore(executable=binary, volume_max=8, authenticated=True)))
