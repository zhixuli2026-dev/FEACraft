"""Actual external-header, Make, object and include-shadow UDF recomputation."""
import asyncio,base64,hashlib,json,subprocess,time,uuid
from pathlib import Path
from urllib.parse import quote
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_workflow_plan_migration_acceptance import submit
from live_llm_udf_cancel_recovery_acceptance import waiting,confirm,drain,solution
import live_llm_udf_workflow_acceptance as bridge

EXTRA_FIXTURE=r'''
import base64,hashlib
from app.services.case_write_capacity import committing
from app.services import file_transactions

@app.post('/api/v1/cases/{case_id}/testing/udf-link-input')
async def udf_link_input(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True)
 data=base64.b64decode(body['data'],validate=True)
 assert data.startswith(b'\x7fELF') and len(data)<1048576 and hashlib.sha256(data).hexdigest()==body['sha256']
 async with committing(db,case):await file_transactions.write(db,case,{'LocalLibraries/helper.o':data})
 return {'actual_object_sha256':hashlib.sha256(data).hexdigest()}
'''
FIXTURE=bridge.FIXTURE+EXTRA_FIXTURE

COMPILE=r'''
import base64,hashlib,json,os,subprocess,tempfile
from pathlib import Path
assert os.getuid()!=0
value=int(__import__('sys').argv[1]);assert 0<=value<=100
with tempfile.TemporaryDirectory(prefix='vibeflow-udf-dep-') as directory:
 root=Path(directory);source=root/'helper.C';target=root/'helper.o'
 source.write_text('extern "C" int external_value() { return '+str(value)+'; }\n')
 result=subprocess.run(['/usr/bin/g++','-fPIC','-c',str(source),'-o',str(target)],capture_output=True,timeout=15)
 assert result.returncode==0,result.stderr.decode()
 data=target.read_bytes();assert data.startswith(b'\x7fELF')
 print(json.dumps({'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest(),
  'actual_gcc_returncode':result.returncode,'uid':os.getuid(),'value':value,
  'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
  'compiler_sha256':hashlib.sha256(Path('/usr/bin/g++').read_bytes()).hexdigest()}))
'''


def object_input(api,value):
    result=subprocess.run(['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec','python3','-I','-S','-c',COMPILE,str(value)],
        capture_output=True,timeout=25,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0,result.stderr.decode('utf8','replace')
    proof=json.loads(result.stdout)
    assert proof['actual_gcc_returncode']==0 and proof['uid']!=0
    saved=write(api,'POST',api.prefix+'/testing/udf-link-input',{'data':proof['data'],'sha256':proof['sha256']})
    assert saved['actual_object_sha256']==proof['sha256']
    proof.pop('data');(api.root/('actual-object-'+str(value)+'.json')).write_text(json.dumps(proof,indent=2),encoding='utf8')
    return proof


def scenario(api,*,store=None,**_):
    (api.root/'dependency-entry-source.py').write_bytes(Path(__file__).read_bytes())
    api.create_case('Actual user-local UDF compiler dependency freshness')
    case_id=api.case_id;case_prefix=api.prefix;checks=[];run_dir='C-run/case-1'
    parameters={'geometry_mode':'export','solve_mode':'udf','udf_name':'myFunctionObject'}
    source=bridge.peer(api,store,'udf-dependency-source');recovery=bridge.peer(api,store,'udf-dependency-recovery')
    for server in [source,recovery]:
        with (server.root/'agent_peer_fixture.py').open('a',encoding='utf8') as stream:stream.write(EXTRA_FIXTURE)
    def record(row):
        checks.append(row);(api.root/'udf-dependency-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    for server,label in [(api,'main'),(source,'source'),(recovery,'recovery')]:
        original=server.opener.open
        def observed(request,*args,_original=original,_label=label,**kwargs):
            started=time.monotonic();failure=None
            try:return _original(request,*args,**kwargs)
            except BaseException as error:failure=type(error).__name__;raise
            finally:
                with (api.root/'udf-dependency-http.jsonl').open('a',encoding='utf8') as stream:
                    stream.write(json.dumps({'role':_label,'method':request.get_method(),'path':request.selector,
                        'seconds':round(time.monotonic()-started,4),'error':failure})+'\n')
        server.opener.open=observed
    def cleanup():
        for job in api.call('GET',case_prefix+'/jobs'):
            if job['status'] in {'queued','running'}:
                api.call('POST','/jobs/'+str(job['id'])+':cancel',{})
                api.wait_job(job['id'],expected='cancelled',timeout=120);drain(api,job['id'])
    def graph(server):
        return {r['kind']:r for r in server.call('GET',case_prefix+'/dependencies?run_dir='+quote(run_dir))['channels']}
    def validate(server,flow,values):
        proof=solution(server,api.root,flow,'UDF_DEPENDENCY_RUN')
        transcript=server.call('GET',case_prefix+'/raw?path='+quote(run_dir+'/udf-solve.log'),raw=True).decode()
        assert ('DEPENDENCY_'+ '_'.join(map(str,values))) in transcript
        job=server.call('GET','/jobs/'+str(proof['build_job']))
        publication=job['attempt']['outputs']['publication']
        manifest=server.call('GET','/testing/workflow-publication/'+publication['id'])['manifest']
        descriptor=manifest['descriptor'];compiler=descriptor['source']['compiler']
        required=['LocalHeaders/second/external.H','LocalConfig/flags.inc','LocalConfig/compiler.mk','LocalLibraries/helper.o']
        assert all(p in compiler['files'] for p in required)
        assert all(compiler['files'][p]==descriptor['native']['source']['files'][p] for p in required)
        native=descriptor['native']['receipt']['native']['compiler_dependencies']
        assert native['compiler_dependency_records'] and len(native['make_preprocessor_probes'])==2
        assert native['files']==compiler['files'] and not native['complete_runtime_closure']
        channels=graph(server);assert all(channels[k]['status']=='fresh' for k in ['geometry','mesh','udf-build','solve'])
        assert hashlib.sha256(server.call('GET',case_prefix+'/raw?path='+quote(cpp),raw=True)).hexdigest()==cpp_sha
        record({'stage':'actual_compile_load_solve','values':values,'proof':proof,'compiler':compiler,
            'actual_dependency_files':native['compiler_dependency_records'],'Make_preprocessors':native['make_preprocessor_probes'],
            'outside_inputs_bound_to_original_seal':True,'main_CXX_unchanged':True,'current_producers_fresh':True})
        return server.call('GET','/workflow-runs/'+flow['id']),proof['library_sha256']
    with source:
        try:
            saved=source.call('GET',case_prefix+'/caseDict')
            text=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
                {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            text=set_block(text,'numericalMethod',{'solver':'icoFoam'})
            text=set_block(text,'postProcessing',{'runDir':run_dir})
            write(source,'PUT',case_prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
            for path,text in cavity_2d().items():put(source,path,text)
            source.call('POST',case_prefix+'/udf/scaffold',{'name':'myFunctionObject','objective':'Actual external compiler dependencies'})
            cpp='UDF/myFunctionObject/myFunctionObject.C'
            original=source.call('GET',case_prefix+'/raw?path='+quote(cpp),raw=True).decode()
            text='#include "external.H"\nextern "C" int external_value();\n'+original.replace('P0_UDF_OK','UDF_DEPENDENCY_RUN')
            text=text.replace('Info << "UDF_DEPENDENCY_RUN" << endl;','Info << "UDF_DEPENDENCY_RUN" << endl;\n'
                '            Info << "DEPENDENCY_" << VF_EXTERNAL_VALUE << "_" << VF_MAKE_VALUE << "_" << external_value() << "_" << VF_GNUMAKE_VALUE << endl;')
            put(source,cpp,text);cpp_sha=hashlib.sha256(text.encode()).hexdigest()
            options=source.call('GET',case_prefix+'/raw?path='+quote('UDF/myFunctionObject/Make/options'),raw=True).decode()
            options='#include "../../../LocalConfig/flags.inc"\n'+options+'\nEXE_INC += -DVF_MAKE_VALUE=VF_MAKE_NUMBER\ninclude ../../LocalConfig/compiler.mk\n'
            put(source,'UDF/myFunctionObject/Make/options',options)
            put(source,'LocalHeaders/second/external.H','#define VF_EXTERNAL_VALUE 11\n')
            put(source,'LocalConfig/flags.inc','#define VF_MAKE_NUMBER 21\n')
            make='EXE_INC += -I../../LocalHeaders/first -I../../LocalHeaders/second -DVF_GNUMAKE_VALUE={value}\nLIB_LIBS += ../../LocalLibraries/helper.o\n'
            put(source,'LocalConfig/compiler.mk',make.format(value=51));object_input(source,41)
            saved=put(source,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
            if saved.get('auto_preview_job'):source.wait_job(saved['auto_preview_job'])
            dictionary=source.call('GET',case_prefix+'/caseDict')
            flow=source.call('POST',case_prefix+'/workflow-runs',{'target':'solve','expected_revision':dictionary['dict_revision'],
                'parameters':parameters,'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
            confirm(source,api.root,flow['id'],'confirm_B');confirm(source,api.root,flow['id'],'confirm_C')
            history,digest=validate(source,flow,[11,21,41,51])
            old_jobs=[j['id'] for j in source.call('GET',case_prefix+'/jobs')]
            pid=source.call('GET','/testing/workflow-api-owner')['pid']
            from live_llm_failure_fallback_acceptance import kill_owned_api
            launcher=kill_owned_api(source,pid)
            record({'stage':'baseline_API_actually_killed','actual_API_pid':pid,'verified_launcher_pid':launcher})
        finally:cleanup()
    if store:assert not (recovery.root/'s3-cache').exists()
    with recovery:
        try:
            assert recovery.call('GET','/workflow-runs/'+flow['id'])==history
            assert [j['id'] for j in recovery.call('GET',case_prefix+'/jobs')]==old_jobs
            channels=graph(recovery);assert all(channels[k]['status']=='fresh' for k in ['mesh','udf-build','solve'])
            put(recovery,'Unrelated/unused.txt','This file is outside all observed compiler inputs and searches.\n')
            channels=graph(recovery);assert all(channels[k]['status']=='fresh' for k in ['mesh','udf-build','solve'])
            record({'stage':'restart_and_unrelated_input','independent_empty_S3_cache':bool(store),
                'old_history_unchanged':True,'no_automatic_Jobs':True,'unrelated_input_does_not_stale_library':True})
            values=[11,21,41,51]
            for change in ['header','cpp_Make_include','GNU_Make_include','link_object','include_shadow']:
                if change=='header':values[0]=12;put(recovery,'LocalHeaders/second/external.H','#define VF_EXTERNAL_VALUE 12\n')
                elif change=='cpp_Make_include':values[1]=22;put(recovery,'LocalConfig/flags.inc','#define VF_MAKE_NUMBER 22\n')
                elif change=='GNU_Make_include':values[3]=52;put(recovery,'LocalConfig/compiler.mk',make.format(value=52))
                elif change=='link_object':values[2]=42;object_input(recovery,42)
                else:values[0]=31;put(recovery,'LocalHeaders/first/external.H','#define VF_EXTERNAL_VALUE 31\n')
                channels=graph(recovery)
                assert all(channels[k]['status']=='fresh' for k in ['geometry','mesh'])
                assert all(channels[k]['status']=='stale' for k in ['udf-build','solve'])
                assert recovery.call('GET','/workflow-runs/'+flow['id'])==history
                dictionary=recovery.call('GET',case_prefix+'/caseDict')
                body={'expected_version':history['version'],'expected_revision':dictionary['dict_revision'],'confirmed_reset':True,
                    'target':'solve','parameters':parameters,'budget':{'timeout_seconds':180,'max_log_bytes':16777216}}
                key=uuid.uuid4().hex;fresh=submit(recovery,'/workflow-runs/'+flow['id']+':recompute',body,key)
                assert fresh['recomputation']['rebuild']==['udf-build','solve'] and set(fresh['recomputation']['reused'])=={'geometry','mesh'}
                confirm(recovery,api.root,fresh['id'],'confirm_B');confirm(recovery,api.root,fresh['id'],'confirm_C')
                new_history,new_digest=validate(recovery,fresh,list(values));assert new_digest!=digest
                assert submit(recovery,'/workflow-runs/'+flow['id']+':recompute',body,key)['id']==fresh['id']
                assert recovery.call('GET','/workflow-runs/'+flow['id'])==history
                record({'stage':'outside_change_recomputed','change':change,'new_library_sha256':new_digest,
                    'only_library_and_solve_rebuilt':True,'main_CXX_unchanged':True,'same_key_same_run':True,'old_history_unchanged':True})
                flow,history,digest=fresh,new_history,new_digest
        finally:cleanup()
    report={'passed':True,'backend':'s3' if store else 'fs','case_id':case_id,'mock_llm_calls':0,'checks':checks,
        'scope':'Actual sealed local compiler/Make/link inputs and search-directory additions. Managed runtime closure, broad-root search policy, all UDF types and typed batches remain.'}
    (api.root/'udf-dependencies.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    api.create_case('Independent real compiler dependency evidence readback')
    return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
