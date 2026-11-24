"""Actual OpenFOAM decomposition, parallel solve and reconstruction on repeat."""
import asyncio,hashlib,json,time
from pathlib import Path

from cae_fixtures import cavity_2d
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_engineering_repeat_acceptance import case_root,retry_job,rerun_job,frozen_retry
from live_llm_udf_repeat_acceptance import seed_solve,vectors

RUN='C-run/case-1'
FIXTURE=r'''
from pathlib import Path
from app import storage
from app.db import SessionLocal
from app.models import Case
from app.services import job_execution as mpi_repeat_execution
mpi_repeat_root=Path(__file__).parent
mpi_repeat_release=mpi_repeat_execution.release_process
async def mpi_receipt_collision(owner,process):
 mode_path=mpi_repeat_root/'mpi-repeat-fault'
 if mode_path.exists() and mode_path.read_text(encoding='utf8')=='receipt':
  async with SessionLocal() as db:
   job,attempt=await mpi_repeat_execution._owned(db,owner);document=json.loads(attempt.input_manifest_json)
   if document.get('handler',{}).get('key')=='mpi.openfoam':
    case=await db.get(Case,job.case_id);root=storage._fs_path(case.s3_prefix,document['workspace']['run_path']).resolve()
    identifier=job.id;attempt_id=attempt.id
   else:root=None
  if root is not None:
   target=(root/'.vibeflow/mpi/receipt.json').resolve();assert target.is_relative_to(root)
   target.parent.mkdir(parents=True,exist_ok=True)
   with target.open('xb') as stream:stream.write(b'ACTUAL_PRIVATE_MPI_RECEIPT_COLLISION')
   (mpi_repeat_root/('actual-mpi-collision-'+str(identifier)+'.json')).write_text(json.dumps(
    {'job_id':identifier,'attempt_id':attempt_id,'actual_file_created':True}),encoding='utf8')
 return await mpi_repeat_release(owner,process)
mpi_repeat_execution.release_process=mpi_receipt_collision
import time
from app.services import attempt_workspace as mpi_workspace,job_repeat as mpi_repeat,step_handlers as mpi_handlers
from app.services import case_read_view as mpi_views,attempt_objects as mpi_objects
from app.services.mpi_handler import MpiHandler
def mpi_stage(label,started,error):
 with (mpi_repeat_root/'mpi-repeat-stages.jsonl').open('a',encoding='utf8') as stream:
  stream.write(json.dumps({'stage':label,'seconds':round(time.monotonic()-started,4),'error':error})+'\n')
def mpi_measure_async(target,name):
 original=getattr(target,name);label=target.__name__+'.'+name
 async def observed(*args,**kwargs):
  started=time.monotonic();error=None
  try:return await original(*args,**kwargs)
  except BaseException as failure:error=type(failure).__name__;raise
  finally:mpi_stage(label,started,error)
 setattr(target,name,observed)
def mpi_measure_sync(target,name):
 original=getattr(target,name);label=target.__name__+'.'+name
 def observed(*args,**kwargs):
  started=time.monotonic();error=None
  try:return original(*args,**kwargs)
  except BaseException as failure:error=type(failure).__name__;raise
  finally:mpi_stage(label,started,error)
 setattr(target,name,observed)
for target,name in [(MpiHandler,'prepare'),(mpi_workspace,'seal'),(mpi_repeat,'perform'),
 (mpi_repeat_execution,'submit'),(mpi_handlers,'bind')]:mpi_measure_async(target,name)
for target,name in [(mpi_views,'inspect'),(mpi_objects,'selection'),(mpi_objects,'ensure_inputs')]:mpi_measure_sync(target,name)
for name in ('materialize','prepare'):mpi_measure_sync(mpi_objects.Snapshot,name)
'''


def native(work,count):
    value=json.loads((work/'.vibeflow/mpi/native.json').read_text(encoding='utf8'))
    assert value['uid']!=0 and value['subdomains']==count
    assert set(value['processors'])=={'processor'+str(i) for i in range(count)}
    assert set(value['steps'])=={'decompose','solve','reconstruct'}
    assert all(s['returncode']==0 for s in value['steps'].values())
    assert value['steps']['solve']['argv'][value['steps']['solve']['argv'].index('-np')+1]==str(count)
    stage=json.loads((work/'.vibeflow/mpi/stage.json').read_text(encoding='utf8'))
    assert stage['removed']
    proof=json.loads((work/RUN/'mpi-evidence.json').read_text(encoding='utf8'))
    assert proof['summary']['finished_cleanly'] and proof['summary']['last_time']==.5
    return value


def scenario(api,*,store=None,**_):
    original_open=api.opener.open
    def observed_request(request,*args,**kwargs):
        started=time.monotonic();error=None
        try:return original_open(request,*args,**kwargs)
        except BaseException as failure:error=type(failure).__name__;raise
        finally:
            with (api.root/'mpi-repeat-http.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'method':request.get_method(),'path':request.selector,
                    'seconds':round(time.monotonic()-started,4),'error':error,'timeout_seconds':kwargs.get('timeout')})+'\n')
    api.opener.open=observed_request
    api.create_case('Actual MPI original retry and current rerun')
    serial=seed_solve(api);root=case_root(api,serial,store)
    reference=vectors(root/serial['attempt']['workspace']['run_path']/RUN/'0.5/U')
    mode=api.root/'mpi-repeat-fault';mode.write_text('receipt',encoding='utf8')
    first=write(api,'POST',api.prefix+'/mpi:run',{'run_dir':RUN,'solver':'icoFoam','subdomains':2})['job_id']
    failed=api.wait_job(first,expected='failed',timeout=360)
    witness=json.loads((api.root/('actual-mpi-collision-'+str(first)+'.json')).read_text(encoding='utf8'))
    assert witness['actual_file_created'] and witness['attempt_id']==failed['attempt']['id']
    assert failed['attempt']['process_stop']['verified'] and 'FileExistsError' in failed['log_backlog']
    native(root/failed['attempt']['workspace']['run_path'],2)
    assert failed['attempt']['outputs'].get('publication',{}).get('status')!='committed'
    mode.write_text('none',encoding='utf8')
    done,body,key=retry_job(api,first);frozen_retry(failed,done,'mpi.openfoam')
    work=root/done['attempt']['workspace']['run_path'];native(work,2);old=vectors(work/RUN/'0.5/U')
    error=max(abs(a-b) for va,vb in zip(reference,old) for a,b in zip(va,vb))/max(1,max(abs(v) for row in reference for v in row))
    # Keep the existing independently established serial/MPI acceptance bound.
    assert error<=1e-5,error
    publication=done['attempt']['outputs']['publication']['id'];output=RUN+'/0.5/U'
    bundle=root/'.vibeflow/publications'/publication/'files'/output;historical=bundle.read_bytes()
    assert historical==(work/output).read_bytes()
    current=cavity_2d()[RUN+'/0/U'].replace('uniform (1 0 0)','uniform (0.5 0 0)')
    assert current!=cavity_2d()[RUN+'/0/U'];put(api,RUN+'/0/U',current)
    dictionary='FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }\nnumberOfSubdomains 4;\nmethod simple;\ncoeffs { n (4 1 1); }\n'
    put(api,RUN+'/system/decomposeParDict',dictionary)
    (api.root/'mpi-repeat-progress.json').write_text(json.dumps({'retry_published':True,'source_job':first,
        'retry_job':done['id'],'serial_two_rank_velocity_relative_error':error}),encoding='utf8')
    fresh=rerun_job(api,first,{'run_dir':RUN,'solver':'icoFoam','subdomains':4})
    work=root/fresh['attempt']['workspace']['run_path'];native(work,4);changed=vectors(work/output)
    difference=max(abs(a-b) for va,vb in zip(old,changed) for a,b in zip(va,vb))
    assert difference>1e-4,difference
    assert bundle.read_bytes()==historical
    assert fresh['attempt']['workspace']['files'][RUN+'/0/U']['sha256']==hashlib.sha256(current.encode()).hexdigest()
    assert fresh['attempt']['workspace']['files'][RUN+'/system/decomposeParDict']['sha256']==hashlib.sha256(dictionary.encode()).hexdigest()
    report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','handler':'mpi.openfoam',
        'source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],'actual_400_cell_final_time':.5,
        'actual_decompose_parallel_solve_reconstruct':True,'actual_ranks':[2,4],'native_nonroot_and_stages_removed':True,
        'retry_frozen_inputs':True,'serial_two_rank_velocity_relative_error':error,'changed_lid_speeds':[1,.5],
        'velocity_max_abs_difference':difference,'current_decomposition_and_initial_field':True,
        'old_immutable_artifact_preserved':True,'model_selects_operations':False}
    (api.root/'mpi-repeat.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    api.create_case('Independent MPI model readback')
    return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
