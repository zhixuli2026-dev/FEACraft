"""Actual CCX, SU2 and ParaView receipt failures, sealed retry and current rerun."""
import ast,asyncio,csv,hashlib,io,json,math,re,uuid
from pathlib import Path
from PIL import Image,ImageStat
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_engineering_repeat_acceptance import case_root,retry_job,rerun_job,frozen_retry
from testsupport import SERVER

FIXTURE=r'''
from pathlib import Path
from app import storage
from app.db import SessionLocal
from app.models import Case
from app.services import job_execution as solver_repeat_execution
solver_repeat_root=Path(__file__).parent
solver_repeat_release=solver_repeat_execution.release_process
async def actual_receipt_collision(owner,process):
 mode_path=solver_repeat_root/'solver-repeat-fault'
 mode=mode_path.read_text(encoding='utf8') if mode_path.exists() else 'none'
 paths={'calculix.solve':'.vibeflow/calculix/receipt.json','su2.solve':'.vibeflow/su2/receipt.json',
  'paraview.run':'.vibeflow/paraview/receipt.json'}
 if mode in paths:
  async with SessionLocal() as db:
   job,attempt=await solver_repeat_execution._owned(db,owner)
   document=json.loads(attempt.input_manifest_json)
   if document.get('handler',{}).get('key')==mode:
    case=await db.get(Case,job.case_id)
    root=storage._fs_path(case.s3_prefix,document['workspace']['run_path']).resolve()
    identifier=job.id;attempt_id=attempt.id
   else:root=None
  if root is not None:
   target=(root/paths[mode]).resolve();assert target.is_relative_to(root) and target!=root
   target.parent.mkdir(parents=True,exist_ok=True)
   with target.open('xb') as stream:stream.write(b'ACTUAL_PRIVATE_RECEIPT_COLLISION')
   (solver_repeat_root/('actual-solver-collision-'+str(identifier)+'.json')).write_text(json.dumps(
    {'job_id':identifier,'attempt_id':attempt_id,'handler':mode,'path':paths[mode],'actual_file_created':True}),encoding='utf8')
 return await solver_repeat_release(owner,process)
solver_repeat_execution.release_process=actual_receipt_collision
'''


def literal(filename,name):
    # Read only literal fixture data; never import or execute historical suites.
    for node in ast.parse((SERVER/filename).read_text(encoding='utf8')).body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets):
            value=ast.literal_eval(node.value);assert isinstance(value,str);return value
    raise AssertionError('Literal fixture not found: '+name)


def fail(api,kind,endpoint,parameters):
    mode=api.root/'solver-repeat-fault';mode.write_text(kind,encoding='utf8')
    first=write(api,'POST',api.prefix+endpoint,parameters)['job_id']
    failed=api.wait_job(first,expected='failed',timeout=360)
    witness=json.loads((api.root/('actual-solver-collision-'+str(first)+'.json')).read_text(encoding='utf8'))
    assert witness['actual_file_created'] and witness['attempt_id']==failed['attempt']['id']
    assert 'FileExistsError' in failed['log_backlog'] and failed['attempt']['process_stop']['verified']
    mode.write_text('none',encoding='utf8');return first,failed


def displacement(data):
    active=False;values={};rounding={}
    for line in data.decode('ascii').splitlines():
        if 'DISP' in line:active=True;continue
        if active and line.startswith(' -3'):break
        if active and line.startswith(' -1'):
            node=int(line[3:13]);tokens=re.findall(r'[+-]?\d+\.\d+E[+-]\d+',line[13:])
            values[node]=[float(v) for v in tokens]
            rounding[node]=[.5*10**(int(v.split('E')[1])-len(v.split('E')[0].split('.')[1])) for v in tokens]
    assert set(values)==set(range(1,9)) and all(len(v)==3 and all(math.isfinite(x) for x in v) for v in values.values())
    assert all(max(map(abs,values[n]))<1e-12 for n in range(1,5))
    return values,rounding


def calculix(api,store):
    api.create_case('Actual CCX repeat')
    deck=literal('calculix_smoke.py','DECK');path='C-run/calculix/beam.inp';resource='Ref/beam.inc'
    put(api,resource,deck);put(api,path,'*INCLUDE, INPUT=Ref/beam.inc\n')
    first,failed=fail(api,'calculix.solve','/calculix:solve',{'input_path':path})
    done,body,key=retry_job(api,first);frozen_retry(failed,done,'calculix.solve')
    root=case_root(api,done,store);output='C-run/calculix/beam.frd'
    old=api.call('GET',api.prefix+'/raw?path='+output,raw=True);original,old_rounding=displacement(old)
    assert all(-.02<original[n][2]<-.01 for n in range(5,9))
    publication=done['attempt']['outputs']['publication']['id'];bundle=root/'.vibeflow/publications'/publication/'files'/output
    historical=bundle.read_bytes();assert historical==old
    current=deck.replace(',3,-1000',',3,-2000');assert current!=deck;put(api,resource,current)
    fresh=rerun_job(api,first,{'input_path':path})
    changed,new_rounding=displacement(api.call('GET',api.prefix+'/raw?path='+output,raw=True))
    error=max(abs(changed[n][j]-2*original[n][j]) for n in original for j in range(3))
    # Compare the independently rounded FRD numbers at their written precision:
    # each value contributes half its last decimal unit, plus float arithmetic.
    assert all(abs(changed[n][j]-2*original[n][j])<=new_rounding[n][j]+2*old_rounding[n][j]
        +8*math.ulp(max(abs(changed[n][j]),2*abs(original[n][j]))) for n in original for j in range(3))
    assert bundle.read_bytes()==historical
    assert fresh['attempt']['workspace']['files'][resource]['sha256']==hashlib.sha256(current.encode()).hexdigest()
    return {'handler':'calculix.solve','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_ccx_static_displacement_verified':True,'doubled_load_linear_displacement_max_abs_error':error,
        'frd_written_rounding_bound_respected':True,
        'retry_original_files':True,'rerun_current_include':True,'public_parameters_without_executable':True,
        'old_immutable_artifact_preserved':True}


def su2_field(path):
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader
    reader=vtkXMLUnstructuredGridReader();reader.SetFileName(str(path));reader.Update();mesh=reader.GetOutput()
    assert mesh.GetNumberOfPoints()==5233 and mesh.GetNumberOfCells()==10216
    fields={}
    for name,low,high in [('Density',.1,5),('Pressure',10000,500000),('Temperature',100,1000),('Mach',0,3)]:
        array=mesh.GetPointData().GetArray(name);assert array is not None and array.GetNumberOfTuples()==5233
        values=[array.GetValue(i) for i in range(5233)]
        assert all(math.isfinite(v) and low<=v<=high for v in values) and max(values)-min(values)>1e-3
        fields[name]=values
    return fields


def su2(api,store):
    api.create_case('Actual SU2 repeat')
    path='C-run/su2/airfoil.cfg';examples=SERVER/'examples/su2'
    config=(examples/'inv_NACA0012.cfg').read_text().replace('ITER= 250','ITER= 3').replace(
        'MESH_FILENAME= mesh_NACA0012_inv.su2','MESH_FILENAME= Ref/naca0012.su2')
    assert 'ITER= 3' in config
    put(api,'Ref/naca0012.su2',(examples/'mesh_NACA0012_inv.su2').read_text());put(api,path,config)
    first,failed=fail(api,'su2.solve','/su2:solve',{'config_path':path})
    done,body,key=retry_job(api,first);frozen_retry(failed,done,'su2.solve')
    root=case_root(api,done,store);work=root/done['attempt']['workspace']['run_path'];original=su2_field(work/'flow.vtu')
    rows=list(csv.DictReader(io.StringIO((work/'history.csv').read_text()),skipinitialspace=True))
    assert rows and all(all(math.isfinite(float(v)) for v in row.values()) for row in rows)
    publication=done['attempt']['outputs']['publication']['id'];bundle=root/'.vibeflow/publications'/publication/'files/flow.vtu'
    historical=bundle.read_bytes();current=config.replace('AOA= 1.25','AOA= 5.0');assert current!=config
    put(api,path,current);fresh=rerun_job(api,first,{'config_path':path});work=root/fresh['attempt']['workspace']['run_path']
    fields=su2_field(work/'flow.vtu');difference=max(abs(a-b) for a,b in zip(original['Pressure'],fields['Pressure']))
    assert difference>1 and bundle.read_bytes()==historical
    assert fresh['attempt']['workspace']['files'][path]['sha256']==hashlib.sha256(current.encode()).hexdigest()
    return {'handler':'su2.solve','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_su2_points':5233,'actual_su2_cells':10216,'finite_physical_fields_and_history':True,
        'changed_angle_pressure_max_abs_difference':difference,'retry_original_files':True,'rerun_current_config':True,
        'public_config_path_without_executable':True,'old_immutable_artifact_preserved':True}


def sphere(path,radius):
    from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
    reader=vtkXMLPolyDataReader();reader.SetFileName(str(path));reader.Update();mesh=reader.GetOutput();bounds=mesh.GetBounds()
    assert mesh.GetNumberOfPoints()==226 and mesh.GetNumberOfCells()==448
    assert abs(bounds[4]+radius)<1e-6 and abs(bounds[5]-radius)<1e-6
    assert all(.98*radius<abs(v)<=1.01*radius for v in bounds)


def paraview(api,store):
    api.create_case('Actual ParaView repeat')
    path='D-post/render.py';put(api,path,literal('paraview_publication_smoke.py','SCRIPT'))
    put(api,'Ref/render.json','{"radius":0.5}')
    first,failed=fail(api,'paraview.run','/paraview:run',{'script_path':path})
    done,body,key=retry_job(api,first);frozen_retry(failed,done,'paraview.run')
    root=case_root(api,done,store);work=root/done['attempt']['workspace']['run_path'];sphere(work/'D-post/paraview-sphere.vtp',.5)
    image=api.call('GET',api.prefix+'/raw?path=D-post/paraview-sphere.png',raw=True)
    with Image.open(io.BytesIO(image)) as picture:
        assert picture.format=='PNG' and picture.size==(320,240) and max(ImageStat.Stat(picture.convert('RGB')).stddev)>5
    publication=done['attempt']['outputs']['publication']['id'];bundle=root/'.vibeflow/publications'/publication/'files/D-post/paraview-sphere.vtp'
    historical=bundle.read_bytes();put(api,'Ref/render.json','{"radius":1.0}')
    fresh=rerun_job(api,first,{'script_path':path});work=root/fresh['attempt']['workspace']['run_path']
    sphere(work/'D-post/paraview-sphere.vtp',1)
    assert bundle.read_bytes()==historical
    return {'handler':'paraview.run','source_job':first,'retry_job':done['id'],'rerun_job':fresh['id'],
        'actual_pvpython_sphere_radii':[.5,1],'actual_points':226,'actual_cells':448,'actual_png_size':[320,240],
        'retry_original_files':True,'rerun_current_resource':True,'public_parameters_without_executable_or_backend':True,
        'old_immutable_artifact_preserved':True}


def scenario(api,*,store=None,**_):
    checks=[]
    for run in (calculix,su2,paraview):
        checks.append(run(api,store))
        (api.root/'solver-repeat-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    report={'passed':True,'mock_llm_calls':0,'backend':'s3' if store else 'fs','checks':checks,
        'actual_configured_native_engines':True,'model_selects_native_operations':False}
    (api.root/'solver-repeat.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
