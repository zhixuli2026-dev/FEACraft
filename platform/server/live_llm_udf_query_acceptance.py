"""Actual wide include root, dynamic Make, response and absent-file inputs."""
import asyncio,hashlib,json,time,uuid
from pathlib import Path
from urllib.parse import quote
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_workflow_plan_migration_acceptance import submit
from live_llm_udf_cancel_recovery_acceptance import confirm,drain,solution
from live_llm_failure_fallback_acceptance import kill_owned_api
import live_llm_udf_workflow_acceptance as bridge

FIXTURE=bridge.FIXTURE


def scenario(api,*,store=None,**_):
    (api.root/'query-entry-source.py').write_bytes(Path(__file__).read_bytes())
    api.create_case('Actual wide-root UDF file query provenance')
    prefix=api.prefix;run_dir='C-run/case-1';cpp='UDF/myFunctionObject/myFunctionObject.C';checks=[]
    source=bridge.peer(api,store,'udf-query-source');recovery=bridge.peer(api,store,'udf-query-recovery')
    parameters={'geometry_mode':'export','solve_mode':'udf','udf_name':'myFunctionObject'}
    budget={'timeout_seconds':180,'max_log_bytes':16777216}
    def record(value):
        checks.append(value);(api.root/'udf-query-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    for server,label in [(api,'main'),(source,'source'),(recovery,'recovery')]:
        original=server.opener.open
        def observed(request,*args,_original=original,_label=label,**kwargs):
            started=time.monotonic();failure=None
            try:return _original(request,*args,**kwargs)
            except BaseException as error:failure=type(error).__name__;raise
            finally:
                with (api.root/'udf-query-http.jsonl').open('a',encoding='utf8') as stream:
                    stream.write(json.dumps({'role':_label,'method':request.get_method(),'path':request.selector,
                        'seconds':round(time.monotonic()-started,4),'error':failure})+'\n')
        server.opener.open=observed
    def cleanup():
        for job in api.call('GET',prefix+'/jobs'):
            if job['status'] in {'queued','running'}:
                api.call('POST','/jobs/'+str(job['id'])+':cancel',{})
                api.wait_job(job['id'],expected='cancelled',timeout=120);drain(api,job['id'])
    def graph(server):return {r['kind']:r for r in server.call('GET',prefix+'/dependencies?run_dir='+quote(run_dir))['channels']}
    def validate(server,flow,values):
        proof=solution(server,api.root,flow,'UDF_QUERY_RUN')
        log=server.call('GET',prefix+'/raw?path='+quote(run_dir+'/udf-solve.log'),raw=True).decode()
        assert ('QUERY_VALUES_'+'_'.join(map(str,values))) in log
        job=server.call('GET','/jobs/'+str(proof['build_job']))
        manifest=server.call('GET','/testing/workflow-publication/'+job['attempt']['outputs']['publication']['id'])['manifest']
        native=manifest['descriptor']['native'];compiler=manifest['descriptor']['source']['compiler']
        evidence=native['receipt']['native']['compiler_dependencies']['file_query_evidence']
        assert evidence['records'] and 0<evidence['bytes']<=67108864 and evidence['events']>0
        assert compiler['policy']['query_schema']=='vibeflow.udf-file-queries.v2'
        assert compiler['policy']['search_roots']==[]
        for path in ['RootHeader.H','DynamicInputs/value.txt','DynamicInputs/compiler.rsp','DynamicInputs/compiler.mk']:
            assert compiler['files'][path]==native['source']['files'][path]
        assert compiler['entries']['OptionalHeader.H']==('file' if values[3] else 'missing')
        assert compiler['entries']['DynamicInputs/optional-dir']==('directory' if values[5] else 'missing')
        assert compiler['entries']['GeneratedHeader.H']==('file' if values[6]==32 else 'missing')
        if values[6]==31:assert 'GeneratedHeader.H' in compiler['policy']['absence_dependencies']
        assert len(compiler['listings']['DynamicInputs/fragments'])==values[4]
        channels=graph(server);assert all(channels[k]['status']=='fresh' for k in ['geometry','mesh','udf-build','solve'])
        assert hashlib.sha256(server.call('GET',prefix+'/raw?path='+quote(cpp),raw=True)).hexdigest()==cpp_sha
        record({'stage':'actual_query_build_solve','values':list(values),'proof':proof,'compiler':compiler,
            'actual_process_trace_files':len(evidence['records']),'trace_bytes':evidence['bytes'],'trace_events':evidence['events'],
            'wide_include_root_without_output_or_confirmation_self_staleness':True,'main_CXX_unchanged':True})
        return server.call('GET','/workflow-runs/'+flow['id']),proof['library_sha256']
    with source:
        try:
            dictionary=source.call('GET',prefix+'/caseDict')
            text=set_block(dictionary['content'],'mesh',{'source':'blockMesh','qualityTargets':
                {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            text=set_block(text,'numericalMethod',{'solver':'icoFoam'});text=set_block(text,'postProcessing',{'runDir':run_dir})
            write(source,'PUT',prefix+'/caseDict',{'content':text,'expected_revision':dictionary['dict_revision']})
            for path,value in cavity_2d().items():put(source,path,value)
            source.call('POST',prefix+'/udf/scaffold',{'name':'myFunctionObject','objective':'Actual dynamic compiler file inputs'})
            original=source.call('GET',prefix+'/raw?path='+quote(cpp),raw=True).decode()
            text=('#include "RootHeader.H"\n#include "GeneratedHeader.H"\n#if __has_include("OptionalHeader.H")\n#include "OptionalHeader.H"\n'
                '#else\n#define VF_OPTIONAL_VALUE 0\n#endif\n'+original.replace('P0_UDF_OK','UDF_QUERY_RUN'))
            text=text.replace('Info << "UDF_QUERY_RUN" << endl;','Info << "UDF_QUERY_RUN" << endl;\n'
                '            Info << "QUERY_VALUES_" << VF_ROOT_VALUE << "_" << VF_DYNAMIC_VALUE << "_" << VF_RESPONSE_VALUE << "_" << VF_OPTIONAL_VALUE << "_" << VF_ENUM_VALUE << "_" << VF_CD_VALUE << "_" << VF_GENERATED_VALUE << endl;')
            put(source,cpp,text);cpp_sha=hashlib.sha256(text.encode()).hexdigest()
            options=source.call('GET',prefix+'/raw?path='+quote('UDF/myFunctionObject/Make/options'),raw=True).decode()
            options+='\ninclude ../../DynamicInputs/compiler.mk\n'
            put(source,'UDF/myFunctionObject/Make/options',options)
            put(source,'DynamicInputs/compiler.mk',
                'VF_PREPARE_GENERATED := $(shell sh ../../DynamicInputs/generate.sh)\n'
                'EXE_INC += -I../../ -DVF_DYNAMIC_VALUE=$(shell cat ../../DynamicInputs/value.txt) @../../DynamicInputs/compiler.rsp '
                '-DVF_ENUM_VALUE=$(words $(wildcard ../../DynamicInputs/fragments/*.txt)) '
                '-DVF_CD_VALUE=$(shell if cd ../../DynamicInputs/optional-dir 2>/dev/null; then echo 1; else echo 0; fi)\n')
            put(source,'DynamicInputs/generate.sh','if [ ! -e ../../GeneratedHeader.H ]; then printf "#define VF_GENERATED_VALUE 31\\n" > ../../GeneratedHeader.H; fi\n')
            put(source,'RootHeader.H','#define VF_ROOT_VALUE 1\n')
            put(source,'DynamicInputs/value.txt','11\n');put(source,'DynamicInputs/compiler.rsp','-DVF_RESPONSE_VALUE=21\n')
            put(source,'DynamicInputs/fragments/a.txt','actual directory member A\n')
            saved=put(source,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
            if saved.get('auto_preview_job'):source.wait_job(saved['auto_preview_job'])
            dictionary=source.call('GET',prefix+'/caseDict')
            flow=source.call('POST',prefix+'/workflow-runs',{'target':'solve','expected_revision':dictionary['dict_revision'],
                'parameters':parameters,'budget':budget})
            confirm(source,api.root,flow['id'],'confirm_B');confirm(source,api.root,flow['id'],'confirm_C')
            history,digest=validate(source,flow,[1,11,21,0,1,0,31])
            before=[j['id'] for j in source.call('GET',prefix+'/jobs')]
            pid=source.call('GET','/testing/workflow-api-owner')['pid'];launcher=kill_owned_api(source,pid)
            record({'stage':'actual_source_API_killed','pid':pid,'verified_launcher_pid':launcher})
        finally:cleanup()
    if store:assert not (recovery.root/'s3-cache').exists()
    with recovery:
        try:
            assert recovery.call('GET','/workflow-runs/'+flow['id'])==history
            assert [j['id'] for j in recovery.call('GET',prefix+'/jobs')]==before
            put(recovery,run_dir+'/diagnostics/unrelated.txt','Actual unqueried engineering diagnostic.\n')
            channels=graph(recovery);assert all(channels[k]['status']=='fresh' for k in ['mesh','udf-build','solve'])
            assert [j['id'] for j in recovery.call('GET',prefix+'/jobs')]==before
            record({'stage':'restarted_and_unrelated_output','independent_empty_S3_cache':bool(store),
                'unqueried_engineering_file_does_not_stale_wide_root':True,'no_jobs_replayed_or_created':True})
            values=[1,11,21,0,1,0,31]
            for change in ['dynamic_Make_read','compiler_response_file','absent_header_added','enumerated_directory_added','wide_root_header','conditional_chdir','generated_header_replaced_by_input']:
                if change=='dynamic_Make_read':values[1]=12;put(recovery,'DynamicInputs/value.txt','12\n')
                elif change=='compiler_response_file':values[2]=22;put(recovery,'DynamicInputs/compiler.rsp','-DVF_RESPONSE_VALUE=22\n')
                elif change=='absent_header_added':values[3]=1;put(recovery,'OptionalHeader.H','#define VF_OPTIONAL_VALUE 1\n')
                elif change=='enumerated_directory_added':values[4]=2;put(recovery,'DynamicInputs/fragments/b.txt','actual directory member B\n')
                elif change=='wide_root_header':values[0]=2;put(recovery,'RootHeader.H','#define VF_ROOT_VALUE 2\n')
                elif change=='conditional_chdir':values[5]=1;put(recovery,'DynamicInputs/optional-dir/member.txt','actual directory presence\n')
                else:values[6]=32;put(recovery,'GeneratedHeader.H','#define VF_GENERATED_VALUE 32\n')
                channels=graph(recovery);assert all(channels[k]['status']=='fresh' for k in ['geometry','mesh'])
                assert all(channels[k]['status']=='stale' for k in ['udf-build','solve'])
                dictionary=recovery.call('GET',prefix+'/caseDict');key=uuid.uuid4().hex
                body={'expected_version':history['version'],'expected_revision':dictionary['dict_revision'],
                    'confirmed_reset':True,'target':'solve','parameters':parameters,'budget':budget}
                fresh=submit(recovery,'/workflow-runs/'+flow['id']+':recompute',body,key)
                assert fresh['recomputation']['rebuild']==['udf-build','solve'] and set(fresh['recomputation']['reused'])=={'geometry','mesh'}
                confirm(recovery,api.root,fresh['id'],'confirm_B');confirm(recovery,api.root,fresh['id'],'confirm_C')
                new_history,new_digest=validate(recovery,fresh,values);assert new_digest!=digest
                assert recovery.call('GET','/workflow-runs/'+flow['id'])==history
                assert submit(recovery,'/workflow-runs/'+flow['id']+':recompute',body,key)['id']==fresh['id']
                record({'stage':'query_change_recomputed','change':change,'only_library_and_solve_rebuilt':True,
                    'new_library_sha256':new_digest,'old_history_unchanged':True,'same_key_same_run':True})
                flow,history,digest=fresh,new_history,new_digest
        finally:cleanup()
    report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,
        'scope':'Actual wide-root file lookups, dynamic Make reads, response files, missing header probes and directory enumeration; complete runtime/environment and remaining mainline matrix continue.'}
    (api.root/'udf-file-queries.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    api.create_case('Independent actual compiler file-query evidence readback')
    return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
