"""Verify scientific charts, immutable downloads, and reports using actual OpenFOAM."""
import argparse
import base64
from contextlib import nullcontext
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import time
from urllib.parse import urlencode
from xml.etree import ElementTree
from zipfile import ZipFile

import httpx

from app.services import process_identity_native
from app.services.casedict import set_block
from postprocess_gate_streaming_acceptance import (
    FIXTURE, RUNTIME, RUN, configured_case, native_files, put_live, shutdown)
from s3testsupport import ObjectStore
from testsupport import IsolatedServer


HISTORY = 'thermal_history'
PANELS = 'thermal_panels'


def config():
    cold = {'source':'thermalProbes', 'field':'T', 'probe':0, 'label':'Cold-side Point',
            'band': {'unit':'K', 'source':'Declared instrument tolerance', 'halfWidth':[.25,.5]}}
    hot = {'source':'thermalProbes', 'field':'T', 'probe':1, 'label':'Hot-side Point'}
    line = {'source':'thermalLine', 'field':'T', 'time':1, 'label':'Steady Temperature Profile',
            'band': {'unit':'K', 'source':'Declared spatial tolerance', 'halfWidth':.2}}
    return {'runDir':RUN, 'fields':{'T':{'unit':'K'}},
        'dataExtraction':{
            'pointProbes':{'thermalProbes':{'fields':['T'],'points':[.25,.5,.5,.75,.5,.5],'times':[0,1]}},
            'lineSamples':{'thermalLine':{'fields':['T'],'start':[.125,.5,.5],'end':[.875,.5,.5], 'samples':7, 'time':1}},
            'volumeAverages':{'mean':{'field':'T','time':1}}},
        'derivedQuantities':{'referenceValues':{'analytic':{'metric':'"mean.T@1"','value':350,'unit':'K',
            'kind':'verification','source':'"T(x)=300+100*x"','l2_max':1e-6}}},
        'artifacts':{'plots':{
            HISTORY:{'layout':'overlay','title':'Temperature at two probe positions','yLabel':'Temperature',
                     'dpi':600,'series':{'hot':hot,'cold':cold}},
            PANELS:{'layout':'panels','columns':2,'title':'Heat conduction over time and space',
                    'series':{'spatial':line,'temporal':hot}},
            'scalar_baseline':{'source':'thermalProbes','field':'T','dpi':300}}}}


def save_config(api, settings):
    current = api.call('GET', api.prefix+'/caseDict')
    api.call('PUT', api.prefix+'/caseDict', {'content':set_block(current['content'],'postProcessing',settings),
        'expected_revision':current['dict_revision']})


def request(api, method, path, *, status=200, headers=None, **kwargs):
    with httpx.Client(base_url=api.base+'/api/v1',timeout=120,trust_env=False) as client:
        response = client.request(method, path, headers={'Authorization':'Bearer '+api.token, **(headers or {})}, **kwargs)
    if response.status_code != status:
        raise AssertionError((method, path, response.status_code, response.text[:500]))
    return response


def result_for(api, publication=None):
    return request(api,'GET',api.prefix+'/postprocess/results', params={'publication_id':publication} if publication else {}).json()


def postprocess(api):
    return request(api,'POST',api.prefix+'/postprocess:run',json={'run_dir':RUN}).json()['job_id']


def finished(api, job_id, expected='succeeded'):
    job = api.wait_job(job_id, timeout=180, expected=expected)
    deadline = time.monotonic() + 90
    while job['attempt']['phase'] != expected or job.get('completion_pending'):
        if time.monotonic() >= deadline:
            raise TimeoutError('Job did not settle after its terminal status')
        time.sleep(.1)
        job = api.call('GET',f'/jobs/{job_id}')
    assert job['status']==expected
    return job


def chart_url(api, result, name):
    query = urlencode({'publication_id':result['publication']['id'], 'path':result['result_dir']+'/'+name})
    return api.prefix+'/postprocess/artifact?'+query


def archived_charts(api, result):
    manifest = {row['path']:row for row in result['manifest']['artifacts']}
    charts = {row['id']:row for row in result['metrics']['extractions']['charts']}
    assert list(charts[HISTORY]['series'][index]['id'] for index in (0,1)) == ['cold','hot']
    assert charts[HISTORY]['layout']=='overlay' and charts[PANELS]['layout']=='panels'
    assert charts['scalar_baseline']['artifacts']==['scalar_baseline.pdf','scalar_baseline.svg','scalar_baseline.png']
    files = {}
    for name, labels, resolution in (
        (HISTORY, ('Time [s]', 'Temperature [K]', 'Cold-side Point', 'Hot-side Point'), (4080,2400)),
        (PANELS, ('Distance [m]', 'Time [s]', 'T [K]', 'Steady Temperature Profile'), (2040,1110)),
        ('scalar_baseline', ('Time [s]', 'T [K]'), (1800,1200)),
    ):
        assert charts[name]['provenance']['case_dict_sha256']==result['metrics']['source']['sha256']
        for artifact in charts[name]['artifacts']:
            response = request(api,'GET',chart_url(api,result,artifact))
            assert response.headers['content-disposition'].startswith('attachment;')
            receipt = manifest[result['result_dir']+'/'+artifact]
            data = response.content
            assert len(data)==receipt['size'] and hashlib.sha256(data).hexdigest()==receipt['sha256']
            files[artifact] = data
        png = files[name+'.png']
        assert png.startswith(b'\x89PNG\r\n\x1a\n') and struct.unpack('>II',png[16:24])==resolution
        assert files[name+'.pdf'].startswith(b'%PDF-')
        svg = ElementTree.fromstring(files[name+'.svg'])
        text = '\n'.join(svg.itertext())
        assert all(label in text for label in labels) and f"job={result['job_id']}" in text
        if name==PANELS:
            positions = {node.text: float(node.attrib['y']) for node in svg.iter('{http://www.w3.org/2000/svg}text')
                         if node.text and 'y' in node.attrib}
            footer = next(node for node in svg.iter('{http://www.w3.org/2000/svg}text')
                          if node.text and node.text.startswith('case='))
            footer_y = float(re.fullmatch(r'translate\(\S+ (\S+)\)',footer.attrib['transform'])[1])
            assert max(positions['Distance [m]'],positions['Time [s]']) + 8 < footer_y
    svg = files[HISTORY+'.svg'].decode('utf8')
    assert '#0072b2' in svg and '#d55e00' in svg and 'stroke-dasharray' in svg and 'fill-opacity: 0.18' in svg
    return charts, files


def compute_reference(files, result):
    grouped = {}
    for name in (HISTORY,PANELS):
        rows = list(csv.DictReader(io.StringIO(files[name+'.plot.csv'].decode('utf8'))))
        grouped[name] = len(rows)
        for row in rows:
            x, value = map(float,(row['x'],row['y']))
            assert row['field']=='T' and row['component']=='0' and row['y_unit']=='K'
            if row['series']=='spatial':
                assert row['x_unit']=='m' and row['source']=='thermalLine' and float(row['time'])==1
                expected = 300+100*(.125+x)
                half_width = .2
            else:
                probe = 0 if row['series']=='cold' else 1
                assert row['x_unit']=='s' and row['source']=='thermalProbes' and int(row['probe_index'])==probe
                assert x in (0.,1.)
                expected = 300 if x==0 else 325 + probe*50
                half_width = {0.:.25,1.:.5}[x] if probe==0 else None
            assert abs(value-expected)<.01, (row, expected)
            if half_width is None:
                assert not row['lower'] and not row['upper'] and not row['band_source']
            else:
                assert abs(float(row['lower'])-(value-half_width))<1e-9 and abs(float(row['upper'])-(value+half_width))<1e-9
                assert row['band_source'].startswith('Declared ')
    assert grouped=={HISTORY:4,PANELS:9}, grouped
    metrics = result['metrics']
    mean = next(item['value'] for item in metrics['metrics'] if item['name']=='mean.T@1')
    assert abs(mean-350)<1e-6 and metrics['readiness']['report_ready']
    assert metrics['readiness']['can_claim_verification'] and not metrics['readiness']['can_claim_validation']
    return grouped, mean


def reports(api, result, files):
    compose = request(api,'POST',api.prefix+'/report:compose?wait=false',json={'overwrite':True}).json()
    finished(api,compose['job_id'])
    composed = request(api,'GET',api.prefix+'/report/compose-result',params={'publication_id':compose['publication_id']}).json()
    figures = next(row['figures'] for row in composed['chapter_schema'] if row['id']=='results')
    by_id = {row['id']:row for row in figures}
    for name in (HISTORY,PANELS):
        figure = by_id[name]
        link = f"{result['result_dir']}/{name}.plot.csv"
        assert figure['data_path']==link and link in composed['content']
        assert figure['data_sha256']==hashlib.sha256(files[name+'.plot.csv']).hexdigest()
        assert f"../{figure['path']}" in composed['content']
        assert figure['sha256']==hashlib.sha256(files[name+'.png']).hexdigest()
    job = request(api,'POST',api.prefix+'/report:build').json()
    finished(api,job['job_id'])
    artifact_url = api.prefix+'/report/artifact'
    publication = job['publication_id']
    html = request(api,'GET',artifact_url,params={'publication_id':publication,'path':'E-report/build/report.html'}).content
    docx = request(api,'GET',artifact_url,params={'publication_id':publication,'path':'E-report/build/report.docx'}).content
    html_hashes = {hashlib.sha256(base64.b64decode(encoded)).hexdigest()
                   for encoded in re.findall(rb'data:image/png;base64,([A-Za-z0-9+/=]+)',html)}
    with ZipFile(io.BytesIO(docx)) as document:
        docx_hashes = {hashlib.sha256(document.read(name)).hexdigest()
                       for name in document.namelist() if name.startswith('word/media/')}
    expected = {hashlib.sha256(files[name+'.png']).hexdigest() for name in (HISTORY,PANELS)}
    assert expected <= html_hashes and expected <= docx_hashes, (expected,html_hashes,docx_hashes)
    return {'compose_publication':compose['publication_id'],'build_publication':publication,
            'html_bytes':len(html),'docx_bytes':len(docx),'embedded_hashes':sorted(expected)}


def storage_idle(api):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = api.call('GET',api.prefix+'/file-transactions',expect=(200,409))
        if status.get('status')=='idle':
            return
        assert status.get('status')=='busy' or status.get('detail',{}).get('code')=='file_conflict', status
        time.sleep(.25)
    raise TimeoutError('Terminal job still owns a live storage operation')


def verify(api, store):
    settings = config()
    _, binding, _ = configured_case(api,'Publication grade heat conduction charts', settings)
    for path, source in native_files(api.root).items():
        put_live(api, store, binding['prefix'], path, source)
    job_id = postprocess(api)
    finished(api,job_id)
    result = result_for(api)
    assert result['job_id']==job_id and not result['publication']['stale']
    charts, files = archived_charts(api,result)
    rows, mean = compute_reference(files,result)
    for name in (HISTORY,PANELS):
        for suffix in ('.png','.svg','.plot.csv'):
            (api.root/(name+suffix)).write_bytes(files[name+suffix])
    name = HISTORY+'.plot.csv'
    url = chart_url(api,result,name)
    version = request(api,'HEAD',url).headers['x-vibeflow-file-version']
    partial = request(api,'GET',url,status=206,headers={'Range':'bytes=5-27','X-Vibeflow-File-Version':version})
    assert partial.content==files[name][5:28]
    request(api,'GET',url,status=409,headers={'X-Vibeflow-File-Version':'f'*64})
    request(api,'GET',chart_url(api,result,'metrics.json'),status=404)
    outsider = api.call('POST','/auth/register',{'email':'chart-outsider@example.com','name':'Other account',
        'password':'isolated-test-123'})
    request(api,'GET',url,status=404,headers={'Authorization':'Bearer '+outsider['token']})
    report = reports(api,result,files)
    print('CHART_REPORTS_PASSED', api.root, flush=True)

    invalid = deepcopy(settings)
    invalid['artifacts']['plots'][PANELS].pop('columns')
    invalid['artifacts']['plots'][PANELS]['layout'] = 'overlay'
    save_config(api,invalid)
    rejected = finished(api,postprocess(api),expected='failed')
    assert '叠加曲线的横纵轴单位必须一致' in rejected['log_backlog']
    if store:
        storage_idle(api)
    historical = result_for(api,result['publication']['id'])
    assert historical['publication']['stale'] and historical['metrics']==result['metrics']
    assert request(api,'GET',url).content==files[name]
    save_config(api,settings)

    cancelled_id = postprocess(api)
    running = api.call('GET',f'/jobs/{cancelled_id}')
    assert running['status']=='running', running['status']
    api.call('POST',f'/jobs/{cancelled_id}:cancel')
    cancelled = finished(api,cancelled_id,expected='cancelled')
    saved = cancelled['attempt']['outputs']
    assert saved.get('publication',{}).get('status')!='committed'
    assert saved['process_stop']['verified'] and saved['process_stop']['scope_empty']
    if store:
        storage_idle(api)
    assert request(api,'GET',url).content==files[name]
    return binding, result, {'passed':True,'real_openfoam':True,'real_model_requests':0,'mock_llm_calls':0,
        'reports':report,'chart_csv_rows':rows,'mean_K':mean,'publ_artifacts':sorted(files),'old_job_id':job_id,
        'legacy_single_trace':charts['scalar_baseline'], 'actual_job_cancelled':True,'csv_range':True,
        'mixed_unit_validation':True,'historical_file_permissions':True}


def run(backend):
    api = IsolatedServer('publication-charts-'+backend, app='fixture:app')
    response = {'passed':False,'backend':backend,'real_model_requests':0,'mock_llm_calls':0}
    print('PUBLICATION_CHARTS',api.root,flush=True)
    remote = ObjectStore(executable=RUNTIME,authenticated=True) if backend=='s3' else nullcontext()
    try:
        with remote as store:
            overrides = store.settings() if store else {}
            (api.root/'fixture.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+FIXTURE,encoding='utf8')
            with api:
                binding, initial, evidence = verify(api,store)
                email = api.call('GET','/auth/me')['email']
                shutdown(api,binding['api_process'])
            assert process_identity_native.probe(binding['api_process'])['state']=='exited'
            with api:
                api.token = api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
                proof = api.call('GET',f'/fixture/postprocess/{api.case_id}/binding',api_prefix=False)
                new_id = postprocess(api)
                finished(api,new_id)
                after = result_for(api)
                assert after['job_id']==new_id and after['publication']['generation']>initial['publication']['generation']
                assert after['publication']['id']!=initial['publication']['id'] and not after['publication']['stale']
                old_name = HISTORY+'.plot.csv'
                assert request(api,'GET',chart_url(api,initial,old_name)).content==(api.root/old_name).read_bytes()
                new_bytes = request(api,'GET',chart_url(api,after,old_name)).content
                assert new_bytes==(api.root/old_name).read_bytes()
                shutdown(api,proof['api_process'])
            assert process_identity_native.probe(proof['api_process'])['state']=='exited'
            response.update(evidence, cold_restart=True, new_job_id=new_id, api_processes_exited=True)
        if backend=='s3':
            assert store.proc.poll() is not None
            response['store_exited']=True
    except BaseException as error:
        response.update(passed=False,failure={'type':type(error).__name__,'message':str(error)[:1000]})
        raise
    finally:
        (api.root/'chart-result.json').write_text(json.dumps(response,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('PUBLICATION_CHARTS_PASSED',api.root,flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=('fs','s3'),required=True)
    run(parser.parse_args().backend)
