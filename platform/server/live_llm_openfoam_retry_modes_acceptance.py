"""Actual fresh-solver retry and frozen continuation retry, distinct from resume."""
import asyncio,hashlib,json,time,uuid
from functools import partial
from pathlib import Path
from live_llm_job_repeat_acceptance import main,repeat
from live_llm_solve_resume_acceptance import scenario,TIMING_FIXTURE

RUN='C-run/case-1'
def at_checkpoint(api,identifier,root,label):
 deadline=time.monotonic()+90;work=None
 while True:
  current=api.call('GET','/jobs/'+str(identifier))
  if current['attempt'].get('workspace'):work=root/current['attempt']['workspace']['run_path']
  if work is not None and all((work/RUN/label/name).is_file() for name in ('U','p')):break
  assert current['status'] in {'queued','running'},current['status']
  if time.monotonic()>deadline:raise TimeoutError('Actual retry did not write '+label)
  time.sleep(.02)
 api.call('POST','/jobs/'+str(identifier)+':cancel')
 deadline=time.monotonic()+60
 while True:
  stopped=api.call('GET','/jobs/'+str(identifier));option=api.call('GET','/jobs/'+str(identifier)+'/operations')
  if stopped['status']=='cancelled' and stopped['attempt'].get('process_stop') and option['retry']['available']:break
  if time.monotonic()>deadline:raise TimeoutError('Actual retried solver did not drain')
  time.sleep(.05)
 assert stopped['attempt']['process_stop']['verified']
 return stopped,option,work

def positive(api,*,store,case_root,work,manifest,source_job,source_attempt,original_control,flow_id):
 original=api.call('GET','/jobs/'+str(source_job));option=api.call('GET','/jobs/'+str(source_job)+'/operations')
 retry={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
 first_key=uuid.uuid4().hex;created=repeat(api,source_job,retry,first_key)
 fresh_retry,option,retry_work=at_checkpoint(api,created['id'],case_root,'0.005')
 assert fresh_retry['attempt']['handler']['key']=='solve.openfoam' and fresh_retry['attempt']['number']==2
 assert fresh_retry['attempt']['root_job_id']==source_job and fresh_retry['attempt']['retry_of_attempt_id']==source_attempt
 assert fresh_retry['attempt']['workspace']['files']==original['attempt']['workspace']['files']
 assert fresh_retry['attempt']['operation']['action']=='retry'
 first_log=(retry_work/RUN/'solve.log').read_text(encoding='utf8',errors='replace')
 from app.services.logparse import parse_solver_log
 first_times=parse_solver_log(first_log)['times'];assert first_times and first_times[0]==.0005
 assert not (retry_work/RUN/'resume-checkpoint.json').exists()
 assert repeat(api,source_job,retry,first_key)['id']==fresh_retry['id']
 resume={'action':'resume','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],'parameters':{'time':'0.005'}}
 resumed=repeat(api,fresh_retry['id'],resume,uuid.uuid4().hex)
 cancelled_resume,option,resume_work=at_checkpoint(api,resumed['id'],case_root,'0.01')
 assert cancelled_resume['attempt']['handler']['key']=='solve.openfoam.resume' and cancelled_resume['attempt']['number']==3
 frozen_checkpoint=(resume_work/RUN/'resume-checkpoint.json').read_bytes();checkpoint=json.loads(frozen_checkpoint)
 assert checkpoint['time']==.005 and checkpoint['source']['attempt_id']==fresh_retry['attempt']['id']
 retry_resume={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
 second_key=uuid.uuid4().hex;new=repeat(api,cancelled_resume['id'],retry_resume,second_key);done=api.wait_job(new['id'],timeout=180)
 assert done['attempt']['handler']['key']=='solve.openfoam.resume' and done['attempt']['number']==4
 assert done['attempt']['root_job_id']==source_job and done['attempt']['retry_of_attempt_id']==cancelled_resume['attempt']['id']
 assert done['attempt']['operation']['action']=='retry'
 assert done['attempt']['workspace']['files']==cancelled_resume['attempt']['workspace']['files']
 assert done['attempt']['outputs']['publication']['status']=='committed'
 final_work=case_root/done['attempt']['workspace']['run_path']
 assert (final_work/RUN/'resume-checkpoint.json').read_bytes()==frozen_checkpoint
 assert repeat(api,cancelled_resume['id'],retry_resume,second_key)['id']==done['id']
 evidence=api.call('GET',api.prefix+'/solve/results')['evidence']
 assert evidence['configuration']['start_time']==.005 and evidence['parsed_log']['times'][0]==.0055
 assert evidence['summary']['last_time']==.5
 # Rerun the original fresh handler itself, using explicit current inputs.
 option=api.call('GET','/jobs/'+str(source_job)+'/operations')
 rerun={'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
  'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence'],'parameters':{'run_dir':RUN}}
 baseline=repeat(api,source_job,rerun,uuid.uuid4().hex);baseline=api.wait_job(baseline['id'],timeout=180)
 assert baseline['attempt']['handler']['key']=='solve.openfoam' and baseline['attempt']['number']==1
 assert baseline['attempt']['root_job_id']==baseline['id'] and baseline['attempt']['outputs']['publication']['status']=='committed'
 fresh_work=case_root/baseline['attempt']['workspace']['run_path']
 assert not (fresh_work/RUN/'resume-checkpoint.json').exists()
 import numpy as np
 import vtk
 from vtk.util.numpy_support import vtk_to_numpy
 def fields(root):
  reader=vtk.vtkOpenFOAMReader();reader.SetFileName(str(root/RUN/'case-1.foam'));reader.CreateCellToPointOff()
  reader.UpdateInformation();reader.EnableAllCellArrays();reader.UpdateTimeStep(.5);grid=reader.GetOutput().GetBlock(0)
  assert grid.GetNumberOfCells()==400
  return {name:vtk_to_numpy(grid.GetCellData().GetArray(name)).copy() for name in ('U','p')}
 a,b=fields(final_work),fields(fresh_work)
 differences={name:float(np.max(np.abs(a[name]-b[name]))) for name in a}
 assert all(np.isfinite(a[name]).all() and np.isfinite(b[name]).all() and np.allclose(a[name],b[name],rtol=2e-3,atol=2e-5) for name in a)
 assert api.call('GET',api.prefix+'/raw?path='+RUN+'/system/controlDict',raw=True)==original_control
 for identifier in [source_job,fresh_retry['id'],cancelled_resume['id']]:assert api.call('GET','/jobs/'+str(identifier))['status']=='cancelled'
 assert api.call('GET','/workflow-runs/'+flow_id)['status']=='cancelled'
 if store:assert not work.exists(),'S3 retry must not reconstruct the old source runtime'
 report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'model_selects_operations':False,
  'actual_solver':'icoFoam','actual_cells':400,'source_job':source_job,'fresh_retry_job':fresh_retry['id'],
  'first_resume_job':cancelled_resume['id'],'frozen_resume_retry_job':done['id'],'fresh_rerun_job':baseline['id'],
  'fresh_retry_same_frozen_files':True,'fresh_retry_first_time':first_times[0],
  'continuation_retry_same_frozen_files':True,'continuation_retry_checkpoint_time':.005,'cancelled_resume_wrote_time':.01,
  'continuation_retry_first_time':.0055,'continuation_retry_preserved_checkpoint_sha256':hashlib.sha256(frozen_checkpoint).hexdigest(),
  'attempt_numbers':[1,2,3,4],'same_original_root':True,'same_key_replays_unique':True,
  'actual_resume_retry_and_fresh_rerun_committed':True,'field_max_abs_differences':differences,
  'cancelled_histories_and_workflow_preserved':True,'original_live_controls_unchanged':True,
  'source_runtime_not_reconstructed':bool(store),'scope':'retry reuses frozen inputs; resume selects a written native time; rerun uses current input/new root'}
 (api.root/'openfoam-retry-modes.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent actual OpenFOAM retry-mode evidence model readback');return report

if __name__=='__main__':asyncio.run(main(selected_override=partial(scenario,positive_override=positive),fixture_override=TIMING_FIXTURE,store_volume_max=16))
