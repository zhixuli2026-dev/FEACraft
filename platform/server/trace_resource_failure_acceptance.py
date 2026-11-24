"""Real process controls: exit98/log text cannot impersonate a helper allowance.

No model inference is called or replaced. This is an actual API/native process
test, separate from the full OpenFOAM trace-limit reproduction.
"""
import json,sys,time
from pathlib import Path
from testsupport import IsolatedServer,SERVER


def main():
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    api=IsolatedServer('trace-resource-controls')
    report={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'jobs':[]}
    def settled(id,status):
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            value=api.call('GET','/jobs/'+str(id));attempt=value.get('attempt') or {}
            if (value['status']==attempt.get('phase')==status
                    and (attempt.get('process_stop') or {}).get('verified') and not value.get('completion_pending')):
                assert attempt['process_stop']['scope_empty']
                return value
            if value.get('completion_error'):raise AssertionError(value['completion_error'])
            time.sleep(.1)
        raise TimeoutError('Native control did not finish')
    try:
        with api:
            try:
                api.create_case('Actual resource classification controls')
                for code in (98,7,0):
                    # A literal diagnostic is deliberately printed by a real
                    # child. Production must use the receipt, never this text.
                    command='python -c "print(\'Native case query trace exceeded its admitted bound\');raise SystemExit('+str(code)+')"'
                    job=api.call('POST',api.prefix+'/jobs',{'command':command},expect=200)
                    report['jobs'].append({'id':job['id'],'exit_code_requested':code})
                    done=settled(job['id'],'succeeded' if code==0 else 'failed')
                    reason=done['attempt']['reason_code']
                    assert reason==('' if code==0 else 'process_exit_failed'),reason
                    assert done['exit_code']==code
                    report['jobs'][-1].update(reason_code=reason,actual_exit_code=done['exit_code'],stop_verified=True)
                    (api.root/('job-'+str(job['id'])+'.json')).write_text(json.dumps(done,ensure_ascii=False,indent=2),encoding='utf8')
                report['functional_passed']=True
            finally:
                for row in report['jobs']:
                    value=api.call('GET','/jobs/'+str(row['id']))
                    if value['status'] in {'queued','running'}:
                        api.call('POST','/jobs/'+str(row['id'])+':cancel');settled(row['id'],'cancelled')
        report['api_exited']=api.proc.poll() is not None
        report['passed']=bool(report.get('functional_passed') and report['api_exited'])
    except Exception as error:report['failure']={'type':type(error).__name__,'message':str(error)[:1500]}
    finally:
        report['api_exited']=api.proc is not None and api.proc.poll() is not None
        (api.root/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('TRACE_RESOURCE_CONTROLS '+str(api.root/'acceptance.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
