"""Actual command retries preserve platform timeline prefixes and live history."""
import asyncio,json,uuid
from pathlib import Path
from live_llm_job_repeat_acceptance import main,repeat,put


def scenario(api,**_):
    permit=api.root/'real-timeline-retry-permit'
    code="from pathlib import Path\nif not Path("+repr(str(permit))+").exists():raise SystemExit(23)\nPath('D-post').mkdir(exist_ok=True)\nPath('D-post/real-result.txt').write_text('actual command result')\n"
    put(api,'Ref/retry.py',code)
    original=api.call('POST',api.prefix+'/jobs',{'type':'convert','command':'python Ref/retry.py','timeout_seconds':120})
    failed=api.wait_job(original['id'],expected='failed');assert failed['exit_code']==23
    choices=api.call('GET','/jobs/'+str(original['id'])+'/operations')
    body={'action':'retry','source_attempt_id':choices['source_attempt_id'],'source_version':choices['source_version']}
    root=next((api.root/'files/workspaces').glob('*/cases/'+str(api.case_id)))
    timeline=root/'0-caseDict/case-events.jsonl';current=timeline.read_bytes()
    count=len(api.call('GET',api.prefix+'/jobs'));checks=[]
    malformed=(current+b'invalid appended platform event\n')
    wrong_case=current+(json.dumps({'schema':'vibeflow.case-event.v1','case_id':api.case_id+1000})+'\n').encode()
    boolean_case=current+(json.dumps({'schema':'vibeflow.case-event.v1','case_id':True})+'\n').encode()
    for label,value in (('old_prefix_rewritten',b'CHANGED_PREFIX\n'+current),('invalid_append',malformed),('wrong_case_append',wrong_case),('boolean_case_append',boolean_case)):
        try:
            timeline.write_bytes(value)
            refused=repeat(api,original['id'],body,uuid.uuid4().hex,expect=409)
            assert refused['detail']['code']=='input_stale'
            assert len(api.call('GET',api.prefix+'/jobs'))==count
            checks.append({'mode':label,'refused_before_execution':True})
        finally:timeline.write_bytes(current)
    permit.touch();retried=repeat(api,original['id'],body,uuid.uuid4().hex);done=api.wait_job(retried['id'])
    assert done['attempt']['number']==2 and done['attempt']['outputs']['publication']['status']=='committed'
    assert timeline.read_bytes().startswith(current)
    checks.append({'mode':'actual_failure_event_append','retry_succeeded':True,'original_prefix_retained':True})
    for mode,statement in (('overwrite',"p.write_bytes(p.read_bytes()+b'NATIVE_UNAPPROVED_TIMELINE_CHANGE\\n')"),('delete','p.unlink()')):
        put(api,'Ref/timeline-'+mode+'.py',"from pathlib import Path\np=Path('0-caseDict/case-events.jsonl')\n"+statement+'\n')
        before=timeline.read_bytes()
        source=api.call('POST',api.prefix+'/jobs',{'type':'convert','command':'python Ref/timeline-'+mode+'.py','timeout_seconds':120})
        rejected=api.wait_job(source['id'],expected='failed')
        assert rejected['attempt']['process_stop']['verified']
        assert rejected['attempt']['outputs']['publication']['status']!='committed'
        after=timeline.read_bytes();assert after.startswith(before) and b'NATIVE_UNAPPROVED_TIMELINE_CHANGE' not in after
        checks.append({'mode':'native_'+mode,'actual_native_process_completed':True,'publication_refused':True,'live_history_preserved':True})
    report={'passed':True,'mock_llm_calls':0,'checks':checks,'actual_python_processes':4,
        'actual_cae_solver_claimed':False,'timeline_input_remains_sealed':True}
    (api.root/'typed-timeline.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario))
