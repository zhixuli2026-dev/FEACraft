"""Failure-stage protocol coverage using actual owned Python exit processes.

These are explicitly labelled typed fault inputs, not real CAE solver runs.
Each process really executes once, exits 29 and supplies its real stop/drain
evidence. Only the final independent evidence readback invokes the real model.
"""
import asyncio,json,uuid
from casedict_fallback_capacity_acceptance import read_settled
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import wait_for


FIXTURE=r'''
import sys,uuid,json
from fastapi import Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import Job,JobAttempt,DurableEvent
from app.routers.workspaces import get_owned_case
from app.services import job_execution,job_delivery,workflow_fallback
from app.services.case_write_capacity import committing
from app.services.job_write_checkpoint import validate

@app.post('/api/v1/cases/{case_id}/testing/stage-failure')
async def stage_failure(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True)
 kind=body['job_type'];assert kind in workflow_fallback.JOB_STAGE
 spec={'command':'Explicit typed stage-policy fault process','case_prefix':case.s3_prefix,
  'runtime':'windows','argv':[sys.executable,'Ref/stage-fault.py'],'evidence_run_dir':None,
  'timeout_seconds':60,'max_log_bytes':1048576}
 async with committing(db,case,content_write=False):
  job,_=await job_execution.submit(db,case_id=case_id,user_id=user.id,key=uuid.uuid4().hex,
   digest=job_execution.request_hash({'stage_policy_fault':kind,'spec':spec}),job_type=kind,
   command=spec['command'],spec=spec,input_paths=['Ref/stage-fault.py','0-caseDict/caseDict'])
  await job_delivery.request(db,job,user_id=user.id,mode='local')
  identifier=job.id
 await job_delivery.deliver(identifier)
 return {'job_id':identifier,'scope':'Real Python exit fault for stage policy; no CAE result claimed'}

@app.get('/api/v1/jobs/{job_id}/testing/stage-fallback')
async def stage_observation(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=False)
 row=await db.get(DurableEvent,workflow_fallback.event_id(job,workflow_fallback.APPLIED))
 if row is None:return {'applied':None}
 attempt=await db.get(JobAttempt,row.attempt_id);validate(case,job,attempt)
 return {'applied':json.loads(row.payload_json),'stage':case.workflow_state,'actual_stop_and_drain_verified':True}

@app.post('/api/v1/jobs/{job_id}/testing/stage-fallback')
async def stage_replay(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=True)
 return await workflow_fallback.settle_failure_fallback(db,case,job)
'''


def scenario(api,*,store=None,**_):
 targets={'geometry':'A','mesh':'B','solve':'C','postprocess':'D','convert':'E','report':'E','udf':'V'}
 checks=[]
 for initial in ['A','V']:
  for kind,target in targets.items():
   api.create_case('Stage fault policy '+initial+' '+kind)
   if initial=='V':api.advance('V')
   counter=api.root/('stage-fault-'+initial+'-'+kind+'-executions')
   code=('from pathlib import Path\nwith Path('+repr(str(counter))+').open("a") as stream:stream.write("once\\n")\n'
    'print("ACTUAL_TYPED_STAGE_POLICY_FAILURE",flush=True)\nraise SystemExit(29)\n')
   put(api,'Ref/stage-fault.py',code)
   submitted=write(api,'POST',api.prefix+'/testing/stage-failure',{'job_type':kind})
   identifier=submitted['job_id'];job=api.wait_job(identifier,expected='failed')
   assert job['type']==kind and job['exit_code']==29 and job['attempt']['process_stop']['verified']
   assert 'ACTUAL_TYPED_STAGE_POLICY_FAILURE' in job['log_backlog']
   route='/jobs/'+str(identifier)+'/testing/stage-fallback'
   def applied():
    value=api.call('GET',route)
    return value if value['applied'] is not None else None
   observed=wait_for(applied,'Typed stage-policy failure did not drain and apply')
   expected=target if initial=='V' else 'A'
   result=observed['applied']['result']
   assert observed['actual_stop_and_drain_verified'] and observed['stage']==expected
   assert result['previous_state']==initial and result['target_state']==target
   assert result['regressed']==(initial=='V' and target!='V')
   dictionary=read_settled(api,api.prefix+'/caseDict');assert dictionary['workflow_state']==expected
   before=read_settled(api,api.prefix+'/raw?path=0-caseDict/case-events.jsonl',raw=True)
   events=[json.loads(line) for line in before.decode().splitlines() if line.strip()]
   matching=[e for e in events if e.get('type')=='workflow.fallback' and e.get('job_id')==identifier]
   assert len(matching)==1 and matching[0]['workflow_state']==expected
   replay=write(api,'POST',route,{});assert replay['replayed']
   assert api.call('GET',api.prefix+'/caseDict')==dictionary
   assert api.call('GET',api.prefix+'/raw?path=0-caseDict/case-events.jsonl',raw=True)==before
   assert counter.read_text().splitlines()==['once']
   checks.append({'job_type':kind,'initial_state':initial,'target_state':target,'actual_state':expected,
    'job_id':identifier,'actual_exit_code':29,'actual_native_executions':1,'regressed':result['regressed'],
    'actual_stop_and_drain_verified':True,'file_timeline_count':1,'idempotent_replay_no_changes':True})
   (api.root/'failure-stage-policy-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
 report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'real_model_core_calls':0,
  'actual_exit_processes':len(checks),'checks':checks,
  'scope':'Typed failure-stage protocol faults with actual stopped processes; V is explicit developer state, not an engineering evidence baseline; no CAE solver outcome claimed'}
 assert len(checks)==14
 (api.root/'failure-stage-policy.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent actual failure-stage policy evidence readback')
 return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
