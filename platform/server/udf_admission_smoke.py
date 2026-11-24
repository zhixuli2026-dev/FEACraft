"""UDF idempotent reads under a real persisted validation fence and missing source."""
import json
from testsupport import IsolatedServer
from case_validation_smoke import FIXTURE

PAUSED='''
from app.services import job_delivery
async def pause(stop):await stop.wait()
async def defer(job_id,**kwargs):raise job_delivery.DeliveryDeferred('UDF admission fixture holds delivery')
job_delivery.recover_forever=pause
job_delivery.deliver=defer
'''


def main():
    api=IsolatedServer('udf-admission',app='udf_admission_fixture:app')
    (api.root/'udf_admission_fixture.py').write_text(FIXTURE+PAUSED,encoding='utf8')
    checks=[]
    with api:
        api.create_case('UDF replay before mutable validation')
        api.call('POST',api.prefix+'/udf/scaffold',{})
        api.put('C-run/case-1/system/controlDict','application icoFoam;\n')
        root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        # Admission-only bytes: the executor stays paused; actual ELF validation
        # and invalid-library rejection have separate real native tests.
        library=root/'UDF/myFunctionObject/libmyFunctionObject.so';library.write_bytes(b'not executed by this fixture')
        submitted=[]
        for mode in ('build','integrate'):
            body={} if mode=='build' else {'run_dir':'C-run/case-1'}
            headers={'Idempotency-Key':'udf-'+mode}
            job=api.call('POST',api.prefix+'/udf:'+mode,body,extra_headers=headers)['job_id']
            submitted.append((mode,body,headers,job))
        owner=api.call('POST',f'/fixture/owner?case_id={api.case_id}')['job_id']
        api.call('POST',f'/fixture/op?job_id={owner}&action=reserve&seconds=60')
        try:
            (root/'UDF/myFunctionObject/myFunctionObject.C').unlink();library.unlink()
            for mode,body,headers,identifier in submitted:
                replay=api.call('POST',api.prefix+'/udf:'+mode,body,extra_headers=headers)
                assert replay['job_id']==identifier and replay['status']=='queued'
                checks.append(mode+'_replay_ignores_missing_source_and_active_write_fence')
            rejected=api.call('POST',api.prefix+'/udf:build',{},extra_headers={'Idempotency-Key':'new'},expect=409)
            assert rejected['detail']['code']=='validation_busy',rejected
            assert len(api.call('GET',api.prefix+'/jobs'))==3
            checks.append('new_submission_remains_blocked_without_an_extra_job')
        finally:api.call('POST',f'/fixture/op?job_id={owner}&action=release')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'admission-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    print('UDF_ADMISSION_OK '+json.dumps(result))


if __name__=='__main__':main()
