"""Actual SU2 NACA0012 solve/restart and S3 publication on both hosts; no LLM."""
import argparse,csv,io,json,math,os,secrets
from pathlib import Path
import httpx
from linux_flotherm_acceptance import Run as NativeRun,read,write,sha

EXAMPLES=Path(__file__).resolve().parent/'examples/su2'
CONFIG_PATH='C-run/su2 space & case/naca.cfg'
MESH_PATH='C-run/su2/naca.su2'

def fields(path):
 from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader
 reader=vtkXMLUnstructuredGridReader();reader.SetFileName(str(path));reader.Update();mesh=reader.GetOutput()
 assert mesh.GetNumberOfPoints()==5233 and mesh.GetNumberOfCells()==10216
 result={}
 for name,low,high in [('Density',.1,5),('Pressure',10000,500000),('Temperature',100,1000),('Mach',0,3)]:
  values=mesh.GetPointData().GetArray(name);assert values is not None and values.GetNumberOfTuples()==5233,name
  numbers=[values.GetValue(i) for i in range(5233)]
  assert all(math.isfinite(v) and low<=v<=high for v in numbers),name
  assert max(numbers)-min(numbers)>1e-3,name
  result[name]={'min':min(numbers),'max':max(numbers),'mean':sum(numbers)/len(numbers)}
 return result

class Run(NativeRun):
 label='su2'
 def solve(self,restart=None):
  headers={'Idempotency-Key':'su2-'+secrets.token_hex(8)};body={'config_path':CONFIG_PATH}
  jid=self.call('POST',self.prefix+'/su2:solve',json=body,headers=headers)['job_id'];job=self.settled(jid)
  children=job['attempt']['child_processes']['children'];assert len(children)==1 and next(iter(children.values()))['purpose']=='su2_solve'
  pub=job['attempt']['outputs']['publication'];assert pub['status']=='committed';base='.vibeflow/publications/'+pub['id']
  manifest=json.loads(self.remote(base+'/manifest.json'));write(self.root/f'publication-{jid}.json',manifest)
  for name,record in manifest['files'].items():
   data=self.remote(base+'/files/'+name);assert sha(data)==record['sha256'] and data==self.live(name)
  flow=self.live('flow.vtu');local=self.root/f'flow-{jid}.vtu';local.write_bytes(flow)
  history=self.live('history.csv');(self.root/f'history-{jid}.csv').write_bytes(history)
  rows=[{k.strip():float(v) for k,v in row.items()} for row in csv.DictReader(io.StringIO(history.decode()),skipinitialspace=True)]
  assert rows and all(math.isfinite(v) for row in rows for v in row.values())
  residual=next(k for k in rows[0] if 'rms' in k.lower() and ('rho' in k.lower() or 'density' in k.lower()))
  if restart is None:assert rows[-1][residual]<rows[0][residual]-1,(rows[0],rows[-1])
  else:
   assert self.remote(job['attempt']['workspace']['inputs_path']+'/restart_flow.dat')==restart
   assert 'restart_flow.dat' in manifest['descriptor']['source']['files']
  self.result.setdefault('numerics',[]).append({'job_id':jid,'restart':restart is not None,'rows':len(rows),'first':rows[0],'last':rows[-1],'density_residual_key':residual,'fields':fields(local)})
  mesh=(EXAMPLES/'mesh_NACA0012_inv.su2').read_bytes()
  assert self.remote(job['attempt']['workspace']['inputs_path']+'/'+MESH_PATH)==mesh
  assert self.call('POST',self.prefix+'/su2:solve',json=body,headers=headers)['job_id']==jid
  self.save();return {'id':pub['id'],'base':base,'flow':flow,'restart':self.live('restart_flow.dat'),'manifest':manifest}
 def run(self):
  try:
   with self.api:
    from app.services.process_identity_native import observe
    self.result['API_birth']=observe(self.api.proc.pid)
    with httpx.Client(base_url=self.api.base+'/api/v1',trust_env=False,timeout=120) as self.client:
     account={'email':'su2-'+secrets.token_hex(6)+'@example.com','password':secrets.token_hex(24)};write(self.root/'private/account.json',account)
     self.client.headers['Authorization']='Bearer '+self.call('POST','/auth/register',json={**account,'name':'Actual SU2 acceptance'})['token']
     self.workspace=self.call('GET','/workspaces')[0]['id'];self.case('NACA0012 real solve and restart')
     capabilities=self.call('GET','/runtime/capabilities');write(self.root/'capabilities.json',capabilities)
     selected=next(v for v in capabilities['environment_registry'] if v['tool']=='su2');assert selected['available']
     config=(EXAMPLES/'inv_NACA0012.cfg').read_text('utf8').replace('MESH_FILENAME= mesh_NACA0012_inv.su2','MESH_FILENAME= '+MESH_PATH)
     self.put(MESH_PATH,(EXAMPLES/'mesh_NACA0012_inv.su2').read_text('utf8'));self.put(CONFIG_PATH,config)
     first=self.solve();assert selected['value']==first['manifest']['descriptor']['executable']
     continuation=config.replace('RESTART_SOL= NO','RESTART_SOL= YES').replace('ITER= 250','ITER= 300').replace('SOLUTION_FILENAME= solution_flow','SOLUTION_FILENAME= restart_flow')
     self.put(CONFIG_PATH,continuation);second=self.solve(first['restart'])
     assert first['flow']!=second['flow'] and self.remote(first['base']+'/files/flow.vtu')==first['flow']
     self.result['checks']+=['actual_250_iteration_budget_Euler_solve_residual_drop','actual_restart_with_frozen_solution_input','finite_nonconstant_density_pressure_temperature_Mach','special_config_path_and_frozen_mesh','publication_hashes_live_aliases_idempotency_and_retained_history'];self.save()
     self.put(CONFIG_PATH,config.replace('SOLVER= EULER','SOLVER= INVALID_SOLVER'))
     jid=self.call('POST',self.prefix+'/su2:solve',json={'config_path':CONFIG_PATH})['job_id'];job=self.settled(jid,'failed')
     assert len(job['attempt']['child_processes']['children'])==1
     assert job['attempt']['outputs'].get('publication',{}).get('status')!='committed'
     assert self.live('flow.vtu')==second['flow'] and self.remote(first['base']+'/files/flow.vtu')==first['flow']
     self.result['checks'].append('actual_invalid_solver_config_failure_preserves_previous_results')
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
