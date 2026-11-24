"""Five typed user-command handlers: real processes and real model readback.

The outputs are ordinary command results, not professional CAE solver evidence.
"""
import asyncio,hashlib,json,uuid
from pathlib import Path
from live_llm_job_repeat_acceptance import main,repeat,put,write
from app.services.casedict import set_block


def scenario(api,**_):
    checks=[]
    for kind in ('convert','geometry','mesh','pvpython','solve'):
        api.create_case('Actual typed '+kind+' repeat')
        original='ORIGINAL_'+kind+'_'+uuid.uuid4().hex
        current='CURRENT_'+kind+'_'+uuid.uuid4().hex
        permit=api.root/('typed-permit-'+kind)
        program=("from pathlib import Path\nvalue=Path('Ref/value.txt').read_text()\n"
            "if not Path("+repr(str(permit))+").exists():raise SystemExit(23)\n"
            "Path('D-post').mkdir(exist_ok=True)\nPath('D-post/result.txt').write_text(value)\nprint(value)\n")
        put(api,'Ref/program.py',program);put(api,'Ref/value.txt',original)
        source=api.call('POST',api.prefix+'/jobs',{'type':kind,'command':'python Ref/program.py','timeout_seconds':120})
        failed=api.wait_job(source['id'],expected='failed');assert failed['exit_code']==23
        assert failed['attempt']['handler']['key']=='command.'+kind
        choices=api.call('GET','/jobs/'+str(source['id'])+'/operations')
        assert choices['retry']['available'] and not choices['resume']['available']
        assert not choices['rerun']['parameters_required']
        permit.touch()
        request={'action':'retry','source_attempt_id':choices['source_attempt_id'],'source_version':choices['source_version']}
        retried=repeat(api,source['id'],request,uuid.uuid4().hex);done=api.wait_job(retried['id'])
        assert original in done['log_backlog'] and done['attempt']['number']==2 and done['attempt']['root_job_id']==source['id']
        assert done['attempt']['handler']['key']=='command.'+kind
        assert done['attempt']['outputs']['publication']['status']=='committed'
        assert api.call('GET',api.prefix+'/raw?path=D-post/result.txt',raw=True)==original.encode()
        case_root=next((api.root/'files/workspaces').glob('*/cases/'+str(api.case_id)))
        publication=case_root/'.vibeflow/publications'/done['attempt']['outputs']['publication']['id']
        manifest=json.loads((publication/'manifest.json').read_text(encoding='utf8'))
        assert manifest['descriptor']['result']['professional_workflow_proof'] is False
        historical=(publication/'files/D-post/result.txt').read_bytes();assert historical==original.encode()
        put(api,'Ref/value.txt',current)
        choices=api.call('GET','/jobs/'+str(source['id'])+'/operations')
        request={'action':'rerun','source_attempt_id':choices['source_attempt_id'],'source_version':choices['source_version'],
            'expected_revision':choices['expected_revision'],'expected_event_sequence':choices['expected_event_sequence']}
        rerun=repeat(api,source['id'],request,uuid.uuid4().hex);fresh=api.wait_job(rerun['id'])
        assert current in fresh['log_backlog'] and fresh['attempt']['handler']['key']=='command.'+kind
        assert fresh['attempt']['number']==1 and fresh['attempt']['root_job_id']==fresh['id']
        assert fresh['attempt']['outputs']['publication']['status']=='committed'
        assert api.call('GET',api.prefix+'/raw?path=D-post/result.txt',raw=True)==current.encode()
        assert (publication/'files/D-post/result.txt').read_bytes()==historical
        assert api.call('GET','/jobs/'+str(source['id']))['status']=='failed'
        # Source changes require rerun; an explicit retry must not publish old
        # results over a case whose registered source has changed.
        put(api,'Ref/fail.py','raise SystemExit(29)\n')
        stale=api.call('POST',api.prefix+'/jobs',{'type':kind,'command':'python Ref/fail.py','timeout_seconds':120})
        api.wait_job(stale['id'],expected='failed')
        stale_choices=api.call('GET','/jobs/'+str(stale['id'])+'/operations')
        put(api,'Ref/value.txt','ACTUAL_INPUT_CHANGED_'+kind)
        count=len(api.call('GET',api.prefix+'/jobs'))
        refused=repeat(api,stale['id'],{'action':'retry',
            'source_attempt_id':stale_choices['source_attempt_id'],'source_version':stale_choices['source_version']},uuid.uuid4().hex,expect=409)
        assert refused['detail']['code']=='input_stale'
        assert len(api.call('GET',api.prefix+'/jobs'))==count
        put(api,'Ref/value.txt',current)
        dictionary=api.call('GET',api.prefix+'/caseDict')
        changed=set_block(dictionary['content'],'numericalMethod',{'solver':'icoFoam'})
        write(api,'PUT',api.prefix+'/caseDict',{'content':changed,'expected_revision':dictionary['dict_revision']})
        physics_refused=repeat(api,stale['id'],{'action':'retry',
            'source_attempt_id':stale_choices['source_attempt_id'],'source_version':stale_choices['source_version']},uuid.uuid4().hex,expect=409)
        assert physics_refused['detail']['code']=='input_stale'
        assert len(api.call('GET',api.prefix+'/jobs'))==count
        choices=api.call('GET','/jobs/'+str(source['id'])+'/operations')
        replacement=repeat(api,source['id'],{
            'action':'rerun','source_attempt_id':choices['source_attempt_id'],'source_version':choices['source_version'],
            'expected_revision':choices['expected_revision'],'expected_event_sequence':choices['expected_event_sequence'],
            'parameters':{'spec':{'command':'echo SHOULD_NOT_REPLACE_ORIGINAL'}}},uuid.uuid4().hex,expect=409)
        assert replacement['detail']['code']=='invalid_request'
        assert len(api.call('GET',api.prefix+'/jobs'))==count
        checks.append({'handler':'command.'+kind,'source_job':source['id'],'retry_job':retried['id'],'rerun_job':rerun['id'],
            'actual_initial_exit_code':23,'retry_uses_original_bytes':True,'rerun_uses_current_bytes':True,
            'retry_same_lineage':True,'rerun_new_root':True,'publications_committed':True,'old_version_preserved':True,
            'changed_source_retry_refused':True,'engineering_dictionary_change_refused':True,
            'failure_bookkeeping_does_not_prevent_retry':True,'command_override_refused':True,'resume_not_registered':True,
            'professional_workflow_proof':False,'source_sha256':hashlib.sha256(program.encode()).hexdigest()})
    report={'passed':True,'checks':checks,'actual_python_processes':20,'mock_llm_calls':0,
        'real_professional_cae_solver_claimed':False,'typed_handlers':5}
    (api.root/'typed-repeat.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario))
