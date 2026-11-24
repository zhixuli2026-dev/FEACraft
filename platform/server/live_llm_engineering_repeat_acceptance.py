"""Actual CadQuery export, blockMesh/checkMesh and native LaTeX retry/rerun."""
import asyncio,hashlib,io,json,re,time,uuid
from pathlib import Path
from urllib.parse import quote
from pypdf import PdfReader
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main,put,repeat,write

FIXTURE=r'''
from pathlib import Path
from app import storage
from app.db import SessionLocal
from app.models import Case
from app.services import job_execution as engineering_execution
engineering_test_root=Path(__file__).parent
engineering_original_release=engineering_execution.release_process
async def engineering_collision_release(owner,process):
 mode_path=engineering_test_root/'engineering-fault-mode'
 mode=mode_path.read_text(encoding='utf8') if mode_path.exists() else 'none'
 paths={'mesh.generate':'B-mesh/quality.json','latex.build':'E-report/latex/build/main.pdf'}
 if mode in paths:
  async with SessionLocal() as db:
   job,attempt=await engineering_execution._owned(db,owner)
   manifest=json.loads(attempt.input_manifest_json)
   if manifest.get('handler',{}).get('key')==mode:
    case=await db.get(Case,job.case_id)
    root=storage._fs_path(case.s3_prefix,manifest['workspace']['run_path']).resolve()
    identifier=job.id;attempt_id=attempt.id
   else:root=None
  if root is not None:
   target=(root/paths[mode]).resolve();assert target.is_relative_to(root) and target!=root
   target.parent.mkdir(parents=True,exist_ok=True)
   with target.open('xb') as stream:stream.write(b'ACTUAL_PRIVATE_OUTPUT_COLLISION')
   (engineering_test_root/('actual-engineering-collision-'+str(identifier)+'.json')).write_text(json.dumps(
    {'job_id':identifier,'attempt_id':attempt_id,'handler':mode,'path':paths[mode],'actual_file_created':True}),encoding='utf8')
 return await engineering_original_release(owner,process)
engineering_execution.release_process=engineering_collision_release
'''


def case_root(api,job,store):
    if store:return api.root/'files'/job['attempt']['workspace']['storage']['prefix']
    return next((api.root/'files/workspaces').glob('*/cases/'+str(api.case_id)))


def await_put(api,path,content):
    result=put(api,path,content)
    if result.get('auto_preview_job'):api.wait_job(result['auto_preview_job'])
    return result


def retry_job(api,identifier):
    options=api.call('GET','/jobs/'+str(identifier)+'/operations')
    assert options['retry']['available'] and not options['resume']['available']
    body={'action':'retry','source_attempt_id':options['source_attempt_id'],'source_version':options['source_version']}
    key=uuid.uuid4().hex;created=repeat(api,identifier,body,key);job=api.wait_job(created['id'],timeout=360)
    assert job['attempt']['number']==2 and job['attempt']['root_job_id']==identifier
    assert job['attempt']['retry_of_attempt_id']==options['source_attempt_id']
    assert job['attempt']['outputs']['publication']['status']=='committed'
    replay=repeat(api,identifier,body,key);assert replay['id']==job['id'] and not replay['created']
    return job,body,key


def rerun_job(api,identifier,parameters):
    options=api.call('GET','/jobs/'+str(identifier)+'/operations')
    body={'action':'rerun','source_attempt_id':options['source_attempt_id'],'source_version':options['source_version'],
        'expected_revision':options['expected_revision'],'expected_event_sequence':options['expected_event_sequence'],'parameters':parameters}
    created=repeat(api,identifier,body,uuid.uuid4().hex);job=api.wait_job(created['id'],timeout=360)
    assert job['attempt']['number']==1 and job['attempt']['root_job_id']==job['id']
    assert job['attempt']['outputs']['publication']['status']=='committed'
    return job


def frozen_retry(failed,done,kind):
    assert failed['status']=='failed' and failed['attempt']['process_stop']['verified']
    assert failed['attempt']['handler']['key']==done['attempt']['handler']['key']==kind
    assert failed['attempt']['workspace']['files']==done['attempt']['workspace']['files']


def geometry(api,store):
    api.create_case('Actual export repeat')
    permit=api.root/'actual-cadquery-export-permitted'
    def source(size):
        return "import cadquery as cq\nfrom pathlib import Path\nif not Path("+repr(str(permit))+").exists():raise RuntimeError('Actual unavailable external condition')\ndef build_geometry(ctx):return {'box':cq.Workplane('XY').box("+','.join([str(size)]*3)+")}\n"
    original=source(1);saved=put(api,'A-geo/geometry.py',original)
    if saved.get('auto_preview_job'):api.wait_job(saved['auto_preview_job'],expected='failed')
    first=write(api,'POST',api.prefix+'/geometry:export',{})['job_id'];failed=api.wait_job(first,expected='failed')
    assert 'Actual unavailable external condition' in failed['log_backlog']
    permit.touch();done,body,key=retry_job(api,first);frozen_retry(failed,done,'geometry.export')
    root=case_root(api,done,store);publication=done['attempt']['outputs']['publication']['id']
    step='A-geo/exports/box.step';old=api.call('GET',api.prefix+'/raw?path='+step,raw=True)
    import cadquery as cq
    owned=api.root/'original-export.step';owned.write_bytes(old)
    assert abs(cq.importers.importStep(str(owned)).val().Volume()-1)<1e-9
    bundle=root/'.vibeflow/publications'/publication/'files'/step;historical=bundle.read_bytes();assert historical==old
    current=source(2);await_put(api,'A-geo/geometry.py',current)
    fresh=rerun_job(api,first,{})
    actual=api.call('GET',api.prefix+'/raw?path='+step,raw=True);owned=api.root/'current-export.step';owned.write_bytes(actual)
    assert abs(cq.importers.importStep(str(owned)).val().Volume()-8)<1e-9 and bundle.read_bytes()==historical
    assert fresh['attempt']['workspace']['files']['A-geo/geometry.py']['sha256']==hashlib.sha256(current.encode()).hexdigest()
    assert api.call('GET','/jobs/'+str(first))['status']=='failed'
    return {'handler':'geometry.export','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_cadquery_step_volumes':[1,8],'retry_original_files':True,'rerun_current_source':True,'old_immutable_artifact_preserved':True}


def mesh(api,store):
    api.create_case('Actual mesh repeat')
    saved=api.call('GET',api.prefix+'/caseDict')
    content=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
        {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
    write(api,'PUT',api.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
    files=cavity_2d()
    for path,content in files.items():put(api,path,content)
    await_put(api,'A-geo/geometry.py',"import cadquery as cq\ndef build_geometry(ctx):return {'box':cq.Workplane('XY').box(1,1,1)}\n")
    revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
    flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'mesh','run_dir':'C-run/case-1','expected_revision':revision,
        'budget':{'timeout_seconds':120,'max_log_bytes':16777216}})
    deadline=time.monotonic()+160
    while True:
        current=api.call('GET','/workflow-runs/'+flow['id']);confirm=next(s for s in current['steps'] if s['key']=='confirm_B')
        if confirm['status']=='awaiting_confirmation':break
        if current['status'] in {'failed','cancelled','interrupted'}:raise AssertionError('Geometry prerequisite failed')
        if time.monotonic()>deadline:raise TimeoutError('Mesh workflow did not reach explicit B confirmation')
        time.sleep(.1)
    for producer in current['steps']:
        if producer['status']=='succeeded' and producer['attempts']:api.wait_job(producer['attempts'][-1]['job_id'])
    mode=api.root/'engineering-fault-mode';mode.write_text('mesh.generate',encoding='utf8')
    api.call('POST',api.prefix+'/caseDict:advance',{'state':confirm['confirmation_state'],'confirmed':True,
        'expected_revision':confirm['confirmation_revision'],'workflow_step_id':confirm['id'],'expected_step_version':confirm['version']})
    deadline=time.monotonic()+30
    while True:
        current=api.call('GET','/workflow-runs/'+flow['id']);step=next(s for s in current['steps'] if s['key']=='mesh')
        if step['attempts']:break
        if time.monotonic()>deadline:raise TimeoutError('Confirmed mesh was not submitted')
        time.sleep(.1)
    first=step['attempts'][-1]['job_id'];failed=api.wait_job(first,expected='failed')
    assert 'FileExistsError' in failed['log_backlog']
    mode.write_text('none',encoding='utf8');done,body,key=retry_job(api,first);frozen_retry(failed,done,'mesh.generate')
    root=case_root(api,done,store);work=root/done['attempt']['workspace']['run_path']
    assert re.search(r'\bcells:\s+400\b',(work/'C-run/case-1/checkMesh.log').read_text())
    quality=api.call('GET',api.prefix+'/mesh/results');assert quality['quality']['mesh_ok'] and quality['quality']['status']=='pass'
    publication=done['attempt']['outputs']['publication']['id'];path='C-run/case-1/constant/polyMesh/points'
    bundle=root/'.vibeflow/publications'/publication/'files'/path;historical=bundle.read_bytes()
    changed=files['C-run/case-1/system/blockMeshDict'].replace('(20 20 1)','(10 10 1)')
    assert changed!=files['C-run/case-1/system/blockMeshDict'];put(api,'C-run/case-1/system/blockMeshDict',changed)
    fresh=rerun_job(api,first,{'run_dir':'C-run/case-1'});work=root/fresh['attempt']['workspace']['run_path']
    assert re.search(r'\bcells:\s+100\b',(work/'C-run/case-1/checkMesh.log').read_text())
    assert bundle.read_bytes()==historical and api.call('GET','/jobs/'+str(first))['status']=='failed'
    return {'handler':'mesh.generate','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_blockMesh_checkMesh_cells':[400,100],'explicit_B_confirmation':True,'retry_original_files':True,
        'rerun_current_block_mesh':True,'old_immutable_artifact_preserved':True}


def latex(api,store):
    api.create_case('Actual LaTeX repeat')
    main_path='E-report/latex/main.tex'
    original='\\documentclass{article}\n\\begin{document}\nOriginalNativeDocument\n\\end{document}\n'
    put(api,main_path,original)
    mode=api.root/'engineering-fault-mode';mode.write_text('latex.build',encoding='utf8')
    parameters={'main':main_path,'recipe':'pdflatex','resources':[]}
    first=write(api,'POST',api.prefix+'/latex:build',parameters)['job_id'];failed=api.wait_job(first,expected='failed',timeout=360)
    assert 'FileExistsError' in failed['log_backlog']
    mode.write_text('none',encoding='utf8');done,body,key=retry_job(api,first);frozen_retry(failed,done,'latex.build')
    publication=done['attempt']['outputs']['publication']['id'];path='E-report/latex/build/main.pdf'
    historical_url=api.prefix+'/latex/artifact?publication_id='+publication+'&path='+quote(path)
    old=api.call('GET',historical_url,raw=True)
    assert 'OriginalNativeDocument' in ''.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(old)).pages)
    current=original.replace('OriginalNativeDocument','CurrentNativeDocument');put(api,main_path,current)
    fresh=rerun_job(api,first,parameters);new_pub=fresh['attempt']['outputs']['publication']['id']
    latest=api.call('GET',api.prefix+'/latex/artifact?publication_id='+new_pub+'&path='+quote(path),raw=True)
    actual=''.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(latest)).pages)
    assert 'CurrentNativeDocument' in actual and 'OriginalNativeDocument' not in actual
    assert api.call('GET',historical_url,raw=True)==old
    assert api.call('GET',api.prefix+'/latex/source?publication_id='+publication+'&path='+quote(main_path),raw=True)==original.encode()
    assert fresh['attempt']['workspace']['files'][main_path]['sha256']==hashlib.sha256(current.encode()).hexdigest()
    return {'handler':'latex.build','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_native_pdflatex':True,'actual_old_and_current_pdf_text_verified':True,'retry_original_files':True,
        'rerun_current_source':True,'old_immutable_artifact_and_source_preserved':True}


def scenario(api,*,store=None,**_):
    checks=[]
    for run in (geometry,mesh,latex):
        checks.append(run(api,store))
        (api.root/'engineering-repeat-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':checks,
        'real_business_processes':True,'model_selects_engineering_operations':False}
    (api.root/'engineering-repeat.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
