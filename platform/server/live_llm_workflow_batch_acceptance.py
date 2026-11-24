"""Actual typed geometry members, atomic admission, capacity and real readback.

This entry does not claim MPI/UDF members, running cancellation or Redis restart.
"""
import asyncio,copy,json,time,uuid
from pathlib import Path

from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_mpi_workflow_acceptance import FIXTURE


def scenario(api,*,store=None,**_):
    (api.root/'batch-entry-source.py').write_bytes(Path(__file__).read_bytes())
    actors=[];checks=[]
    def record(value):
        checks.append(value);(api.root/'workflow-batch-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    workspace=api.call('GET','/workspaces')[0]['id']
    def actor(name):
        case=api.call('POST','/workspaces/'+str(workspace)+'/cases',{'name':name})
        value=copy.copy(api);value.case_id=case['id'];value.prefix='/cases/'+str(case['id']);actors.append(value)
        saved=put(value,'A-geo/geometry.py','import cadquery as cq\nimport time\ndef build_geometry(ctx):\n time.sleep(2)\n return {"box":cq.Workplane("XY").box(1,2,3)}\n')
        if saved.get('auto_preview_job'):
            jid=saved['auto_preview_job'];value.wait_job(jid)
            wait_for(lambda:value.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Actual preview did not drain')
        return value
    def member(value,key):
        return {'key':key,'case_id':value.case_id,'target':'geometry','run_dir':'C-run/case-1',
            'expected_revision':value.call('GET',value.prefix+'/caseDict')['dict_revision'],
            'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':180,'max_log_bytes':16777216}}
    def submit(body,key,expect=202):
        return api.call('POST','/workflow-batches',body,expect=expect,extra_headers={'Idempotency-Key':key})
    def run_ids(value):return [r['id'] for r in value.call('GET',value.prefix+'/workflow-runs')]
    try:
        first,second=actor('Typed batch actual member one'),actor('Typed batch actual member two')
        body={'members':[member(first,'one'),member(second,'two')],'max_concurrent':1}
        before=[run_ids(first),run_ids(second)];groups_before=api.call('GET','/workflow-batches')
        bad=copy.deepcopy(body);bad['members'][1]['expected_revision']+=1
        refused=submit(bad,uuid.uuid4().hex,409)
        assert refused['detail']['code']=='revision_conflict'
        assert [run_ids(first),run_ids(second)]==before and api.call('GET','/workflow-batches')==groups_before
        record({'stage':'actual_SQL_half_batch_rollback','first_valid_second_stale':True,'no_parent_or_member_run_published':True})
        unknown=copy.deepcopy(body);unknown['members'][1]['case_id']=2147483000
        submit(unknown,uuid.uuid4().hex,404)
        assert [run_ids(first),run_ids(second)]==before and api.call('GET','/workflow-batches')==groups_before
        record({'stage':'unknown_member_refused_before_any_batch_write','no_partial_runs':True})
        key=uuid.uuid4().hex;batch=submit(body,key);identifier=batch['id'];ids={r['id'] for r in batch['members']}
        assert len(ids)==2 and {r['case_id'] for r in batch['members']}=={first.case_id,second.case_id}
        assert submit(body,key)['id']==identifier
        changed=copy.deepcopy(body);changed['max_concurrent']=2
        assert submit(changed,key,409)['detail']['code']=='idempotency_conflict'
        record({'stage':'persisted_typed_members_and_request_idempotency','batch_id':identifier,'run_ids':sorted(ids),
            'same_key_same_group':True,'changed_payload_refused':True})
        maximum=0;observations=[];deadline=time.monotonic()+240
        while True:
            batch=api.call('GET','/workflow-batches/'+identifier)
            jobs=[j for value in actors for j in value.call('GET',value.prefix+'/jobs') if j.get('run_id') in ids]
            active=[j for j in jobs if (j.get('attempt') or {}).get('phase') in {'claimed','starting','running','finalizing','cancelling'}]
            maximum=max(maximum,len(active));assert len(active)<=1,active
            observations.append({'active_job_ids':[j['id'] for j in active],'statuses':[r['status'] for r in batch['members']]})
            if all(r['status']=='awaiting_confirmation' for r in batch['members']):break
            if any(r['status'] in {'failed','interrupted','cancelled'} for r in batch['members']):
                (api.root/'batch-failed-runs.json').write_text(json.dumps({'batch':batch,'jobs':jobs},indent=2),encoding='utf8')
                raise AssertionError('Actual batch geometry member failed; see batch-failed-runs.json')
            if time.monotonic()>deadline:raise TimeoutError('Actual typed batch did not reach independent confirmations')
            time.sleep(.1)
        assert maximum==1
        proofs=[]
        for value in actors:
            summary=next(r for r in batch['members'] if r['case_id']==value.case_id)
            flow=value.call('GET','/workflow-runs/'+summary['id'])
            geometry=next(s for s in flow['steps'] if s['key']=='geometry');confirmation=next(s for s in flow['steps'] if s['key']=='confirm_B')
            jid=geometry['attempts'][-1]['job_id'];job=value.wait_job(jid)
            wait_for(lambda:value.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Batch geometry did not drain')
            publication=value.call('GET','/testing/workflow-publication/'+geometry['publication']['id'])['manifest']
            step_files=[p for p,r in publication['files'].items() if p.lower().endswith(('.step','.stp')) and r['size']>100]
            assert step_files and job['status']=='succeeded'
            write(value,'POST',value.prefix+'/caseDict:advance',{'state':'B','confirmed':True,
                'expected_revision':confirmation['confirmation_revision'],'workflow_step_id':confirmation['id'],
                'expected_step_version':confirmation['version']})
            proofs.append({'case_id':value.case_id,'run_id':flow['id'],'actual_job':jid,'actual_STEP_files':step_files,
                'explicit_B_confirmation':True,'publication':geometry['publication']['id']})
        final=wait_for(lambda:(b if (b:=api.call('GET','/workflow-batches/'+identifier))['status']=='succeeded' else None),
            'Batch did not derive final success after both confirmations')
        before_jobs=[[j['id'] for j in value.call('GET',value.prefix+'/jobs')] for value in actors]
        assert submit(body,key)['id']==identifier
        assert before_jobs==[[j['id'] for j in value.call('GET',value.prefix+'/jobs')] for value in actors]
        record({'stage':'actual_CadQuery_exports_and_batch_capacity','maximum_observed_claimed_or_running':maximum,
            'observations':observations,'proofs':proofs,'both_independent_confirmations':True,'terminal_replay_created_no_jobs':True})
        stranger=copy.copy(api);stranger.create_case('Foreign batch reader')
        stranger.call('GET','/workflow-batches/'+identifier,expect=404)
        stranger.call('POST','/workflow-batches/'+identifier+':cancel',{'expected_version':final['version']},expect=404)
        assert stranger.call('GET','/workflow-batches')==[]
        record({'stage':'other_user_batch_read_cancel_and_list_refused','no_members_exposed':True})
        report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,
            'scope':'Independent case geometry workflow batch only; MPI/UDF, running cancellation, cross-API restart and Redis matrix remain.'}
        (api.root/'workflow-batch.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        api.create_case('Independent actual typed workflow batch evidence readback')
        return report
    finally:
        for value in actors:
            for job in value.call('GET',value.prefix+'/jobs'):
                if job['status'] in {'queued','running'}:
                    value.call('POST','/jobs/'+str(job['id'])+':cancel',{})
                    value.wait_job(job['id'],expected='cancelled',timeout=120)


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
