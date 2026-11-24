"""Actual confirmed geometry stage plus real post-commit SQL-owner contention."""
import ast,asyncio,json,uuid
from pathlib import Path
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_mpi_workflow_acceptance import FIXTURE as MPI_FIXTURE

EXTRA=r'''
import asyncio,json
from pathlib import Path
from app.models import Case,Workspace,WorkflowStep
from app.db import SessionLocal
from app.services import file_transactions
from app.services.storage_admission import lock_owner
cleanup_original=file_transactions.finish_committed
async def cleanup_contended(db,case):
 marker=root/'confirmation-cleanup-armed.json';proof=root/'confirmation-cleanup-busy.json'
 if not marker.exists() or proof.exists():return await cleanup_original(db,case)
 request=json.loads(marker.read_text());ready=asyncio.Event();release=asyncio.Event();held_case={}
 async def holder():
  async with SessionLocal() as held:
   current_case=await held.get(Case,case.id);workspace=await held.get(Workspace,current_case.workspace_id)
   await lock_owner(held,workspace.user_id)
   step=await held.get(WorkflowStep,request['step_id'])
   held_case.update(workflow_state=current_case.workflow_state,dict_revision=current_case.dict_revision,
    step_status=step.status,step_version=step.version,owner_lock_actually_acquired=True)
   ready.set();await release.wait()
 task=asyncio.create_task(holder())
 try:
  await asyncio.wait_for(ready.wait(),10)
  try:
   result=await cleanup_original(db,case)
   proof.write_text(json.dumps({'actual_committed_state':held_case,'error_type':None,'status':200,'detail':None,
    'cleanup_returned_normally_under_actual_owner_lock':True},indent=2),encoding='utf8')
   return result
  except BaseException as error:
   value={'actual_committed_state':held_case,'error_type':type(error).__name__,'status':getattr(error,'status_code',None),'detail':getattr(error,'detail',None)}
   proof.write_text(json.dumps(value,indent=2),encoding='utf8');raise
 finally:release.set();await task
file_transactions.finish_committed=cleanup_contended
'''
FIXTURE=MPI_FIXTURE+EXTRA


def scenario(api,*,store=None,**_):
 (api.root/'confirmation-cleanup-entry-source.py').write_bytes(Path(__file__).read_bytes())
 api.create_case('Actual native geometry confirmation cleanup contention')
 program='import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n';ast.parse(program)
 saved=put(api,'A-geo/geometry.py',program)
 if saved.get('auto_preview_job'):
  jid=saved['auto_preview_job'];api.wait_job(jid)
  wait_for(lambda:api.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Actual preview did not drain')
 revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
 flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'geometry','expected_revision':revision,'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
 def waiting():
  current=api.call('GET','/workflow-runs/'+flow['id']);assert current['status'] not in {'failed','cancelled','interrupted'},current
  step=next(s for s in current['steps'] if s['key']=='confirm_B')
  return (current,step) if step['status']=='awaiting_confirmation' else None
 current,step=wait_for(waiting,'Real geometry did not reach B',240)
 producer=next(s for s in current['steps'] if s['key']=='geometry');jid=producer['attempts'][0]['job_id']
 api.wait_job(jid);wait_for(lambda:api.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Actual geometry did not drain')
 wait_for(lambda:not api.call('GET','/jobs/'+str(jid))['completion_pending'],'Actual geometry did not settle')
 (api.root/'confirmation-cleanup-armed.json').write_text(json.dumps({'step_id':step['id']}),encoding='utf8')
 body={'state':'B','confirmed':True,'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']}
 response=api.call('POST',api.prefix+'/caseDict:advance',body,expect=(200,409))
 after=api.call('GET','/workflow-runs/'+flow['id']);dictionary=api.call('GET',api.prefix+'/caseDict')
 lock=json.loads((api.root/'confirmation-cleanup-busy.json').read_text())
 report={'passed':False,'mock_llm_calls':0,'actual_native_geometry_job':jid,'response':response,'actual_lock':lock,
  'after_status':after['status'],'after_state':dictionary['workflow_state'],'after_revision':dictionary['dict_revision']}
 (api.root/'confirmation-cleanup-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 assert lock['actual_committed_state']['step_status']=='succeeded' and lock['actual_committed_state']['workflow_state']=='B'
 assert lock['actual_committed_state']['owner_lock_actually_acquired']
 if store is None:assert lock['status']==409 and lock['detail']['code']=='storage_quota_busy'
 else:assert lock['status']==200 and lock['cleanup_returned_normally_under_actual_owner_lock']
 report['actual_cleanup_busy_observed']=lock['status']==409
 assert not response.get('detail'),'Committed B confirmation returned a pre-admission busy response'
 assert after['status']=='succeeded' and dictionary['workflow_state']=='B' and dictionary['dict_revision']==revision+1
 assert len(next(s for s in after['steps'] if s['key']=='geometry')['attempts'])==1
 report['passed']=True;(api.root/'confirmation-cleanup-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent actual confirmation cleanup evidence readback');return report

if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
