"""Real S3 geometry/mesh/solve/extraction/report workflow with pinned dependencies."""
import argparse
from contextlib import nullcontext
import json
import math
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from app.services.casedict import set_block
from cae_benchmarks import thermal_files
from geometry_publication_smoke import script
from professional_launch_smoke import settled
from s3_typed_publication_smoke import api_for,RUNTIME
from s3testsupport import ObjectStore
from testsupport import SERVER


def workflow_api(store,peer=None):
    api=api_for(store,peer)
    original=api.opener.open
    timings=[]
    def observed(request,*args,**kwargs):
        # Full recursive remote provenance is synchronous during admission.
        # Measure it separately from the native job timeout; never retry writes.
        kwargs['timeout']=120
        started=time.monotonic()
        try:return original(request,*args,**kwargs)
        finally:
            timings.append({'method':request.get_method(),'path':request.selector,
                'seconds':round(time.monotonic()-started,3)})
            (api.root/'http-timings.json').write_text(json.dumps(timings,indent=2),encoding='utf8')
    api.opener.open=observed
    return api


def wait(api,identifier,confirmation=None):
    deadline=time.monotonic()+240
    while time.monotonic()<deadline:
        flow=api.call('GET',f'/workflow-runs/{identifier}')
        (api.root/'workflow-observed.json').write_text(json.dumps(flow,indent=2),encoding='utf8')
        if confirmation and any(s['key']==confirmation and s['status']=='awaiting_confirmation' for s in flow['steps']):return flow
        if not confirmation and flow['status']=='succeeded':return flow
        if flow['status'] in {'failed','cancelled','interrupted'}:
            for step in flow['steps']:
                for attempt in step['attempts']:
                    job=api.call('GET',f'/jobs/{attempt["job_id"]}')
                    (api.root/('job-'+str(job['id'])+'-observed.json')).write_text(json.dumps(job,indent=2),encoding='utf8')
            raise AssertionError([(s['key'],s['status'],s['attempts']) for s in flow['steps']])
        time.sleep(.2)
    raise TimeoutError([(s['key'],s['status']) for s in flow['steps']])


def main(extra=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--fs',action='store_true',help='Run the same real workflow on the default filesystem backend')
    args=parser.parse_args()
    checks=[]
    if args.fs:
        directory=Path(tempfile.mkdtemp(prefix='fs-core-workflow-',dir=SERVER.parents[1]/'tmp'))
        context=nullcontext(SimpleNamespace(root=directory,settings=lambda:{
            'VIBEFLOW_STORAGE_BACKEND':'fs','VIBEFLOW_STORAGE_FS_ROOT':str(directory/'files')}))
    else:context=ObjectStore(executable=RUNTIME)
    with context as store:
        with workflow_api(store) as api:
            api.create_case('S3 formal conduction report')
            saved=api.call('GET',api.prefix+'/caseDict');run='C-run/conduction'
            text=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':{'maxNonOrtho':70,'policy':'fail'}})
            text=set_block(text,'numericalMethod',{'solver':'laplacianFoam'})
            post={'runDir':run,'fields':{'T':{'unit':'K'}},'dataExtraction':{
                'volumeAverages':{'mean':{'field':'T','time':1}},
                'lineSamples':{'profile':{'fields':['T'],'start':[.05,.5,.5],'end':[.95,.5,.5],'nPoints':19,'times':[1]}}},
                'derivedQuantities':{'referenceValues':{'analytic_mean':{'metric':'"mean.T@1"','value':350,
                    'unit':'K','kind':'verification','source':'"analytic linear conduction"','l2_max':1e-6}}},
                'artifacts':{'plots':{'temperature':{'source':'profile','field':'T','time':1,'formats':['png'],'title':'"Temperature"'}}}}
            text=set_block(text,'postProcessing',post)
            api.call('PUT',api.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
            for path,content in thermal_files().items():api.put(path,content.replace('(20 2 2)','(20 4 4)'))
            preview=api.put('A-geo/geometry.py',script(2))
            assert preview.get('auto_preview_job'),preview
            before=settled(api,preview['auto_preview_job']);assert before['status']=='succeeded',before
            checks.append('S3_registered_auto_preview_commits_geometry')
            saved=api.call('GET',api.prefix+'/caseDict')
            flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'report','run_dir':run,
                'expected_revision':saved['dict_revision'],'parameters':{'geometry_mode':'export','report_mode':'compose'},
                'budget':{'timeout_seconds':180,'max_log_bytes':4*1024*1024}})
            for key in ('confirm_B','confirm_C','confirm_D','confirm_E'):
                pending=wait(api,flow['id'],key)
                step=next(s for s in pending['steps'] if s['key']==key)
                if key=='confirm_E':assert next(s for s in pending['steps'] if s['key']=='report')['status']=='pending'
                confirmation=api.call('POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
                    'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
                (api.root/(key+'-response.json')).write_text(json.dumps(confirmation,indent=2),encoding='utf8')
                assert confirmation['workflow_dispatch']['status']=='dispatched',confirmation['workflow_dispatch']
                checks.append(key+'_requires_pinned_professional_publication')
                print('PASS '+checks[-1],flush=True)
            finished=wait(api,flow['id'])
            for step in finished['steps']:
                for attempt in step['attempts']:
                    job=settled(api,attempt['job_id']);assert job['status']=='succeeded',step
                    if not args.fs:assert job['attempt']['workspace']['storage']['backend']=='s3'
                    assert job['attempt']['outputs']['publication']['status']=='committed'
            mesh=api.call('GET',api.prefix+'/mesh/results')
            solve=api.call('GET',api.prefix+'/solve/results')
            post=api.call('GET',api.prefix+'/postprocess/results')
            report=api.call('GET',api.prefix+'/report/results')
            assert post['metrics']['readiness']['report_ready'] and post['metrics']['readiness']['can_claim_verification']
            metric=next(m for m in post['metrics']['metrics'] if m['name']=='mean.T@1')
            assert math.isclose(metric['value'],350,abs_tol=1e-6),metric
            assert all(not value['publication']['stale'] for value in (mesh,solve,post,report))
            html=api.call('GET',api.prefix+'/raw?path=E-report/build/report.html',raw=True)
            docx=api.call('GET',api.prefix+'/raw?path=E-report/build/report.docx',raw=True)
            assert b'mean.T@1' in html and str(metric['value']).encode() in html and docx.startswith(b'PK')
            checks.append('real_formal_geometry_export_mesh_solve_analytic_extraction_and_Pandoc_report')
            if extra:
                checks.extend(extra(api,store,'published'))
                report=api.call('GET',api.prefix+'/report/results')
            with workflow_api(store,peer=api) as reader:
                for kind in ('mesh','solve','postprocess','report'):
                    result=reader.call('GET',api.prefix+'/'+kind+'/results')
                    assert not result['publication']['stale'],kind
                ref={'command':'echo FROZEN_FORMAL_REPORT','input_publications':[{'publication_id':report['publication']['id'],
                    'paths':['E-report/build/report.html'],'require_current':True}]}
                job=reader.call('POST',api.prefix+'/jobs',ref);assert settled(reader,job['id'])['status']=='succeeded'
                checks.append('fresh_cache_recursive_professional_results_and_report_reference')
                api.put('A-geo/geometry.py',script(3))
                for kind in ('mesh','solve','postprocess','report'):
                    assert reader.call('GET',api.prefix+'/'+kind+'/results')['publication']['stale'],kind
                if extra:checks.extend(extra(reader,store,'stale'))
                reader.call('POST',api.prefix+'/jobs',ref,expect=409)
                ref['input_publications'][0]['require_current']=False
                job=reader.call('POST',api.prefix+'/jobs',ref);assert settled(reader,job['id'])['status']=='succeeded'
                checks.append('geometry_change_invalidates_recursive_current_lineage_but_preserves_history')
            result={'passed':True,'backend':'fs' if args.fs else 's3','count':len(checks),'checks':checks,
                'evidence':str(store.root),'api':str(api.root),'reader':str(reader.root),
                'http_timeout_seconds':120,'timings':'http-timings.json'}
    (store.root/'core-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('S3_CORE_WORKFLOW_OK '+json.dumps(result),flush=True)


if __name__=='__main__':main()
