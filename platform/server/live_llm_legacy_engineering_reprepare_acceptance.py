"""Real legacy mesh/solve/postprocess argv, explicit native reprepare and readback.

The old Python program really executes once and exits 23. No model, solver,
process or publication response is substituted. Missing history/drain records
are owned protocol fault fixtures and are identified separately in the report.
"""
import asyncio,hashlib,json,re,time,uuid
from pathlib import Path

from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main,put,repeat,write
from live_llm_legacy_reprepare_acceptance import FIXTURE as LEGACY_FIXTURE,choices,body_for
from live_llm_post_report_repeat_acceptance import RUN,seed_thermal,raw,extraction
from live_llm_engineering_repeat_acceptance import case_root

FIXTURE=LEGACY_FIXTURE.replace("in {'geometry','convert'}", "in {'mesh','solve','postprocess'}")
assert FIXTURE!=LEGACY_FIXTURE

def wait_file(path):
 deadline=time.monotonic()+30
 while not path.exists():
  if time.monotonic()>deadline:raise TimeoutError('Actual legacy argv did not start')
  time.sleep(.05)

def scenario(api,*,store=None,**_):
 api.create_case('Real legacy engineering explicit reprepare')
 seed_thermal(api);checks=[];histories=[]
 for kind,target in (('mesh','mesh.generate'),('solve','solve.openfoam'),('postprocess','postprocess.run')):
  executions=api.root/('actual-legacy-'+kind+'-executions')
  started=api.root/('actual-legacy-'+kind+'-started');release=api.root/('actual-legacy-'+kind+'-release')
  code="from pathlib import Path\nimport time\nwith Path("+repr(str(executions))+").open('a') as f:f.write('once\\n')\nPath("+repr(str(started))+").touch()\nwhile not Path("+repr(str(release))+").exists():time.sleep(.05)\nraise SystemExit(23)\n"
  put(api,'Ref/legacy.py',code)
  original=write(api,'POST',api.prefix+'/testing/legacy-fixed-argv',
   {'job_type':kind,'input_paths':['Ref/legacy.py','0-caseDict/caseDict']})['job_id']
  wait_file(started)
  running=choices(api,original,available=False)
  refused=repeat(api,original,body_for(running,target,{'run_dir':RUN}),uuid.uuid4().hex,expect=409)
  assert refused['detail']['code'] in {'state_conflict','recovery_required'}
  release.touch();failed=api.wait_job(original,expected='failed')
  assert failed['exit_code']==23 and failed['attempt']['process_stop']['verified'] and failed['attempt'].get('handler') is None
  old_root=case_root(api,failed,store)
  # Capture the immutable execution input instead of relying on live case files.
  frozen=old_root/failed['attempt']['workspace']['run_path']
  assert (frozen/'Ref/legacy.py').read_text(encoding='utf8')==code
  sealed_dictionary=failed['attempt']['workspace']['files']['0-caseDict/caseDict']['sha256']
  option=choices(api,original);assert {'key':target,'version':'1'} in option['reprepare']['handler_choices']
  assert not any(option[action]['available'] for action in ('retry','rerun','resume'))
  body=body_for(option,target,{'run_dir':RUN})
  for invalid in (
   {**body,'reprepare_handler':{'key':'geometry.export','version':'1'}},
   {**body,'reprepare_handler':{'key':target,'version':'unknown'}},
   {**body,'reprepare_handler':{'key':target,'version':'1','command':'replacement'}},
   {**body,'action':'retry'},{**body,'action':'resume'}):
   denied=repeat(api,original,invalid,uuid.uuid4().hex,expect=409)
   assert denied['detail']['code']=='invalid_request'
  denied=repeat(api,original,{**body,'source_version':body['source_version']+1},uuid.uuid4().hex,expect=409)
  assert denied['detail']['code']=='state_conflict'
  denied=repeat(api,original,{**body,'expected_event_sequence':body['expected_event_sequence']+1},uuid.uuid4().hex,expect=409)
  assert denied['detail']['code']=='input_stale'
  # Current configuration differs from the historical seal without invalidating
  # the upstream engineering problem. The obsolete argv itself is now broken.
  put(api,'Ref/legacy.py',"raise RuntimeError('OBSOLETE_ARGV_MUST_NEVER_REPLAY')\n")
  saved=api.call('GET',api.prefix+'/caseDict')
  content=set_block(saved['content'],'report',{'title':'Current explicit '+kind+' preparation'})
  write(api,'PUT',api.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
  current=raw(api,'0-caseDict/caseDict');assert hashlib.sha256(current).hexdigest()!=sealed_dictionary
  denied=repeat(api,original,body,uuid.uuid4().hex,expect=409);assert denied['detail']['code']=='input_stale'
  option=choices(api,original);body=body_for(option,target,{'run_dir':RUN});key=uuid.uuid4().hex
  created=repeat(api,original,body,key);done=api.wait_job(created['id'],timeout=360)
  attempt=done['attempt'];assert attempt['handler']['key']==target and attempt['number']==1 and attempt['root_job_id']==done['id']
  assert attempt['outputs']['publication']['status']=='committed'
  assert attempt['workspace']['files']['0-caseDict/caseDict']['sha256']==hashlib.sha256(current).hexdigest()
  operation=attempt['operation'];assert operation['source_job_id']==original and operation['source_attempt_id']==failed['attempt']['id']
  assert operation['reprepared_handler']=={'key':target,'version':'1'}
  work=case_root(api,done,store)/attempt['workspace']['run_path']
  if kind=='mesh':
   result=api.call('GET',api.prefix+'/mesh/results');assert result['quality']['mesh_ok'] and result['quality']['status']=='pass'
   assert (work/RUN/'constant/polyMesh/points').stat().st_size>100
   artifact={'native':'blockMesh/checkMesh','mesh_ok':True,'quality_pass':True}
  elif kind=='solve':
   text=(work/RUN/'1/T').read_text(encoding='utf8')
   match=re.search(r'internalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;',text,re.S)
   assert match and int(match.group(1))==80
   values=[float(value) for value in match.group(2).split()]
   assert len(values)==80 and max(abs(value-(300+100*((i%10)+.5)/10)) for i,value in enumerate(values))<1e-6
   artifact={'native':'laplacianFoam','actual_cells':80,'actual_final_time':1,'linear_temperature_error_max':max(abs(value-(300+100*((i%10)+.5)/10)) for i,value in enumerate(values))}
  else:
   metrics=extraction(work,done['id'],19)
   artifact={'native':'VTK extraction/Matplotlib','actual_pdf_svg_png':True,'actual_mean_temperature_k':next(v['value'] for v in metrics['metrics'] if v['name']=='mean.T@1')}
  replay=repeat(api,original,body,key);assert replay['id']==done['id'] and not replay['created']
  assert executions.read_text().splitlines()==['once'] and (frozen/'Ref/legacy.py').read_text(encoding='utf8')==code
  assert api.call('GET','/jobs/'+str(original))['status']=='failed'
  checks.append({'legacy_job_type':kind,'target_handler':target,'source_job':original,'new_job':done['id'],
   'actual_fixed_argv_executed_once':True,'obsolete_live_argv_not_replayed':True,'current_inputs_new_root':True,
   'actual_artifact':artifact,'same_key_same_job':True,'wrong_kind_version_and_shape_refused':True,
   'stale_attempt_version_revision_and_event_refused':True,'running_source_refused':True,'retry_resume_refused':True})
  histories.append((kind,target,original,executions))
  (api.root/'legacy-engineering-reprepare-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
 for kind,target,original,executions in histories:
  history=write(api,'POST',api.prefix+'/testing/legacy-fixed-argv',{'job_type':kind,'unsealed_history':True})['job_id']
  unavailable=choices(api,history,available=False)
  assert unavailable['source_attempt_id'] is None and unavailable['reprepare']['reason']['code']=='recovery_required'
 for kind,target,original,executions in histories:
  write(api,'POST','/jobs/'+str(original)+'/testing/remove-drain-proof',{})
  damaged=choices(api,original,available=False);assert damaged['reprepare']['reason']['code']=='recovery_required'
  denied=repeat(api,original,body_for(damaged,target,{'run_dir':RUN}),uuid.uuid4().hex,expect=409)
  assert denied['detail']['code']=='recovery_required' and executions.read_text().splitlines()==['once']
 report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':checks,
  'missing_history_and_drain_are_owned_protocol_faults':True,'model_selects_operations':False,
  'scope':'G1 remaining legacy mesh/solve/postprocess native reprepare; G2/G3 and N5-05 not closed'}
 (api.root/'legacy-engineering-reprepare.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent legacy engineering model readback');return report

if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
