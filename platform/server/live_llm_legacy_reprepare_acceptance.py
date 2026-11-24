"""Actual fixed-argv history, explicit registered reprepare and real model readback."""
import asyncio,hashlib,io,json,time,uuid
from pathlib import Path
from urllib.parse import quote
from pypdf import PdfReader
from live_llm_job_repeat_acceptance import main,put,repeat,write

FIXTURE=r'''
import asyncio,sys,uuid
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.deps import get_current_user
from app.models import User,Job,JobAttempt
from app.routers.workspaces import get_owned_case
from app.services import job_execution,job_delivery
from app.services.case_write_capacity import committing
from app.services.durable_events import json_text
from app.services.workflow_state import ExecutionConflict
from app.services.job_repeat_parameters import public_parameters
from app.services.step_handlers import get_handler

@app.post('/api/v1/cases/{case_id}/testing/repeat-public-parameters')
async def repeat_parameters(case_id:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 await get_owned_case(case_id,user,db,write=True)
 def check():
  import hashlib
  from pathlib import Path
  from app.services.calculix import calculix_executable
  from app.services.su2 import su2_executable
  from app.services.paraview import paraview_executable
  records=[]
  for key,parameters,resolve in (
    ('calculix.solve',{'input_path':'C-run/calculix/beam.inp'},calculix_executable),
    ('su2.solve',{'config_path':'C-run/su2/model.cfg'},su2_executable),
    ('paraview.run',{'script_path':'D-post/scripts/model.py'},lambda:paraview_executable('pvpython'))):
   handler=get_handler(key);configured=str(Path(resolve()).resolve())
   value=public_parameters(handler,parameters)
   assert value['executable']==configured and Path(configured).is_file()
   assert public_parameters(handler,{**parameters,'executable':configured})==value
   try:public_parameters(handler,{**parameters,'executable':str(Path(sys.executable).resolve())})
   except ExecutionConflict as error:assert error.code=='invalid_request'
   else:raise AssertionError('Arbitrary replacement executable was accepted')
   if key=='su2.solve':assert value['input_path']==parameters['config_path'] and 'config_path' not in value
   if key=='paraview.run':assert value['backend']=='pvpython'
   records.append({'handler':key,'actual_configured_binary_sha256':hashlib.sha256(Path(configured).read_bytes()).hexdigest(),
    'server_resolves_tool':True,'matching_old_parameter_accepted':True,'replacement_tool_refused':True,
    'scope':'protocol and real configured file; native solver repeat not claimed'})
  return records
 return await asyncio.to_thread(check)

@app.post('/api/v1/cases/{case_id}/testing/legacy-fixed-argv')
async def legacy_fixed(case_id:int,body:dict,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True)
 assert body['job_type'] in {'geometry','convert'}
 async with committing(db,case,content_write=False):
  if body.get('unsealed_history'):
   job=Job(case_id=case.id,type=body['job_type'],command='Imported history with no execution proof',status='failed')
   db.add(job);await db.flush();identifier=job.id
  else:
   spec={'command':'Historical fixed argv','case_prefix':case.s3_prefix,'runtime':'windows',
    'argv':[sys.executable,'Ref/legacy.py'],'evidence_run_dir':None,'timeout_seconds':300,'max_log_bytes':16777216}
   job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=uuid.uuid4().hex,
    digest=job_execution.request_hash({'actual_legacy_argv':uuid.uuid4().hex}),job_type=body['job_type'],
    command=spec['command'],spec=spec,input_paths=body['input_paths'])
   await job_delivery.request(db,job,user_id=user.id,mode='local');identifier=job.id
 if not body.get('unsealed_history'):await job_delivery.deliver(identifier)
 return {'job_id':identifier}

@app.post('/api/v1/jobs/{job_id}/testing/remove-drain-proof')
async def remove_drain(job_id:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=True)
 async with committing(db,case,content_write=False):
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
  assert job.status=='failed' and attempt.phase=='failed'
  attempt.write_checkpoint_json='{}'
 return {'removed_from_owned_protocol_fixture':True}
'''


def choices(api,identifier,*,available=True):
    deadline=time.monotonic()+30
    while True:
        value=api.call('GET','/jobs/'+str(identifier)+'/operations')
        if value['reprepare']['available']==available:return value
        if time.monotonic()>deadline:raise TimeoutError('Legacy reprepare availability did not settle: '+json.dumps(value))
        time.sleep(.1)


def body_for(option,target,parameters):
    return {'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
        'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence'],
        'parameters':parameters,'reprepare_handler':{'key':target,'version':'1'}}


def scenario(api,*,store=None,**_):
    checks=[]
    for kind,target in (('geometry','geometry.export'),('convert','latex.build')):
        api.create_case('Actual legacy '+kind+' reprepare')
        executions=api.root/('actual-legacy-executions-'+kind);started=api.root/('actual-legacy-started-'+kind)
        release=api.root/('actual-legacy-release-'+kind)
        code="from pathlib import Path\nimport time\nwith Path("+repr(str(executions))+").open('a') as f:f.write('once\\n')\nPath("+repr(str(started))+").touch()\nwhile not Path("+repr(str(release))+").exists():time.sleep(.05)\nraise SystemExit(23)\n"
        put(api,'Ref/legacy.py',code)
        if kind=='geometry':
            path='A-geo/geometry.py';old="import cadquery as cq\ndef build_geometry(ctx):return {'box':cq.Workplane('XY').box(1,1,1)}\n"
            current=old.replace('box(1,1,1)','box(2,2,2)');parameters={}
        else:
            path='E-report/latex/main.tex';old='\\documentclass{article}\n\\begin{document}\nOldLegacySource\n\\end{document}\n'
            current=old.replace('OldLegacySource','CurrentRepreparedSource')
            parameters={'main':path,'recipe':'pdflatex','resources':[]}
        saved=put(api,path,old)
        if saved.get('auto_preview_job'):api.wait_job(saved['auto_preview_job'])
        original=write(api,'POST',api.prefix+'/testing/legacy-fixed-argv',{'job_type':kind,'input_paths':['Ref/legacy.py',path]})['job_id']
        deadline=time.monotonic()+30
        while not started.exists():
            if time.monotonic()>deadline:raise TimeoutError('Actual fixed argv did not execute')
            time.sleep(.05)
        running=choices(api,original,available=False)
        assert not running['retry']['available'] and not running['reprepare']['available']
        refused=repeat(api,original,body_for(running,target,parameters),uuid.uuid4().hex,expect=409)
        assert refused['detail']['code'] in {'state_conflict','recovery_required'}
        release.touch();failed=api.wait_job(original,expected='failed')
        assert failed['exit_code']==23 and failed['attempt']['process_stop']['verified']
        assert failed['attempt'].get('handler') is None
        option=choices(api,original)
        assert not option['retry']['available'] and not option['rerun']['available'] and not option['resume']['available']
        assert {'key':target,'version':'1'} in option['reprepare']['handler_choices']
        for action in ('retry','resume'):
            body={'action':action,'source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
                'reprepare_handler':{'key':target,'version':'1'}}
            denied=repeat(api,original,body,uuid.uuid4().hex,expect=409);assert denied['detail']['code']=='invalid_request'
        body=body_for(option,target,parameters)
        wrong=repeat(api,original,{**body,'reprepare_handler':{'key':'mesh.generate','version':'1'}},uuid.uuid4().hex,expect=409)
        assert wrong['detail']['code']=='invalid_request'
        malformed=repeat(api,original,{**body,'reprepare_handler':{'key':target,'version':'1','command':'override'}},uuid.uuid4().hex,expect=409)
        assert malformed['detail']['code']=='invalid_request'
        updated=put(api,path,current)
        if updated.get('auto_preview_job'):api.wait_job(updated['auto_preview_job'])
        stale=repeat(api,original,body,uuid.uuid4().hex,expect=409);assert stale['detail']['code']=='input_stale'
        option=choices(api,original);body=body_for(option,target,parameters);key=uuid.uuid4().hex
        created=repeat(api,original,body,key);done=api.wait_job(created['id'],timeout=360)
        assert done['attempt']['handler']['key']==target and done['attempt']['number']==1 and done['attempt']['root_job_id']==done['id']
        assert done['attempt']['workspace']['files'][path]['sha256']==hashlib.sha256(current.encode()).hexdigest()
        assert done['attempt']['outputs']['publication']['status']=='committed'
        operation=done['attempt']['operation'];assert operation['source_job_id']==original and operation['source_attempt_id']==failed['attempt']['id']
        assert operation['reprepared_handler']=={'key':target,'version':'1'}
        if kind=='geometry':
            import cadquery as cq
            result=api.call('GET',api.prefix+'/raw?path=A-geo/exports/box.step',raw=True)
            exported=api.root/'reprepared-box.step';exported.write_bytes(result)
            assert abs(cq.importers.importStep(str(exported)).val().Volume()-8)<1e-9
        else:
            publication=done['attempt']['outputs']['publication']['id'];output='E-report/latex/build/main.pdf'
            result=api.call('GET',api.prefix+'/latex/artifact?publication_id='+publication+'&path='+quote(output),raw=True)
            actual=''.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(result)).pages)
            assert 'CurrentRepreparedSource' in actual and 'OldLegacySource' not in actual
        replay=repeat(api,original,body,key);assert replay['id']==done['id'] and not replay['created']
        assert executions.read_text().splitlines()==['once'] and api.call('GET','/jobs/'+str(original))['status']=='failed'
        assert not choices(api,done['id'],available=False)['reprepare']['available']
        history=write(api,'POST',api.prefix+'/testing/legacy-fixed-argv',{'job_type':kind,'unsealed_history':True})['job_id']
        unsealed=choices(api,history,available=False);assert unsealed['source_attempt_id'] is None
        assert unsealed['reprepare']['reason']['code']=='recovery_required'
        write(api,'POST','/jobs/'+str(original)+'/testing/remove-drain-proof',{})
        damaged=choices(api,original,available=False);assert damaged['reprepare']['reason']['code']=='recovery_required'
        denied=repeat(api,original,body_for(damaged,target,parameters),uuid.uuid4().hex,expect=409)
        assert denied['detail']['code']=='recovery_required' and executions.read_text().splitlines()==['once']
        checks.append({'legacy_job_type':kind,'target_handler':target,'source_job':original,'new_job':done['id'],
            'actual_fixed_argv_executed_once':True,'running_source_refused':True,'retry_resume_refused':True,
            'wrong_kind_and_malformed_handler_refused':True,'stale_revision_refused':True,'current_inputs_new_root':True,
            'actual_registered_artifact_verified':True,'formal_publication':True,'source_relationship_and_old_failure_preserved':True,
            'idempotent_reprepare':True,'missing_drain_refused':True,'unsealed_history_protocol_fixture_refused':True})
        (api.root/'legacy-reprepare-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':checks,
        'unsealed_rows_are_protocol_fixtures':True,'model_selects_reprepare':False}
    # The deliberately damaged historical cases remain blocked. Readback uses
    # its own clean case and authenticates the complete scenario report hash.
    api.create_case('Clean legacy acceptance model readback')
    report['configured_tool_parameter_protocol']=api.call('POST',api.prefix+'/testing/repeat-public-parameters',{})
    (api.root/'legacy-reprepare.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
