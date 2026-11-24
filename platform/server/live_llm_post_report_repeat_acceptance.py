"""Real thermal extraction and report failure/retry/current-rerun acceptance."""
import asyncio,csv,hashlib,io,json,math,os,re,sys,time,zipfile
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

from PIL import Image,ImageStat
from pypdf import PdfReader
from cae_benchmarks import thermal_files
from app.services.case_evidence import run_inventory
from app.services.casedict import set_block
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_document_repeat_acceptance import upload
from live_llm_engineering_repeat_acceptance import await_put,case_root,retry_job,rerun_job,frozen_retry

RUN='C-run/conduction'
GRID=(10,4,2)
FIXTURE=r'''
from pathlib import Path
from app import storage
from app.db import SessionLocal
from app.models import Case
from app.services import job_execution as thermal_execution
thermal_root=Path(__file__).parent
thermal_release=thermal_execution.release_process
async def thermal_collision(owner,process):
 mode_path=thermal_root/'post-report-fault'
 mode=mode_path.read_text(encoding='utf8') if mode_path.exists() else 'none'
 if mode in {'postprocess.case','postprocess.run','report.compose','report.compose-generated','report.build'}:
  async with SessionLocal() as db:
   job,attempt=await thermal_execution._owned(db,owner);document=json.loads(attempt.input_manifest_json)
   if document.get('handler',{}).get('key')==mode:
    case=await db.get(Case,job.case_id);root=storage._fs_path(case.s3_prefix,document['workspace']['run_path']).resolve()
    descriptor=json.loads(attempt.publication_json)['descriptor'];identifier=job.id;attempt_id=attempt.id
   else:root=None
  if root is not None:
   if mode.startswith('postprocess.'):relative='D-post/data/postprocess-toolchain.json'
   elif mode=='report.build':relative='E-report/build/report-toolchain.json'
   else:relative='E-report/compositions/'+descriptor['execution_id']+'/toolchain.json'
   target=(root/relative).resolve();assert target.is_relative_to(root) and target!=root
   target.parent.mkdir(parents=True,exist_ok=True)
   with target.open('xb') as stream:stream.write(b'ACTUAL_PRIVATE_POST_REPORT_TOOLCHAIN_COLLISION')
   (thermal_root/('actual-post-report-collision-'+str(identifier)+'.json')).write_text(json.dumps(
    {'job_id':identifier,'attempt_id':attempt_id,'handler':mode,'path':relative,
     'execution_id':descriptor['execution_id'],'actual_file_created':True}),encoding='utf8')
 return await thermal_release(owner,process)
thermal_execution.release_process=thermal_collision
import cProfile,os,time,uuid
from app.services import job_repeat as thermal_repeat,case_read_view as thermal_views,attempt_objects as thermal_objects
from app.services import publication_inputs as thermal_inputs
from app.services import document_admission as thermal_admission,postprocess_publication as thermal_post_publication
from app.services import report_build as thermal_report_build
from app.services.compose_handler import ComposeHandler
from app.services.report_handler import ReportHandler
def thermal_stage(label,started,error):
 with (thermal_root/'post-report-stages.jsonl').open('a',encoding='utf8') as stream:
  stream.write(json.dumps({'stage':label,'seconds':round(time.monotonic()-started,4),'error':error})+'\n')
def thermal_measure_async(target,name):
 original=getattr(target,name);label=target.__name__+'.'+name
 async def observed(*args,**kwargs):
  started=time.monotonic();error=None
  try:return await original(*args,**kwargs)
  except BaseException as failure:error=type(failure).__name__;raise
  finally:thermal_stage(label,started,error)
 setattr(target,name,observed)
def thermal_measure_sync(target,name):
 original=getattr(target,name);label=target.__name__+'.'+name
 def observed(*args,**kwargs):
  started=time.monotonic();error=None;profiler=None
  if os.environ.get('VIBEFLOW_POST_REPORT_DIAG')=='1' and target in (thermal_views,thermal_inputs):
   profiler=cProfile.Profile()
  try:return profiler.runcall(original,*args,**kwargs) if profiler else original(*args,**kwargs)
  except BaseException as failure:error=type(failure).__name__;raise
  finally:
   if profiler:profiler.dump_stats(str(thermal_root/('post-report-profile-'+uuid.uuid4().hex+'.pstats')))
   thermal_stage(label,started,error)
 setattr(target,name,observed)
thermal_measure_async(thermal_repeat,'perform')
thermal_measure_async(thermal_inputs,'resolve')
thermal_measure_async(ComposeHandler,'prepare')
thermal_measure_async(ReportHandler,'prepare')
thermal_measure_async(ReportHandler,'complete_preparation')
thermal_measure_async(thermal_admission,'validate')
thermal_measure_async(thermal_execution,'submit')
thermal_measure_sync(thermal_post_publication,'_gate_files')
thermal_measure_sync(thermal_report_build,'report_gate_reason')
thermal_measure_sync(thermal_views,'inspect')
thermal_measure_sync(thermal_inputs,'inspect_files')
thermal_measure_sync(thermal_objects.Snapshot,'materialize')
from fastapi import Depends,HTTPException
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.case_write_capacity import committing
from app.services import step_handlers as thermal_handlers,artifact_publication as thermal_publications
@app.post('/api/v1/cases/{case_id}/testing/post-report-resolve')
async def observed_graph(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True);counts={}
 original=thermal_handlers.publication_handler
 class ObservedHandler:
  def __init__(self,target):self.target=target
  def __getattr__(self,name):return getattr(self.target,name)
  def source(self,*args,**kwargs):
   counts[self.target.key]=counts.get(self.target.key,0)+1
   return self.target.source(*args,**kwargs)
 def observed(descriptor):return ObservedHandler(original(descriptor))
 try:
  async with committing(db,case,content_write=False):
   _,post=await thermal_publications.current(db,case.id,'postprocess')
   _,solve=await thermal_publications.current(db,case.id,'solve')
   _,mesh=await thermal_publications.current(db,case.id,'mesh');root=post or solve
   if root is None or mesh is None:raise ValueError('Actual registered graph unavailable')
   thermal_handlers.publication_handler=observed
   await thermal_inputs.resolve(db,case,[{'publication_id':root.id,'paths':None,'require_current':True},
    {'publication_id':mesh.id,'paths':['B-mesh/quality.json'],'require_current':True}])
  return {'counts':counts,'actual_handlers_delegated':True}
 except (ValueError,OSError,KeyError) as error:
  raise HTTPException(409,{'code':'input_stale','error_type':type(error).__name__}) from error
 finally:thermal_handlers.publication_handler=original
'''


def observe_http(api):
    original=api.opener.open
    def observed(request,*args,**kwargs):
        started=time.monotonic();error=None
        if os.environ.get('VIBEFLOW_POST_REPORT_DIAG')=='1':kwargs['timeout']=120
        try:return original(request,*args,**kwargs)
        except BaseException as failure:error=type(failure).__name__;raise
        finally:
            with (api.root/'post-report-http.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'method':request.get_method(),'path':request.selector,
                    'seconds':round(time.monotonic()-started,4),'error':error,'timeout_seconds':kwargs.get('timeout')})+'\n')
    api.opener.open=observed


def post_config(samples):
    return {'runDir':RUN,'fields':{'T':{'unit':'K'}},'dataExtraction':{
        'volumeAverages':{'mean':{'field':'T','time':1}},
        'pointProbes':{'center':{'fields':['T'],'points':[.5,.5,.5],'times':[1]}},
        'lineSamples':{'profile':{'fields':['T'],'start':[.05,.5,.5],'end':[.95,.5,.5],'samples':samples,'times':[1]}}},
        'derivedQuantities':{'referenceValues':{'analytic_mean':{'metric':'"mean.T@1"','value':350,
            'unit':'K','kind':'verification','source':'"analytic T(x)=300+100*x"','l2_max':1e-6}}},
        'artifacts':{'plots':{'temperature':{'source':'profile','field':'T','time':1,
            'formats':['pdf','svg','png'],'title':'"Actual temperature profile"'}}}}


def configure(api,samples,*,engineering=False):
    saved=api.call('GET',api.prefix+'/caseDict');content=saved['content']
    if engineering:
        content=set_block(content,'caseMeta',{'discipline':'thermal','analysisType':'steady','dimensions':'3D',
            'units':{'length':'m','temperature':'K'}})
        content=set_block(content,'mesh',{'source':'blockMesh','qualityTargets':
            {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
        content=set_block(content,'numericalMethod',{'solver':'laplacianFoam'})
    content=set_block(content,'postProcessing',post_config(samples))
    return write(api,'PUT',api.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})


def seed_thermal(api,confirm_observer=None):
    configure(api,19,engineering=True)
    for path,text in thermal_files().items():
        # Retain the 80-cell unit cube and analytic thermal problem. The old
        # 20x2x2 mesh fails native checkMesh's determinant check; reduce its
        # aspect ratio without changing the quality gate or the cell count.
        if path.endswith('/system/blockMeshDict'):text=text.replace('(20 2 2)','(10 4 2)')
        put(api,path,text)
    await_put(api,'A-geo/geometry.py',"import cadquery as cq\ndef build_geometry(ctx):return {'box':cq.Workplane('XY').box(1,1,1,centered=(False,False,False))}\n")
    revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
    flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'solve','run_dir':RUN,'expected_revision':revision,
        'budget':{'timeout_seconds':120,'max_log_bytes':16777216}})
    for key in ('confirm_B','confirm_C','confirm_D'):
        deadline=time.monotonic()+180
        while True:
            value=api.call('GET','/workflow-runs/'+flow['id']);step=next(s for s in value['steps'] if s['key']==key)
            if step['status']=='awaiting_confirmation':break
            if value['status'] in {'failed','interrupted','cancelled'}:
                (api.root/'thermal-prerequisite-failure.json').write_text(json.dumps(value,indent=2),encoding='utf8')
                raise AssertionError('Actual thermal prerequisite failed: '+json.dumps(
                    [{'key':s['key'],'status':s['status'],'error_code':s['error_code'],'detail':s['detail']} for s in value['steps']])[:2000])
            if time.monotonic()>deadline:raise TimeoutError('Actual thermal workflow did not reach '+key)
            time.sleep(.1)
        for producer in value['steps']:
            if producer['status']=='succeeded' and producer['attempts']:api.wait_job(producer['attempts'][-1]['job_id'])
        if confirm_observer is not None:confirm_observer(api,value,step)
        api.call('POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
            'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
    solve=next(s for s in value['steps'] if s['key']=='solve')
    return api.wait_job(solve['attempts'][-1]['job_id'])


def raw(api,path):return api.call('GET',api.prefix+'/raw?path='+quote(path,safe=''),raw=True)


def fail(api,kind,endpoint,body):
    (api.root/'post-report-fault').write_text(kind,encoding='utf8')
    identifier=write(api,'POST',api.prefix+endpoint,body)['job_id']
    failed=api.wait_job(identifier,expected='failed',timeout=360)
    witness=json.loads((api.root/('actual-post-report-collision-'+str(identifier)+'.json')).read_text(encoding='utf8'))
    assert witness['actual_file_created'] and witness['attempt_id']==failed['attempt']['id']
    assert failed['attempt']['process_stop']['verified'] and 'FileExistsError' in failed['log_backlog']
    assert (failed['attempt'].get('outputs') or {}).get('publication',{}).get('status')!='committed'
    (api.root/'post-report-fault').write_text('none',encoding='utf8')
    return identifier,failed,witness


def extraction(work,job_id,samples):
    folder=work/('D-post/data/postprocess-'+str(job_id));metrics=json.loads((folder/'metrics.json').read_text(encoding='utf8'))
    mean=next(v['value'] for v in metrics['metrics'] if v['name']=='mean.T@1')
    assert math.isfinite(mean) and abs(mean-350)<1e-6
    assert metrics['readiness']['report_ready'] and metrics['readiness']['can_claim_verification']
    assert not metrics['readiness']['can_claim_validation'] and all(v['status']=='pass' for v in metrics['comparisons'])
    line=next(v for v in metrics['extractions']['lineSamples'] if v['id']=='profile');assert len(line['points'])==samples
    values=[v[0] for v in line['series']['T']['values']]
    assert all(math.isfinite(v) and 300<=v<=400 for v in values)
    assert values[0]<315 and values[-1]>385 and all(a<b for a,b in zip(values,values[1:]))
    rows=list(csv.DictReader(io.StringIO((folder/'profile.csv').read_text(encoding='utf8'))));assert len(rows)==samples
    assert max(abs(float(row['value'])-value) for row,value in zip(rows,values))<1e-9
    assert len(PdfReader(io.BytesIO((folder/'temperature.pdf').read_bytes())).pages)==1
    ET.fromstring((folder/'temperature.svg').read_bytes())
    with Image.open(folder/'temperature.png') as picture:
        assert picture.format=='PNG' and min(picture.size)>100 and max(ImageStat.Stat(picture.convert('RGB')).stddev)>5
    return metrics


def extraction_repeat(api,store,kind):
    first,failed,witness=fail(api,kind,'/postprocess:run',{'run_dir':RUN})
    root=case_root(api,failed,store);extraction(root/failed['attempt']['workspace']['run_path'],first,19)
    done,body,key=retry_job(api,first);frozen_retry(failed,done,kind)
    extraction(root/done['attempt']['workspace']['run_path'],done['id'],19)
    current=api.call('GET',api.prefix+'/postprocess/results');assert current['job_id']==done['id'] and not current['publication']['stale']
    old_path='D-post/data/postprocess-'+str(done['id'])+'/metrics.json'
    publication=done['attempt']['outputs']['publication']['id'];bundle=root/'.vibeflow/publications'/publication/'files'/old_path
    historical=bundle.read_bytes();configure(api,11)
    fresh=rerun_job(api,first,{'run_dir':RUN});metrics=extraction(root/fresh['attempt']['workspace']['run_path'],fresh['id'],11)
    assert bundle.read_bytes()==historical
    result=api.call('GET',api.prefix+'/postprocess/results');assert result['job_id']==fresh['id'] and not result['publication']['stale']
    frozen=fresh['attempt']['workspace'].get('publications',[])
    if kind=='postprocess.run':assert {'solve','mesh'}<={v['kind'] for v in frozen}
    else:assert frozen==[]
    return {'handler':kind,'source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_vtk_mean_temperature_k':next(v['value'] for v in metrics['metrics'] if v['name']=='mean.T@1'),
        'actual_line_sample_counts':[19,11],'actual_pdf_svg_png':True,'retry_original_files':True,
        'current_config_and_reference_readiness':True,'old_immutable_artifact_preserved':True,
        'registered_solver_lineage':kind=='postprocess.run'}


def compose_repeat(api,store,output,kind):
    first,failed,witness=fail(api,kind,'/report:compose?wait=false',{'output':output,'overwrite':True})
    root=case_root(api,failed,store);failed_draft=root/failed['attempt']['workspace']['run_path']/('E-report/compositions/'+witness['execution_id']+'/report.md')
    assert 'mean.T@1' in failed_draft.read_text(encoding='utf8')
    done,body,key=retry_job(api,first);frozen_retry(failed,done,kind)
    publication=done['attempt']['outputs']['publication']['id']
    original=api.call('GET',api.prefix+'/report/compose-result?publication_id='+publication)
    sealed=root/done['attempt']['workspace']['run_path']
    pointer=json.loads((sealed/'D-post/data/latest-postprocess.json').read_text(encoding='utf8'))
    metrics=json.loads((sealed/pointer['metrics_path']).read_text(encoding='utf8'))
    mean=next(v['value'] for v in metrics['metrics'] if v['name']=='mean.T@1')
    assert abs(mean-350)<1e-6 and original['publication']['applied']
    assert 'mean.T@1' in original['content'] and str(mean) in original['content']
    source=original['source_path'];bundle=root/'.vibeflow/publications'/publication/'files'/source;historical=bundle.read_bytes()
    marker='CURRENT_COMPOSE_TITLE' if kind=='report.compose' else 'CURRENT_GENERATED_TITLE'
    saved=api.call('GET',api.prefix+'/caseDict');content=set_block(saved['content'],'report',{'title':marker})
    write(api,'PUT',api.prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
    current_dictionary=raw(api,'0-caseDict/caseDict')
    fresh=rerun_job(api,first,{'run_dir':RUN,'overwrite':True})
    current=api.call('GET',api.prefix+'/report/compose-result?publication_id='+fresh['attempt']['outputs']['publication']['id'])
    assert current['publication']['applied'] and marker in current['content'] and marker not in original['content']
    assert raw(api,output).decode('utf8')==current['content'] and bundle.read_bytes()==historical
    assert current['backup'] and (root/fresh['attempt']['workspace']['run_path']/current['backup']).read_bytes()==historical
    assert fresh['attempt']['workspace']['files']['0-caseDict/caseDict']['sha256']==hashlib.sha256(current_dictionary).hexdigest()
    return {'handler':kind,'source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_evidence_composition':True,'retry_original_files':True,'current_report_title':marker,
        'current_target_backup_exact':True,'old_immutable_artifact_preserved':True,'output':output}


def compiled(work,marker,png):
    html=(work/'E-report/build/report.html').read_bytes();docx=(work/'E-report/build/report.docx').read_bytes()
    assert marker.encode() in html and b'<img' in html and (b'class="math' in html or b'<math' in html)
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        document=archive.read('word/document.xml');ET.fromstring(document)
        assert marker.encode() in document and b'oMath' in document
        assert any(archive.read(name)==png for name in archive.namelist() if name.startswith('word/media/'))
    return html,docx


def build_repeat(api,store,post):
    plot='D-post/data/postprocess-'+str(post['rerun_job'])+'/temperature.png';png=raw(api,plot)
    original=raw(api,'E-report/report.md').decode('utf8')+'\n\n![Actual thermal profile](../'+plot+')\n\n$T(x)=300+100x$\n\nORIGINAL_BUILD_INPUT\n'
    put(api,'E-report/report.md',original)
    first,failed,witness=fail(api,'report.build','/report:build',{})
    root=case_root(api,failed,store);compiled(root/failed['attempt']['workspace']['run_path'],'ORIGINAL_BUILD_INPUT',png)
    done,body,key=retry_job(api,first);frozen_retry(failed,done,'report.build')
    html,docx=compiled(root/done['attempt']['workspace']['run_path'],'ORIGINAL_BUILD_INPUT',png)
    publication=done['attempt']['outputs']['publication']['id'];bundle=root/'.vibeflow/publications'/publication/'files/E-report/build'
    assert (bundle/'report.html').read_bytes()==html and (bundle/'report.docx').read_bytes()==docx
    current=original.replace('ORIGINAL_BUILD_INPUT','CURRENT_BUILD_INPUT');put(api,'E-report/report.md',current)
    fresh=rerun_job(api,first,{'run_dir':RUN,'mode':'markdown','allow_standalone':True})
    new_html,new_docx=compiled(root/fresh['attempt']['workspace']['run_path'],'CURRENT_BUILD_INPUT',png)
    assert b'ORIGINAL_BUILD_INPUT' not in new_html and new_docx!=docx and (bundle/'report.html').read_bytes()==html and (bundle/'report.docx').read_bytes()==docx
    assert fresh['attempt']['workspace']['files']['E-report/report.md']['sha256']==hashlib.sha256(current.encode()).hexdigest()
    return {'handler':'report.build','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_pandoc_html_docx':True,'actual_plot_embedded_in_docx':True,'actual_formula_in_html_and_docx':True,
        'retry_original_files':True,'rerun_current_markdown':True,'old_immutable_artifact_preserved':True}


def thermal_seed_inputs(api,store):
    solved=seed_thermal(api);root=case_root(api,solved,store);work=root/solved['attempt']['workspace']['run_path']
    text=(work/RUN/'1/T').read_text(encoding='utf8')
    match=re.search(r'internalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;',text,re.S)
    assert match and int(match.group(1))==80
    values=[float(v) for v in match.group(2).split()];assert len(values)==80 and max(abs(v-(300+100*((i%GRID[0])+.5)/GRID[0])) for i,v in enumerate(values))<1e-6
    return {p.relative_to(work).as_posix():p.read_bytes() for p in run_inventory(work,RUN)}


def legacy_case(api,store,seed):
    api.create_case('Legacy extraction from actual thermal fields');configure(api,19,engineering=True)
    for path,data in seed.items():upload(api,path,data,'application/octet-stream')
    return extraction_repeat(api,store,'postprocess.case')


def graph_guard(api):
    observed=write(api,'POST',api.prefix+'/testing/post-report-resolve',{})
    assert observed['actual_handlers_delegated'] and len(observed['counts'])>=3
    assert all(n==1 for n in observed['counts'].values()),observed
    path=RUN+'/0/T';before=raw(api,path)
    put(api,path,before.decode('utf8')+'\n// Actual current input change after successful graph inspection\n')
    denied=api.call('POST',api.prefix+'/testing/post-report-resolve',{},expect=409)
    assert denied['detail']['code']=='input_stale'
    return {'actual_graph_source_calls':observed['counts'],'each_source_once_per_traversal':True,
        'later_actual_input_change_refused':True,'scope':'actual infrastructure and freshness protocol; no model output fabricated'}


def scenario(api,*,store=None,**_):
    observe_http(api);api.create_case('Actual thermal extraction and report repeat')
    seed=thermal_seed_inputs(api,store)
    checks=[]
    def record(row):
        checks.append(row);(api.root/'post-report-repeat-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    post=extraction_repeat(api,store,'postprocess.run');record(post)
    record(compose_repeat(api,store,'E-report/report.md','report.compose'))
    record(compose_repeat(api,store,'E-report/report.generated.md','report.compose-generated'))
    record(build_repeat(api,store,post))
    guard=graph_guard(api)
    record(legacy_case(api,store,seed))
    report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':checks,
        'actual_thermal_cells':80,'thermal_grid':list(GRID),'actual_thermal_final_time':1,'actual_repeated_handler_jobs':15,
        'actual_thermal_field_matches_linear_solution':True,'model_selects_operations':False,'publication_graph_guards':guard,
        'diagnostic_extended_http_wait':os.environ.get('VIBEFLOW_POST_REPORT_DIAG')=='1'}
    (api.root/'post-report-repeat.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    api.create_case('Independent postprocess and report model readback');return report


def legacy_scenario(api,*,store=None,**_):
    observe_http(api);api.create_case('Actual thermal donor for focused case extraction repeat')
    seed=thermal_seed_inputs(api,store);guard=graph_guard(api);row=legacy_case(api,store,seed)
    report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':[row],
        'actual_thermal_cells':80,'thermal_grid':list(GRID),'actual_thermal_final_time':1,'actual_repeated_handler_jobs':3,
        'actual_thermal_field_matches_linear_solution':True,'model_selects_operations':False,
        'scope':'postprocess.case_only','publication_graph_guards':guard,
        'diagnostic_extended_http_wait':os.environ.get('VIBEFLOW_POST_REPORT_DIAG')=='1'}
    (api.root/'post-report-repeat.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    api.create_case('Independent focused case extraction model readback');return report


if __name__=='__main__':
    diagnostic='--diagnose-report' in sys.argv
    if diagnostic:sys.argv.remove('--diagnose-report');os.environ['VIBEFLOW_POST_REPORT_DIAG']='1'
    focused='--legacy-only' in sys.argv
    if focused:sys.argv.remove('--legacy-only')
    asyncio.run(main(selected_override=legacy_scenario if focused else scenario,fixture_override=FIXTURE,store_volume_max=16))
