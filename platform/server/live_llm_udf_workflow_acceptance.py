"""Real wmake/load/UDF solve workflow, restart, downstream and source rebuild.

The final two model calls use the authorized service for independent readback.
Native compilation, library loading and solver replies are never substituted.
"""
import ast,asyncio,hashlib,json,time,uuid
from pathlib import Path
from urllib.parse import quote

from cae_fixtures import cavity_2d
from app.services.casedict import set_block
import live_llm_mpi_workflow_acceptance as bridge
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_workflow_plan_migration_acceptance import submit
from live_llm_workflow_batch_professional_acceptance import confirmed_dispatch_pending_kind

FIXTURE=bridge.FIXTURE


def peer(api,store,role):
    value=bridge.peer(api,store,role)
    if store:
        fixture=value.root/'agent_peer_fixture.py';lines=fixture.read_text(encoding='utf8').splitlines()
        assert lines[0]=='import os' and lines[1].startswith('os.environ.update(')
        call=ast.parse(lines[1]).body[0].value;shared=ast.literal_eval(call.args[0])
        shared['VIBEFLOW_STORAGE_FS_ROOT']=str(value.root/'s3-cache')
        lines[1]='os.environ.update('+repr(shared)+')'
        fixture.write_text('\n'.join(lines)+'\n',encoding='utf8')
    return value


def scenario(api,*,store=None,**_):
    (api.root/'entry-source.py').write_bytes(Path(__file__).read_bytes())
    api.create_case('Actual canonical UDF workflow and rebuild')
    name='myFunctionObject';run_dir='C-run/case-1';checks=[]
    source=peer(api,store,'udf-workflow-source');recovery=peer(api,store,'udf-workflow-recovery')
    def record(row):
        checks.append(row)
        (api.root/'udf-workflow-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    def waiting(server,identifier,key):
        def observed():
            value=server.call('GET','/workflow-runs/'+identifier)
            if value['status'] in {'failed','interrupted','cancelled'}:
                jobs={str(a['job_id']):server.call('GET','/jobs/'+str(a['job_id'])) for s in value['steps'] for a in s['attempts']}
                (api.root/'failed-workflow.json').write_text(json.dumps({'workflow':value,'jobs':jobs},indent=2),encoding='utf8')
                raise AssertionError('Actual UDF workflow failed; see failed-workflow.json')
            step=next(s for s in value['steps'] if s['key']==key)
            return (value,step) if step['status']=='awaiting_confirmation' else None
        return wait_for(observed,'Workflow did not reach '+key,240)
    def drain(server,jid):
        wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Actual UDF producer did not drain',90)
    def confirm(server,identifier,key):
        value,step=waiting(server,identifier,key)
        for item in value['steps']:
            if item['status']=='succeeded' and item['attempts']:
                jid=item['attempts'][-1]['job_id'];server.wait_job(jid);drain(server,jid)
        result=write(server,'POST',server.prefix+'/caseDict:advance',{'state':step['confirmation_state'],
            'confirmed':True,'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],
            'expected_step_version':step['version']})
        kind=confirmed_dispatch_pending_kind(result)
        if kind:
            with (api.root/'udf-workflow-confirmation-backpressure.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'workflow':identifier,'confirmation':key,'known_wait':kind,
                    'actual_dispatch':result['workflow_dispatch']})+'\n')
    def cleanup():
        for row in api.call('GET',api.prefix+'/jobs'):
            if row['status'] in {'queued','running'}:
                api.call('POST','/jobs/'+str(row['id'])+':cancel',{})
                wait_for(lambda:api.call('GET','/jobs/'+str(row['id']))['status'] not in {'queued','running'},'Owned UDF failure cleanup did not stop',90)
    def validate_solution(server,flow,marker):
        value,confirmation=waiting(server,flow['id'],'confirm_D')
        build=next(s for s in value['steps'] if s['key']=='udf-build');solve=next(s for s in value['steps'] if s['key']=='solve')
        assert len(build['attempts'])==len(solve['attempts'])==1
        jobs=[server.wait_job(s['attempts'][0]['job_id'],timeout=240) for s in [build,solve]]
        for job in jobs:drain(server,job['id'])
        build_pub=jobs[0]['attempt']['outputs']['publication'];solve_pub=jobs[1]['attempt']['outputs']['publication']
        assert build_pub['kind']=='udf-build' and solve_pub['kind']=='solve'
        bm=api.call('GET','/testing/workflow-publication/'+build_pub['id'])['manifest']
        sm=api.call('GET','/testing/workflow-publication/'+solve_pub['id'])['manifest']
        bn=bm['descriptor']['native']['receipt']['native'];sn=sm['descriptor']['native']['receipt']['native']
        assert bn['uid']!=0 and sn['uid']!=0 and set(bn['steps'])=={'build','load'} and set(sn['steps'])=={'load','solve'}
        library='UDF/'+name+'/libmyFunctionObject.so';digest=bm['files'][library]['sha256']
        assert sn['library']['sha256']==bn['library']['sha256']==digest
        assert sm['descriptor']['native']['source']['files'][library]['sha256']==digest
        assert sm['descriptor']['source']['schema']=='vibeflow.workflow-udf-source.v2'
        assert sm['descriptor']['source']['native_case']==sm['descriptor']['native']['source']
        assert sn['case_queries']['evidence']['events']>0 and sn['case_queries']['evidence']['bytes']>0
        assert library in sm['descriptor']['native']['origin_source']['files']
        assert sm['descriptor']['native']['origin_source']['files'][run_dir+'/system/controlDict']['sha256']==hashlib.sha256(control.encode()).hexdigest()
        assert sm['descriptor']['native']['configuration']['load_log']==run_dir+'/udf-load.log'
        assert build_pub['id'] in {r['publication_id'] for r in sm['descriptor']['input_publications']}
        assert server.call('GET',server.prefix+'/raw?path='+quote(run_dir+'/system/controlDict'),raw=True).decode()==control
        transcript=server.call('GET',server.prefix+'/raw?path='+quote(run_dir+'/udf-solve.log'),raw=True).decode()
        assert marker in transcript
        assert marker not in ('P0_UDF_OK','')
        raw=server.call('GET',server.prefix+'/raw?path='+quote(run_dir+'/0.5/U'),raw=True)
        assert len(bridge.vectors(raw))==400
        graph=server.call('GET',server.prefix+'/dependencies?run_dir='+quote(run_dir))
        channels={c['kind']:c for c in graph['channels']}
        assert all(channels[k]['status']=='fresh' for k in ['mesh','udf-build','solve'])
        assert server.call('GET',server.prefix+'/caseDict')['workflow_state']=='C'
        record({'workflow':flow['id'],'actual_UDF_build_job':jobs[0]['id'],'actual_UDF_solve_job':jobs[1]['id'],
            'build_publication':build_pub['id'],'solve_publication':solve_pub['id'],'actual_library_sha256':digest,
            'actual_wmake_and_CDLL':True,'actual_icoFoam_loaded_pinned_library':True,'actual_marker':marker,
            'canonical_solve_publication':True,'private_control_did_not_stale_mesh':True,'actual_400_cell_final_time':.5,
            'runtime_query_events':sn['case_queries']['evidence']['events'],'runtime_query_bytes':sn['case_queries']['evidence']['bytes'],
            'original_source_retained':True,'private_derived_control_bound':True,
            'automatic_ready_build_to_solve_continuation':True})
        # Completion/alias visibility may settle after confirm_D first appears.
        # Compare restart history only after the actual producer wait/drain.
        return server.call('GET','/workflow-runs/'+flow['id']),digest
    def finish(server,flow):
        confirm(server,flow['id'],'confirm_D')
        value,step=waiting(server,flow['id'],'confirm_E')
        post=next(s for s in value['steps'] if s['key']=='postprocess')
        server.wait_job(post['attempts'][0]['job_id']);drain(server,post['attempts'][0]['job_id'])
        jid=post['attempts'][0]['job_id']
        metrics=json.loads(server.call('GET',server.prefix+'/raw?path='+quote('D-post/data/postprocess-'+str(jid)+'/metrics.json'),raw=True))
        mean=next(row['value'] for row in metrics['metrics'] if row['name']=='meanPressure.p@0.5')
        assert abs(mean-.022259352333744136)<1e-5 and metrics['readiness']['report_ready'],metrics
        confirm(server,flow['id'],'confirm_E')
        final=server.call('GET','/workflow-runs/'+flow['id']);assert final['status']=='succeeded'
        return final
    for server,label in [(api,'main'),(source,'source'),(recovery,'recovery')]:
        original=server.opener.open
        def observed(request,*args,_original=original,_label=label,**kwargs):
            started=time.monotonic();failure=None
            try:return _original(request,*args,**kwargs)
            except BaseException as error:failure=type(error).__name__;raise
            finally:
                with (api.root/'udf-workflow-http.jsonl').open('a',encoding='utf8') as stream:
                    stream.write(json.dumps({'role':_label,'method':request.get_method(),'path':request.selector,
                        'seconds':round(time.monotonic()-started,4),'error':failure})+'\n')
        server.opener.open=observed
    with source:
        try:
            saved=source.call('GET',source.prefix+'/caseDict')
            text=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
                {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            text=set_block(text,'numericalMethod',{'solver':'icoFoam'})
            text=set_block(text,'postProcessing',{'runDir':run_dir,'fields':{'p':{'unit':'m2/s2'}},
                'dataExtraction':{'volumeAverages':{'meanPressure':{'field':'p','time':.5}}}})
            write(source,'PUT',source.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
            inputs=cavity_2d();control=inputs[run_dir+'/system/controlDict']
            for path,text in inputs.items():put(source,path,text)
            source.call('POST',source.prefix+'/udf/scaffold',{'name':name,'objective':'Actual formal workflow build/load/solve'})
            cpp='UDF/'+name+'/myFunctionObject.C'
            original=source.call('GET',source.prefix+'/raw?path='+quote(cpp),raw=True).decode();assert 'P0_UDF_OK' in original
            put(source,cpp,original.replace('P0_UDF_OK','UDF_WORKFLOW_V1'))
            saved=put(source,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
            if saved.get('auto_preview_job'):source.wait_job(saved['auto_preview_job'])
            dictionary=source.call('GET',source.prefix+'/caseDict')
            flow=source.call('POST',source.prefix+'/workflow-runs',{'target':'postprocess','expected_revision':dictionary['dict_revision'],
                'parameters':{'geometry_mode':'export','solve_mode':'udf','udf_name':name},
                'budget':{'timeout_seconds':120,'max_log_bytes':16777216}})
            confirm(source,flow['id'],'confirm_B');confirm(source,flow['id'],'confirm_C')
            old_wait,library1=validate_solution(source,flow,'UDF_WORKFLOW_V1')
            (api.root/'before-restart-workflow.json').write_text(json.dumps(old_wait,indent=2),encoding='utf8')
            jobs_before=[j['id'] for j in api.call('GET',api.prefix+'/jobs')]
            pid=source.call('GET','/testing/workflow-api-owner')['pid'];launcher=kill_owned_api(source,pid)
            record({'actual_source_API_pid_killed':pid,'verified_launcher_pid':launcher,'stage_D_not_auto_confirmed':True})
        finally:cleanup()
    if store:assert not (recovery.root/'s3-cache').exists()
    with recovery:
        try:
            after_restart=recovery.call('GET','/workflow-runs/'+flow['id'])
            (api.root/'after-restart-workflow.json').write_text(json.dumps(after_restart,indent=2),encoding='utf8')
            assert after_restart==old_wait
            assert [j['id'] for j in api.call('GET',api.prefix+'/jobs')]==jobs_before
            old_history=finish(recovery,flow)
            record({'restart_preserved_UDF_and_build_without_replay':True,'explicit_D_and_E':True,'actual_pressure_extraction_ready':True})
            if store:record({'new_API_started_with_empty_independent_cache':True,
                'source_cache_root':str(source.root/'s3-cache'),'recovery_cache_root':str(recovery.root/'s3-cache'),
                'canonical_remote_library_mesh_solve_and_postprocess_read':True})
            put(recovery,cpp,original.replace('P0_UDF_OK','UDF_WORKFLOW_V2'))
            graph=recovery.call('GET',recovery.prefix+'/dependencies?run_dir='+quote(run_dir));channels={c['kind']:c for c in graph['channels']}
            assert channels['mesh']['status']=='fresh' and all(channels[k]['status']=='stale' for k in ['udf-build','solve','postprocess'])
            dictionary=recovery.call('GET',recovery.prefix+'/caseDict')
            body={'expected_version':old_history['version'],'expected_revision':dictionary['dict_revision'],'confirmed_reset':True}
            key=uuid.uuid4().hex;fresh=submit(recovery,'/workflow-runs/'+flow['id']+':recompute',body,key)
            assert fresh['recomputation']['rebuild']==['udf-build','solve','postprocess']
            assert set(fresh['recomputation']['reused'])=={'geometry','mesh'}
            confirm(recovery,fresh['id'],'confirm_B');confirm(recovery,fresh['id'],'confirm_C')
            _,library2=validate_solution(recovery,fresh,'UDF_WORKFLOW_V2');assert library2!=library1
            new_history=finish(recovery,fresh)
            assert submit(recovery,'/workflow-runs/'+flow['id']+':recompute',body,key)['id']==fresh['id']
            assert recovery.call('GET','/workflow-runs/'+flow['id'])==old_history
            record({'changed_CXX_built_distinct_actual_ELF':True,'only_UDF_and_downstream_rebuilt':True,
                'mesh_geometry_reused_with_explicit_B_C':True,'old_history_unchanged':True,'idempotent_new_run':True,
                'new_workflow':fresh['id'],'old_library_sha256':library1,'new_library_sha256':library2})
            before_report_jobs=[j['id'] for j in api.call('GET',api.prefix+'/jobs')]
            dictionary=recovery.call('GET',recovery.prefix+'/caseDict')
            report_plan=submit(recovery,'/workflow-runs/'+fresh['id']+':recompute',{
                'expected_version':new_history['version'],'expected_revision':dictionary['dict_revision'],
                'confirmed_reset':True,'target':'report'},uuid.uuid4().hex)
            assert report_plan['recomputation']['rebuild']==['report']
            assert set(report_plan['recomputation']['reused'])=={'geometry','mesh','udf-build','solve','postprocess'}
            confirm(recovery,report_plan['id'],'confirm_B');confirm(recovery,report_plan['id'],'confirm_C')
            reused,_=waiting(recovery,report_plan['id'],'confirm_D')
            assert all(not s['attempts'] and s['execution_mode']=='reuse' for s in reused['steps'] if s['key'] in {'udf-build','solve'})
            confirm(recovery,report_plan['id'],'confirm_D');at_E,_=waiting(recovery,report_plan['id'],'confirm_E')
            cancelled=recovery.call('POST','/workflow-runs/'+report_plan['id']+':cancel',{'expected_version':at_E['version']})
            assert cancelled['status']=='cancelled'
            assert [j['id'] for j in api.call('GET',api.prefix+'/jobs')]==before_report_jobs
            assert recovery.call('GET','/workflow-runs/'+fresh['id'])==new_history
            record({'fresh_build_and_solve_prefix_reused_without_Jobs':True,'report_plan_cancelled_before_execution':True,
                'B_C_D_remained_explicit_for_reuse':True})
        finally:cleanup()
    report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,
        'scope':'Actual native build/load/UDF workflow/downstream/restart/CXX-change recomputation; running cancellation, full local/external dependency closure and typed multi-case batches remain'}
    (api.root/'udf-workflow.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    api.create_case('Independent actual UDF workflow evidence readback')
    return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
