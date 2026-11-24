"""Actual IR conversion and non-root wmake/CDLL retry/rerun acceptance."""
import asyncio,hashlib,json,uuid
from pathlib import Path
from app.services.flotherm_ir import build_ir
from live_llm_job_repeat_acceptance import main,put,repeat,write
from live_llm_engineering_repeat_acceptance import case_root,retry_job,rerun_job,frozen_retry

FIXTURE=r'''
from pathlib import Path
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.db import SessionLocal,get_db
from app.deps import get_current_user
from app.models import Case,User,ArtifactPublication
from app.routers.workspaces import get_owned_case
from app.services import job_execution as flotherm_repeat_execution
from app.services.artifact_publication import read_file
from app.services.jobrunner import win_to_wsl_path
import asyncio,subprocess
flotherm_repeat_root=Path(__file__).parent
flotherm_repeat_release=flotherm_repeat_execution.release_process
async def flotherm_receipt_collision(owner,process):
 mode_path=flotherm_repeat_root/'flotherm-repeat-fault'
 mode=mode_path.read_text(encoding='utf8') if mode_path.exists() else 'none'
 paths={'flotherm.convert':'.vibeflow/flotherm-convert/receipt.json',
  'flotherm.build-library':'.vibeflow/flotherm-library/receipt.json'}
 if mode in paths:
  async with SessionLocal() as db:
   job,attempt=await flotherm_repeat_execution._owned(db,owner)
   document=json.loads(attempt.input_manifest_json)
   if document.get('handler',{}).get('key')==mode:
    case=await db.get(Case,job.case_id)
    root=storage._fs_path(case.s3_prefix,document['workspace']['run_path']).resolve()
    identifier=job.id;attempt_id=attempt.id
   else:root=None
  if root is not None:
   target=(root/paths[mode]).resolve();assert target.is_relative_to(root)
   target.parent.mkdir(parents=True,exist_ok=True)
   with target.open('xb') as stream:stream.write(b'ACTUAL_PRIVATE_FLOTHERM_RECEIPT_COLLISION')
   (flotherm_repeat_root/('actual-flotherm-collision-'+str(identifier)+'.json')).write_text(json.dumps(
    {'job_id':identifier,'attempt_id':attempt_id,'handler':mode,'path':paths[mode],'actual_file_created':True}),encoding='utf8')
 return await flotherm_repeat_release(owner,process)
flotherm_repeat_execution.release_process=flotherm_receipt_collision

@app.post('/api/v1/cases/{case_id}/testing/flotherm-library-value')
async def actual_library_value(case_id:int,body:dict,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False)
 publication=await db.get(ArtifactPublication,body['publication_id'])
 assert publication is not None and publication.case_id==case.id and publication.status=='committed'
 assert publication.kind.startswith('flotherm-library:')
 data=await asyncio.to_thread(read_file,case.s3_prefix,publication,body['path'])
 assert data.startswith(b'\x7fELF')
 library=storage._fs_path(case.s3_prefix,'.vibeflow/publications/'+publication.id+'/files/'+body['path']).resolve()
 base=storage._fs_path(case.s3_prefix,'.vibeflow/publications/'+publication.id+'/files').resolve()
 assert library.is_relative_to(base) and library.read_bytes()==data
 from app.services.runtime_config import get_runtime_setting
 code='import ctypes,json,sys;lib=ctypes.CDLL(sys.argv[1]);f=lib.vfFlothermPowerScale;f.restype=ctypes.c_double;print(json.dumps({"value":f()}))'
 process=await asyncio.create_subprocess_exec('wsl.exe','-d',get_runtime_setting('wsl_distro','vibeflow-linux'),
  '--user','nobody','--exec','python3','-I','-S','-c',code,win_to_wsl_path(library),
  stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
 try:output,error=await asyncio.wait_for(process.communicate(),30)
 finally:
  if process.returncode is None:process.kill();await process.wait()
 assert process.returncode==0,'Actual owned-library observer failed: '+error.decode('utf8',errors='replace')[:500]
 result=json.loads(output);result['actual_immutable_library_called']=True
 return result
'''


def fail(api,kind,endpoint,parameters):
    mode=api.root/'flotherm-repeat-fault';mode.write_text(kind,encoding='utf8')
    first=write(api,'POST',api.prefix+endpoint,parameters)['job_id'];failed=api.wait_job(first,expected='failed',timeout=360)
    witness=json.loads((api.root/('actual-flotherm-collision-'+str(first)+'.json')).read_text(encoding='utf8'))
    assert witness['actual_file_created'] and witness['attempt_id']==failed['attempt']['id']
    assert 'FileExistsError' in failed['log_backlog'] and failed['attempt']['process_stop']['verified']
    mode.write_text('none',encoding='utf8');return first,failed


def occupied(api,identifier,parameters):
    option=api.call('GET','/jobs/'+str(identifier)+'/operations')
    body={'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
        'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence'],'parameters':parameters}
    result=repeat(api,identifier,body,uuid.uuid4().hex,expect=409)
    assert result['detail']['code']=='output_exists'
    return result


def convert(api,store):
    api.create_case('Actual FloTHERM conversion repeat')
    ir=build_ir('source.ecxml',b'chip',[{'id':'chip','type':'box','material':'silicon','mapping':'converted','power_w':5,
        'shape':{'kind':'box','origin':[0,0,0],'dimensions':[1,1,1]}}],{'silicon':{'conductivity_w_mk':130,'rho_cp':1000}})
    put(api,'Ref/ir.json',json.dumps(ir));put(api,'Ref/settings.json',json.dumps({'power':{'chip':{'value':5,'unit':'W'}}}))
    parameters={'ir_path':'Ref/ir.json','settings_path':'Ref/settings.json','output_dir':'C-run/thermal-original'}
    first,failed=fail(api,'flotherm.convert','/flotherm:convert',parameters)
    done,body,key=retry_job(api,first);frozen_retry(failed,done,'flotherm.convert')
    old_path=parameters['output_dir']+'/conversion-coverage.json'
    old=api.call('GET',api.prefix+'/raw?path='+old_path,raw=True);assert json.loads(old)['summary']['input_power_w']==5
    root=case_root(api,done,store);publication=done['attempt']['outputs']['publication']['id']
    bundle=root/'.vibeflow/publications'/publication/'files'/old_path;historical=bundle.read_bytes();assert historical==old
    refused=occupied(api,first,parameters);assert api.call('GET',api.prefix+'/raw?path='+old_path,raw=True)==old
    settings=json.dumps({'power':{'chip':{'value':7500,'unit':'mW'}}});put(api,'Ref/settings.json',settings)
    current={**parameters,'output_dir':'C-run/thermal-current'};fresh=rerun_job(api,first,current)
    coverage=api.call('GET',api.prefix+'/raw?path='+current['output_dir']+'/conversion-coverage.json',raw=True)
    assert json.loads(coverage)['summary']['input_power_w']==7.5 and bundle.read_bytes()==historical
    assert fresh['attempt']['workspace']['files']['Ref/settings.json']['sha256']==hashlib.sha256(settings.encode()).hexdigest()
    return {'handler':'flotherm.convert','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_conversion_power_w':[5,7.5],'retry_original_files':True,'rerun_current_settings_new_output':True,
        'occupied_output_refused':refused,'old_immutable_artifact_preserved':True,'commercial_flotherm_solver_claimed':False}


def library(api,store):
    api.create_case('Actual FloTHERM library repeat')
    source='Ref/helper-source';original='extern "C" double vfFlothermPowerScale() { return 1.0; }\n'
    put(api,source+'/flothermHelper.C',original)
    put(api,source+'/Make/files','flothermHelper.C\n\nLIB = $(PWD)/libflothermHelper\n')
    put(api,source+'/Make/options','EXE_INC =\nLIB_LIBS =\n')
    parameters={'source_dir':source,'destination_dir':'UDF/helper-original'}
    first,failed=fail(api,'flotherm.build-library','/flotherm:build-library',parameters)
    done,body,key=retry_job(api,first);frozen_retry(failed,done,'flotherm.build-library')
    path=parameters['destination_dir']+'/libflothermHelper.so';publication=done['attempt']['outputs']['publication']['id']
    root=case_root(api,done,store);old=api.call('GET',api.prefix+'/raw?path='+path,raw=True);assert old[:4]==b'\x7fELF'
    observed=api.call('POST',api.prefix+'/testing/flotherm-library-value',{'publication_id':publication,'path':path})
    assert observed['value']==1 and observed['actual_immutable_library_called']
    bundle=root/'.vibeflow/publications'/publication/'files'/path;historical=bundle.read_bytes();assert historical==old
    refused=occupied(api,first,parameters)
    current=original.replace('return 1.0','return 2.0');put(api,source+'/flothermHelper.C',current)
    fresh_parameters={**parameters,'destination_dir':'UDF/helper-current'};fresh=rerun_job(api,first,fresh_parameters)
    current_pub=fresh['attempt']['outputs']['publication']['id'];new_path=fresh_parameters['destination_dir']+'/libflothermHelper.so'
    observed=api.call('POST',api.prefix+'/testing/flotherm-library-value',{'publication_id':current_pub,'path':new_path})
    assert observed['value']==2 and observed['actual_immutable_library_called'] and bundle.read_bytes()==historical
    assert fresh['attempt']['workspace']['files'][source+'/flothermHelper.C']['sha256']==hashlib.sha256(current.encode()).hexdigest()
    return {'handler':'flotherm.build-library','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_wmake_and_cdll':True,'actual_exported_function_values':[1,2],'retry_original_files':True,
        'rerun_current_cpp_new_destination':True,'occupied_output_refused':refused,'old_immutable_artifact_preserved':True,
        'function_value_observer_scope':'actual separate non-root WSL read observer of verified immutable libraries'}


def scenario(api,*,store=None,**_):
    checks=[]
    for run in (convert,library):
        checks.append(run(api,store));(api.root/'flotherm-repeat-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':checks,'model_selects_operations':False}
    api.create_case('Independent Flotherm model readback')
    (api.root/'flotherm-repeat.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
