"""Real pvpython/pvbatch CFD postprocessing, OSMesa render and S3 publication; no LLM."""
import argparse,csv,io,json,os,secrets
from pathlib import Path
import httpx,numpy as np
from PIL import Image
from linux_flotherm_acceptance import Run as NativeRun,read,write,sha

EXAMPLES=Path(__file__).resolve().parent/'examples/paraview'
SCRIPT_PATH='D-post/script space & %/naca.py'
SCRIPT=(EXAMPLES/'naca_postprocess.py').read_text('utf8')
FAILURE="""import os
from pathlib import Path
from paraview.simple import Sphere
s=Sphere();s.UpdatePipeline();assert s.GetDataInformation().GetNumberOfPoints()>0
(Path(os.environ['VIBEFLOW_CASE_ROOT'])/'D-post/pressure.vtu').write_text('uncommitted failed attempt')
raise RuntimeError('Intentional failure after actual ParaView execution')
"""

def mesh(path,poly=False):
 from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader,vtkXMLPolyDataReader
 reader=(vtkXMLPolyDataReader if poly else vtkXMLUnstructuredGridReader)();reader.SetFileName(str(path));reader.Update();return reader.GetOutput()

class Run(NativeRun):
 label='paraview'
 def execute(self,backend,factor):
  self.put('Ref/render.json',json.dumps({'factor':factor}))
  headers={'Idempotency-Key':'pv-'+secrets.token_hex(8)};body={'script_path':SCRIPT_PATH,'backend':backend}
  jid=self.call('POST',self.prefix+'/paraview:run',json=body,headers=headers)['job_id'];job=self.settled(jid)
  children=job['attempt']['child_processes']['children'];assert len(children)==1 and next(iter(children.values()))['purpose']=='paraview_run'
  pub=job['attempt']['outputs']['publication'];assert pub['status']=='committed';base='.vibeflow/publications/'+pub['id']
  manifest=json.loads(self.remote(base+'/manifest.json'));write(self.root/f'publication-{jid}.json',manifest)
  for name,record in manifest['files'].items():
   data=self.remote(base+'/files/'+name);assert sha(data)==record['sha256'] and data==self.live(name)
  folder=self.root/f'outputs-{jid}';folder.mkdir()
  for name in ['pressure.vtu','sonic.vtp','sonic.csv','pressure.png','statistics.json']:(folder/name).write_bytes(self.live('D-post/'+name))
  from vtkmodules.util.numpy_support import vtk_to_numpy
  actual=mesh(folder/'pressure.vtu');source=mesh(EXAMPLES/'naca0012-su2.vtu');assert actual.GetNumberOfPoints()==5233 and actual.GetNumberOfCells()==10216
  assert np.array_equal(vtk_to_numpy(actual.GetPoints().GetData()),vtk_to_numpy(source.GetPoints().GetData()))
  pressure=vtk_to_numpy(source.GetPointData().GetArray('Pressure')).astype(float);ratio=vtk_to_numpy(actual.GetPointData().GetArray('PressureRatio')).astype(float)
  assert np.all(np.isfinite(ratio)) and np.allclose(ratio,pressure*factor/101325,rtol=2e-7,atol=1e-8)
  contour=mesh(folder/'sonic.vtp',True);assert contour.GetNumberOfPoints()>0 and contour.GetNumberOfCells()>0
  mach=vtk_to_numpy(contour.GetPointData().GetArray('Mach'));assert np.allclose(mach,1,rtol=0,atol=1e-6)
  rows=list(csv.DictReader(io.StringIO((folder/'sonic.csv').read_text())));assert len(rows)==contour.GetNumberOfPoints()
  assert all(abs(float(row['Mach'])-1)<1e-6 for row in rows)
  with Image.open(folder/'pressure.png') as picture:
   assert picture.format=='PNG' and picture.size==(640,360);pixels=np.array(picture.convert('RGB'))
   colors=len(np.unique(pixels.reshape(-1,3),axis=0));assert colors>64 and float(pixels.std())>10
  statistics=read(folder/'statistics.json');assert statistics['version']=='(5, 11)' and statistics['factor']==factor and statistics['sonic_points']==len(rows)
  assert self.client.get(self.prefix+'/raw',params={'path':'D-post/remove-me.txt'}).status_code==404
  assert self.remote(job['attempt']['workspace']['inputs_path']+'/Ref/naca.vtu')==(EXAMPLES/'naca0012-su2.vtu').read_bytes()
  assert json.loads(self.remote(job['attempt']['workspace']['inputs_path']+'/Ref/render.json'))=={'factor':factor}
  assert self.call('POST',self.prefix+'/paraview:run',json=body,headers=headers)['job_id']==jid
  self.result.setdefault('numerics',[]).append({'job_id':jid,'backend':backend,'factor':factor,'pressure_ratio_max_absolute_error':float(np.max(np.abs(ratio-pressure*factor/101325))),
    'sonic_points':len(rows),'sonic_cells':contour.GetNumberOfCells(),'png_colors':colors,'statistics':statistics})
  self.save();return {'base':base,'flow':self.live('D-post/pressure.vtu'),'manifest':manifest}
 def run(self):
  try:
   with self.api:
    from app.services.process_identity_native import observe
    self.result['API_birth']=observe(self.api.proc.pid)
    with httpx.Client(base_url=self.api.base+'/api/v1',trust_env=False,timeout=120) as self.client:
     account={'email':'pv-'+secrets.token_hex(6)+'@example.com','password':secrets.token_hex(24)};write(self.root/'private/account.json',account)
     self.client.headers['Authorization']='Bearer '+self.call('POST','/auth/register',json={**account,'name':'Actual ParaView acceptance'})['token']
     self.workspace=self.call('GET','/workspaces')[0]['id']
     capabilities=self.call('GET','/runtime/capabilities');write(self.root/'capabilities.json',capabilities)
     for backend in ['pvpython','pvbatch']:
      selected=next(v for v in capabilities['environment_registry'] if v['tool']==backend);assert selected['available']
      self.case('Real ParaView '+backend)
      data=(EXAMPLES/'naca0012-su2.vtu').read_bytes();assert sha(data)==read(EXAMPLES/'naca0012-su2-origin.json')['sha256']
      uploaded=self.call('POST',self.prefix+'/upload',params={'path':'Ref/naca.vtu'},files={'file':('naca.vtu',data,'application/octet-stream')});assert uploaded['content_sha256']==sha(data)
      self.put(SCRIPT_PATH,SCRIPT);self.put('D-post/remove-me.txt','obsolete result')
      first=self.execute(backend,1);second=self.execute(backend,2)
      assert selected['value']==first['manifest']['descriptor']['executable']
      assert first['flow']!=second['flow'] and self.remote(first['base']+'/files/D-post/pressure.vtu')==first['flow']
      assert 'D-post/remove-me.txt' in first['manifest']['descriptor']['deleted_files']
      self.put(SCRIPT_PATH,FAILURE);jid=self.call('POST',self.prefix+'/paraview:run',json={'script_path':SCRIPT_PATH,'backend':backend})['job_id'];job=self.settled(jid,'failed')
      assert len(job['attempt']['child_processes']['children'])==1 and job['attempt']['outputs'].get('publication',{}).get('status')!='committed'
      assert self.live('D-post/pressure.vtu')==second['flow'] and self.remote(first['base']+'/files/D-post/pressure.vtu')==first['flow']
      self.result['checks'].append(backend+'_real_CFD_read_calculate_contour_render_two_settings_frozen_inputs_delete_history_idempotency_failure_retention');self.save()
     requests=self.db('SELECT id,phase,children_json FROM native_requests',());write(self.root/'native-requests.json',requests)
     for row in requests:
      assert row['phase']=='succeeded'
      for child in json.loads(row['children_json']).get('children',{}).values():assert child['stop']['verified'] and child['stop']['scope_empty']
     self.result['passed']=True
  except BaseException as error:self.result['failure']={'type':type(error).__name__,'message':str(error)[:3000]};raise
  finally:
   self.result['API_exited']=self.api.proc is None or self.api.proc.poll() is not None
   if 'API_birth' in self.result:
    from app.services.process_identity_native import probe
    self.result['API_exit']=probe(self.result['API_birth'])
   self.save()

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('review',type=Path);p.add_argument('--windows',action='store_true');p.add_argument('--s3-endpoint',required=True);args=p.parse_args()
 review=read(args.review);assert review['classification']=='no_llm'
 for name,digest in review['sources'].items():
  assert name.startswith('platform/server/')
  source=Path(__file__).resolve().parents[2]/name if os.name=='nt' else Path(__file__).resolve().parent/name.removeprefix('platform/server/')
  assert sha(source.read_bytes())==digest,name
 assert args.windows==(os.name=='nt');Run(args.windows,args.s3_endpoint).run()
