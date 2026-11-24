"""Real registered Gmsh/blockMesh thermal workflows and published mesh previews, no LLM."""
import argparse,importlib.util,json,secrets,sys
from pathlib import Path
import httpx,meshio
from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader
R=Path(__file__).resolve().parents[2];sys.path.insert(0,str(R/'platform/server'))
from app.services.casedict import set_block
from cae_benchmarks import thermal_files
from registered_cae_benchmarks import thermal_errors
from testsupport import IsolatedServer
spec=importlib.util.spec_from_file_location('native_cae',Path(__file__).with_name('linux-cae-acceptance.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)

class Run(base.Run):
 def __init__(self,review,url,windows,kind):
  self.root=R/'tmp/linux-gmsh-20261118'/((('windows-' if windows else 'linux-')+kind+'-'+secrets.token_hex(5)))
  (self.root/'private').mkdir(parents=True);self.kind=kind;self.windows=windows
  self.result={'passed':False,'checks':[],'jobs':[],'flows':[],'review':review,'real_model_calls':0,'mock_llm_calls':0,'windows':windows,'mesh_source':kind}
  self.client=httpx.Client(base_url=url+'/api/v1',trust_env=False,timeout=120);self.save();print('GMSH_EVIDENCE '+str(self.root),flush=True)

 def setup(self):
  case=self.api('POST',f'/workspaces/{self.workspace}/cases',json={'name':self.kind+' actual conduction'})
  self.case_id=case['id'];self.prefix='/cases/'+str(self.case_id);self.run_dir='C-run/conduction';self.result['cases']=[self.case_id];self.save()
  current=self.api('GET',self.prefix+'/caseDict');content=current['content']
  for key,value in {
   'caseMeta':{'discipline':'thermal','analysisType':'steady','dimensions':'3D','units':{'length':'m','temperature':'K'}},
   'geometry':{'generator':'cadquery','parameters':{'lx':1,'ly':1,'lz':1}},
   'mesh':{'source':self.kind,'qualityTargets':{'policy':'fail','maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100}},
   'numericalMethod':{'solver':'laplacianFoam'},
   'postProcessing':{'runDir':self.run_dir,'fields':{'T':{'unit':'K'}},'dataExtraction':{'volumeAverages':{'mean':{'field':'T','time':1}}},
    'derivedQuantities':{'referenceValues':{'linear':{'metric':'"mean.T@1"','value':350,'unit':'K','kind':'verification','source':'"analytic T(x)=300+100*x"','l2_max':1e-6}}}},
  }.items():content=set_block(content,key,value)
  self.api('PUT',self.prefix+'/caseDict',json={'content':content,'expected_revision':current['dict_revision']})
  self.inputs=thermal_files()
  if self.kind=='gmsh':
   del self.inputs[self.run_dir+'/system/blockMeshDict']
   self.inputs[self.run_dir+'/system/mesh.geo']=(R/'platform/server/examples/gmsh-conduction/mesh.geo').read_text('utf8')
  else:
   # Match the previously accepted Linux 80-cell conduction mesh; the older
   # 20x2x2 fixture fails checkMesh's cell-determinant criterion on this release.
   path=self.run_dir+'/system/blockMeshDict';self.inputs[path]=self.inputs[path].replace('(20 2 2)','(10 4 2)')
  for path,text in self.inputs.items():self.put(path,text)
  value=self.put('A-geo/geometry.py',"import cadquery as cq\ndef build_geometry(ctx):\n return cq.Workplane('XY').box(1,1,1,centered=(False,False,False))\n")
  if value.get('auto_preview_job'):self.job(value['auto_preview_job'])

 def workflow(self):
  self.setup();fid=self.start('report');flow,_=self.advance(fid)
  jobs={s['key']:self.job(s['attempts'][-1]['job_id']) for s in flow['steps'] if s['attempts']}
  assert set(jobs)=={'geometry','mesh','solve','postprocess','report'}
  mesh=self.api('GET',self.prefix+'/mesh/results');assert not mesh['publication']['stale']
  assert mesh['quality']['mesh_ok'] and mesh['quality']['status']=='pass'
  assert mesh['toolchain']['mesh_source']==self.kind
  assert mesh['toolchain']['distro']==('vibeflow-linux' if self.windows else 'native-linux')
  assert set(mesh['toolchain']['executables'])==({'gmsh','gmshToFoam','checkMesh'} if self.kind=='gmsh' else {'blockMesh','checkMesh'})
  patches={p['name'] for p in mesh['boundaries']['patches'] if p['nFaces']};assert patches=={'xMin','xMax','insulated'},patches
  base.write(self.root/'mesh-results.json',mesh)
  cells=64 if self.kind=='gmsh' else 80
  if self.kind=='gmsh':
   data=self.raw(self.run_dir+'/gmsh.msh');path=self.root/'actual.msh';path.write_bytes(data);actual=meshio.read(path)
   assert sum(len(b.data) for b in actual.cells if b.type=='hexahedron')==cells
   assert set(actual.field_data)=={'xMin','xMax','insulated','solid'}
   preview=self.api('POST',self.prefix+'/geometry/msh-preview',json={'path':self.run_dir+'/gmsh.msh'})
   assert preview['points']==len(actual.points) and preview['cells']==sum(len(b.data) for b in actual.cells)
   data=self.raw(preview['path']);vtu=self.root/'actual-preview.vtu';vtu.write_bytes(data)
   reader=vtkXMLUnstructuredGridReader();reader.SetFileName(str(vtu));reader.Update()
   assert reader.GetOutput().GetNumberOfPoints()==preview['points'] and reader.GetOutput().GetNumberOfCells()==preview['cells']
   self.result['preview']={**preview,'vtu_sha256':base.sha(data)};self.save()
  fields=self.root/'actual-fields'
  for name in ['constant/polyMesh/'+n for n in ['points','faces','owner','neighbour']]+['1/T']:
   path=fields/self.run_dir/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(self.raw(self.run_dir+'/'+name))
  l2,maximum=thermal_errors(fields,self.run_dir,cells);assert l2<1e-6 and maximum<1e-6,(l2,maximum)
  solve=self.api('GET',self.prefix+'/solve/results');assert not solve['publication']['stale'] and solve['evidence']['parsed_log']['summary']['finished_cleanly']
  post=self.api('GET',self.prefix+'/postprocess/results');assert post['metrics']['readiness']['report_ready']
  report=self.api('GET',self.prefix+'/report/results');assert not report['publication']['stale']
  for path,item in report['manifest']['artifacts'].items():
   response=self.client.get(self.prefix+'/report/artifact',params={'publication_id':report['publication']['id'],'path':path});response.raise_for_status()
   assert base.sha(response.content)==item['sha256'];(self.root/Path(path).name).write_bytes(response.content)
  assert all(self.raw(p)==t.encode() for p,t in self.inputs.items())
  for name,value in [('workflow',flow),('solve',solve),('postprocess',post),('report',report)]:base.write(self.root/(name+'.json'),value)
  self.check('actual_'+self.kind+'_registered_mesh_to_report',cells=cells,temperature_l2_K=l2,temperature_max_error_K=maximum,case_id=self.case_id)

def main():
 p=argparse.ArgumentParser();p.add_argument('review');p.add_argument('--windows',action='store_true');p.add_argument('--source',choices=['gmsh','blockMesh'],default='gmsh');args=p.parse_args()
 review=json.loads((R/args.review).read_text('utf8'));assert review['classification']=='no_llm'
 for name,digest in review['sources'].items():assert base.sha((R/name).read_bytes())==digest,name
 server=IsolatedServer('native-gmsh-windows') if args.windows else None;run=None
 try:
  if server:server.__enter__()
  run=Run(args.review,server.base if server else 'http://127.0.0.1:57700',args.windows,args.source)
  if server:run.result['isolated_API']=str(server.root);run.save()
  run.register();run.workflow();run.result['functional_passed']=True
 except BaseException as error:
  if run:run.result['failure']={'type':type(error).__name__,'message':str(error)[:2500]}
  raise
 finally:
  try:
   if run:run.close();run.result['all_native_stops_verified']=True
  finally:
   if server and server.proc is not None:
    server.__exit__(*sys.exc_info())
    if run:run.result['isolated_API_exited']=server.proc.poll() is not None
   if run:run.result['passed']=bool(run.result.get('functional_passed') and run.result.get('all_native_stops_verified'));run.save()

if __name__=='__main__':main()
