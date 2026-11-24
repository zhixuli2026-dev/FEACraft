"""Actual wmake/CDLL build and 400-cell icoFoam integration retry/rerun."""
import asyncio,hashlib,json,math,re,time
from pathlib import Path
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_document_repeat_acceptance import upload
from live_llm_engineering_repeat_acceptance import await_put,case_root,retry_job,rerun_job,frozen_retry

FIXTURE=r'''
from pathlib import Path
from app import storage
from app.db import SessionLocal
from app.models import Case
from app.services import job_execution as udf_repeat_execution
udf_repeat_root=Path(__file__).parent
udf_repeat_release=udf_repeat_execution.release_process
async def udf_receipt_collision(owner,process):
 mode_path=udf_repeat_root/'udf-repeat-fault'
 mode=mode_path.read_text(encoding='utf8') if mode_path.exists() else 'none'
 if mode in {'udf.build','udf.integrate'}:
  async with SessionLocal() as db:
   job,attempt=await udf_repeat_execution._owned(db,owner);document=json.loads(attempt.input_manifest_json)
   if document.get('handler',{}).get('key')==mode:
    case=await db.get(Case,job.case_id);root=storage._fs_path(case.s3_prefix,document['workspace']['run_path']).resolve()
    identifier=job.id;attempt_id=attempt.id
   else:root=None
  if root is not None:
   target=(root/'.vibeflow/udf/receipt.json').resolve();assert target.is_relative_to(root)
   target.parent.mkdir(parents=True,exist_ok=True)
   with target.open('xb') as stream:stream.write(b'ACTUAL_PRIVATE_UDF_RECEIPT_COLLISION')
   (udf_repeat_root/('actual-udf-collision-'+str(identifier)+'.json')).write_text(json.dumps(
    {'job_id':identifier,'attempt_id':attempt_id,'handler':mode,'actual_file_created':True}),encoding='utf8')
 return await udf_repeat_release(owner,process)
udf_repeat_execution.release_process=udf_receipt_collision
'''


def control_checks():
    from app.services.udf_control import insert_function
    control=insert_function('application icoFoam;\n','myFunctionObject')
    assert insert_function(control,'myFunctionObject',reuse_identical=True)==control
    commented=control.replace('type myFunctionObject;','/* preserved note */ type myFunctionObject;')
    assert insert_function(commented,'myFunctionObject',reuse_identical=True)==commented
    checks=[{'mode':'exact_current_registration','reused_without_rewrite':True},
        {'mode':'comments_in_current_registration','reused_without_rewrite':True}]
    for mode,candidate,name,options in (
        ('strict_initial_submission',control,'myFunctionObject',{}),
        ('other_library',control.replace('libmyFunctionObject.so','other.so'),'myFunctionObject',{'reuse_identical':True}),
        ('extra_settings',control.replace('type myFunctionObject;','type myFunctionObject; enabled false;'),'myFunctionObject',{'reuse_identical':True}),
        ('other_name',control,'differentName',{'reuse_identical':True}),
        ('duplicate_type',control.replace('\n}', '\nother { type myFunctionObject; libs ("libmyFunctionObject.so"); }\n}',1),'myFunctionObject',{'reuse_identical':True})):
        try:insert_function(candidate,name,**options)
        except FileExistsError:pass
        else:raise AssertionError('Conflicting function registration accepted: '+mode)
        checks.append({'mode':mode,'refused_without_overwrite':True})
    from app.services.case_file_delta import restore_source
    from app.services.case_events import CASE_EVENTS_PATH
    original={'files':{CASE_EVENTS_PATH:{'size':1,'sha256':'0'*64,'executable':False}},'directories':['0-caseDict']}
    changed={'size':2,'sha256':'1'*64,'executable':False}
    descriptor={'source':original,'output_files':{CASE_EVENTS_PATH:changed}}
    actual={'files':{CASE_EVENTS_PATH:changed},'directories':['0-caseDict']}
    assert restore_source(actual,descriptor)['files'][CASE_EVENTS_PATH]==changed
    descriptor={'source':original,'deleted_files':{CASE_EVENTS_PATH:original['files'][CASE_EVENTS_PATH]}}
    assert CASE_EVENTS_PATH not in restore_source({'files':{},'directories':['0-caseDict']},descriptor)['files']
    checks.append({'mode':'historical_output_cannot_mask_timeline_change_or_delete','refused_normalization':True})
    return checks


def fail(api,kind,endpoint,parameters):
    mode=api.root/'udf-repeat-fault';mode.write_text(kind,encoding='utf8')
    first=write(api,'POST',api.prefix+endpoint,parameters)['job_id'];failed=api.wait_job(first,expected='failed',timeout=360)
    witness=json.loads((api.root/('actual-udf-collision-'+str(first)+'.json')).read_text(encoding='utf8'))
    assert witness['actual_file_created'] and witness['attempt_id']==failed['attempt']['id']
    assert 'FileExistsError' in failed['log_backlog'] and failed['attempt']['process_stop']['verified']
    mode.write_text('none',encoding='utf8');return first,failed


def build(api,store):
    api.create_case('Actual UDF build repeat')
    name='myFunctionObject';base='UDF/'+name
    write(api,'POST',api.prefix+'/udf/scaffold',{'name':name})
    source=api.call('GET',api.prefix+'/raw?path='+base+'/myFunctionObject.C',raw=True).decode()
    original=source.replace('P0_UDF_OK','P0_UDF_OK_RETRY_ORIGINAL');put(api,base+'/myFunctionObject.C',original)
    first,failed=fail(api,'udf.build','/udf:build',{'name':name})
    done,body,key=retry_job(api,first);frozen_retry(failed,done,'udf.build')
    library_path=base+'/libmyFunctionObject.so';old=api.call('GET',api.prefix+'/raw?path='+library_path,raw=True)
    assert old[:4]==b'\x7fELF' and b'P0_UDF_OK_RETRY_ORIGINAL' in old
    root=case_root(api,done,store);work=root/done['attempt']['workspace']['run_path']
    native=json.loads((work/'.vibeflow/udf/native.json').read_text(encoding='utf8'))
    assert native['uid']!=0 and set(native['steps'])=={'build','load'}
    assert all(v['returncode']==0 for v in native['steps'].values())
    publication=done['attempt']['outputs']['publication']['id'];bundle=root/'.vibeflow/publications'/publication/'files'/library_path
    historical=bundle.read_bytes();assert historical==old
    current=source.replace('P0_UDF_OK','P0_UDF_OK_RERUN_CURRENT');put(api,base+'/myFunctionObject.C',current)
    fresh=rerun_job(api,first,{'name':name,'run_dir':''})
    library=api.call('GET',api.prefix+'/raw?path='+library_path,raw=True)
    assert library[:4]==b'\x7fELF' and b'P0_UDF_OK_RERUN_CURRENT' in library and library!=old
    assert bundle.read_bytes()==historical and fresh['attempt']['workspace']['files'][base+'/myFunctionObject.C']['sha256']==hashlib.sha256(current.encode()).hexdigest()
    return {'handler':'udf.build','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_nonroot_wmake_cdll':True,'actual_original_and_current_elf_markers':True,'retry_original_files':True,
        'rerun_current_cpp':True,'old_immutable_artifact_preserved':True},library


def seed_solve(api):
    saved=api.call('GET',api.prefix+'/caseDict')
    content=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
        {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
    content=set_block(content,'numericalMethod',{'solver':'icoFoam'})
    content=set_block(content,'postProcessing',{'runDir':'C-run/case-1'})
    write(api,'PUT',api.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
    for path,value in cavity_2d().items():put(api,path,value)
    await_put(api,'A-geo/geometry.py',"import cadquery as cq\ndef build_geometry(ctx):return {'box':cq.Workplane('XY').box(1,1,1)}\n")
    revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
    flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'solve','run_dir':'C-run/case-1','expected_revision':revision,
        'budget':{'timeout_seconds':120,'max_log_bytes':16777216}})
    def wait(key,status):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            value=api.call('GET','/workflow-runs/'+flow['id']);step=next(s for s in value['steps'] if s['key']==key)
            if step['status']==status:return value,step
            if value['status'] in {'failed','interrupted','cancelled'}:raise AssertionError('Actual serial prerequisite failed: '+json.dumps(value)[:1500])
            time.sleep(.1)
        raise TimeoutError('Actual serial workflow did not reach '+key)
    for key in ('confirm_B','confirm_C'):
        value,step=wait(key,'awaiting_confirmation')
        for producer in value['steps']:
            if producer['status']=='succeeded' and producer['attempts']:api.wait_job(producer['attempts'][-1]['job_id'])
        api.call('POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
            'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
    value,step=wait('solve','succeeded');job=api.wait_job(step['attempts'][-1]['job_id'])
    assert job['attempt']['outputs']['publication']['status']=='committed'
    return job


def vectors(path):
    text=path.read_text(encoding='utf8')
    match=re.search(r'internalField\s+nonuniform\s+List<vector>\s+(\d+)\s*\((.*?)\)\s*;',text,re.S)
    assert match,'Actual icoFoam did not write an explicit vector field'
    values=[tuple(float(x) for x in row.split()) for row in re.findall(r'\(([^()]*)\)',match.group(2))]
    assert len(values)==int(match.group(1))==400
    assert all(len(v)==3 and all(math.isfinite(x) and abs(x)<5 for x in v) for v in values)
    return values


def integrate(api,store,library):
    api.create_case('Actual UDF integration repeat');name='myFunctionObject';base='UDF/'+name
    upload(api,base+'/libmyFunctionObject.so',library,'application/octet-stream')
    seed_solve(api)
    original_control=api.call('GET',api.prefix+'/raw?path=C-run/case-1/system/controlDict',raw=True)
    parameters={'name':name,'run_dir':'C-run/case-1'}
    first,failed=fail(api,'udf.integrate','/udf:integrate',parameters)
    assert api.call('GET',api.prefix+'/raw?path=C-run/case-1/system/controlDict',raw=True)==original_control
    done,body,key=retry_job(api,first);frozen_retry(failed,done,'udf.integrate')
    root=case_root(api,done,store);work=root/done['attempt']['workspace']['run_path']
    assert 'P0_UDF_OK_RERUN_CURRENT' in (work/'C-run/case-1/udf-solve.log').read_text(encoding='utf8')
    old=vectors(work/'C-run/case-1/0.5/U');proof=json.loads((work/'C-run/case-1/udf-evidence.json').read_text(encoding='utf8'))
    assert proof['summary']['finished_cleanly'] and proof['summary']['last_time']==.5
    publication=done['attempt']['outputs']['publication']['id'];path='C-run/case-1/0.5/U'
    bundle=root/'.vibeflow/publications'/publication/'files'/path;historical=bundle.read_bytes()
    current=cavity_2d()['C-run/case-1/0/U'].replace('uniform (1 0 0)','uniform (0.5 0 0)')
    put(api,'C-run/case-1/0/U',current)
    (api.root/'udf-integration-progress.json').write_text(json.dumps({'actual_retry_integrated_and_published':True,
        'source_job':first,'retry_job':done['id'],'original_lid_speed':1,'current_lid_speed':.5}),encoding='utf8')
    fresh=rerun_job(api,first,parameters);work=root/fresh['attempt']['workspace']['run_path'];new=vectors(work/path)
    difference=max(abs(a-b) for old_v,new_v in zip(old,new) for a,b in zip(old_v,new_v))
    assert difference>1e-4 and bundle.read_bytes()==historical
    control=(work/'C-run/case-1/system/controlDict').read_text(encoding='utf8')
    assert len(re.findall(r'type\s+myFunctionObject\s*;',control))==1
    assert 'P0_UDF_OK_RERUN_CURRENT' in (work/'C-run/case-1/udf-solve.log').read_text(encoding='utf8')
    return {'handler':'udf.integrate','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_400_cell_icoFoam_final_time':.5,'actual_udf_marker_in_solver_log':True,'retry_original_files':True,
        'changed_lid_speeds':[1,.5],'velocity_max_abs_difference':difference,'rerun_current_initial_field':True,
        'single_existing_function_registration':True,'old_immutable_artifact_preserved':True}


def scenario(api,*,store=None,**_):
    built,library=build(api,store);checks=[built]
    (api.root/'udf-repeat-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    checks.append(integrate(api,store,library))
    report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':checks,
        'control_and_history_protocol_checks':control_checks(),'model_selects_operations':False}
    api.create_case('Independent UDF model readback')
    (api.root/'udf-repeat.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
