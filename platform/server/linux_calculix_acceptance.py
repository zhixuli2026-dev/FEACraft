"""Real CCX solve versus independent linear C3D8 stiffness, S3 publications; no LLM."""
import argparse,json,os,re,secrets
from pathlib import Path
import httpx,numpy as np
from linux_flotherm_acceptance import Run as NativeRun,read,write,sha

DECK='''*NODE
1,0,0,0
2,1,0,0
3,1,1,0
4,0,1,0
5,0,0,1
6,1,0,1
7,1,1,1
8,0,1,1
*ELEMENT,TYPE=C3D8,ELSET=ALL
1,1,2,3,4,5,6,7,8
*INCLUDE,INPUT=C-run/material.inp
*SOLID SECTION,ELSET=ALL,MATERIAL=STEEL
*BOUNDARY
1,1,3,0
2,1,3,0
3,1,3,0
4,1,3,0
*STEP
*STATIC
*CLOAD
5,3,-1000
6,3,-1000
7,3,-1000
8,3,-1000
*NODE FILE
U
*EL FILE
S
*END STEP
'''
MATERIAL='*MATERIAL,NAME=STEEL\n*ELASTIC\n210000,0.3\n'

def reference(load):
 """Independent eight-point integration of the unit cube's linear stiffness."""
 signs=np.array([[-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1],[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]],dtype=float)
 E=210000.;nu=.3;lam=E*nu/((1+nu)*(1-2*nu));mu=E/(2*(1+nu))
 D=np.zeros((6,6));D[:3,:3]=lam;np.fill_diagonal(D[:3,:3],lam+2*mu);D[3:,3:]=np.eye(3)*mu
 K=np.zeros((24,24))
 for x in (-1/np.sqrt(3),1/np.sqrt(3)):
  for y in (-1/np.sqrt(3),1/np.sqrt(3)):
   for z in (-1/np.sqrt(3),1/np.sqrt(3)):
    B=np.zeros((6,24))
    for i,(sx,sy,sz) in enumerate(signs):
     dx=sx*(1+sy*y)*(1+sz*z)/4;dy=sy*(1+sx*x)*(1+sz*z)/4;dz=sz*(1+sx*x)*(1+sy*y)/4
     B[:,3*i:3*i+3]=[[dx,0,0],[0,dy,0],[0,0,dz],[dy,dx,0],[0,dz,dy],[dz,0,dx]]
    K+=B.T@D@B/8
 F=np.tile([0.,0.,-load],4);U=np.zeros(24);U[12:]=np.linalg.solve(K[12:,12:],F)
 assert np.linalg.norm(K[12:,12:]@U[12:]-F)<1e-8
 return U.reshape((8,3))

def displacements(data):
 result={};active=False
 for line in data.decode('ascii','replace').splitlines():
  if line.startswith(' -4'):active='DISP' in line;continue
  if active and line.startswith(' -3'):break
  if active and line.startswith(' -1'):
   node=int(line[3:13]);values=re.findall(r'[-+]?\d+\.\d+(?:[Ee][-+]?\d+)?',line[13:]);result[node]=list(map(float,values[:3]))
 assert set(result)==set(range(1,9)) and all(len(v)==3 for v in result.values())
 return np.array([result[n] for n in range(1,9)])

class Run(NativeRun):
 label='calculix'
 def solve(self,path,load):
  headers={'Idempotency-Key':'ccx-'+secrets.token_hex(8)};body={'input_path':path}
  jid=self.call('POST',self.prefix+'/calculix:solve',json=body,headers=headers)['job_id'];job=self.settled(jid)
  children=job['attempt']['child_processes']['children'];assert len(children)==1 and next(iter(children.values()))['purpose']=='calculix_solve'
  pub=job['attempt']['outputs']['publication'];assert pub['status']=='committed';base='.vibeflow/publications/'+pub['id']
  manifest=json.loads(self.remote(base+'/manifest.json'));write(self.root/f'publication-{jid}.json',manifest)
  for name,record in manifest['files'].items():
   data=self.remote(base+'/files/'+name);assert sha(data)==record['sha256'] and data==self.live(name)
  frd=self.live(path[:-4]+'.frd');actual=displacements(frd);expected=reference(load)
  assert np.allclose(actual,expected,rtol=5e-5,atol=1e-8),(actual.tolist(),expected.tolist())
  error=float(np.max(np.abs(actual-expected)));self.result.setdefault('numerics',[]).append({'job_id':jid,'load_per_top_node':load,'max_absolute_error':error,'actual':actual.tolist(),'reference':expected.tolist()})
  source=manifest['descriptor']['source'];assert 'C-run/material.inp' in source['files']
  assert self.remote(job['attempt']['workspace']['inputs_path']+'/C-run/material.inp')==MATERIAL.encode()
  assert self.call('POST',self.prefix+'/calculix:solve',json=body,headers=headers)['job_id']==jid
  return {'id':pub['id'],'base':base,'frd':frd,'job':jid,'manifest':manifest}
 def run(self):
  try:
   with self.api:
    from app.services.process_identity_native import observe
    self.result['API_birth']=observe(self.api.proc.pid)
    with httpx.Client(base_url=self.api.base+'/api/v1',trust_env=False,timeout=120) as self.client:
     account={'email':'calculix-'+secrets.token_hex(6)+'@example.com','password':secrets.token_hex(24)};write(self.root/'private/account.json',account)
     self.client.headers['Authorization']='Bearer '+self.call('POST','/auth/register',json={**account,'name':'Actual CalculiX acceptance'})['token']
     self.workspace=self.call('GET','/workspaces')[0]['id'];self.case('Actual elastic cube')
     capabilities=self.call('GET','/runtime/capabilities');write(self.root/'capabilities.json',capabilities)
     selected=next(v for v in capabilities['environment_registry'] if v['tool']=='calculix');assert selected['available']
     path='C-run/ccx space & beam/model test.inp';self.put('C-run/material.inp',MATERIAL);self.put(path,DECK)
     first=self.solve(path,1000);self.put(path,DECK.replace('-1000','-2000'));second=self.solve(path,2000)
     assert selected['value']==first['manifest']['descriptor']['executable']
     assert first['frd']!=second['frd'] and self.remote(first['base']+'/files/'+path[:-4]+'.frd')==first['frd']
     a=np.array(self.result['numerics'][0]['actual']);b=np.array(self.result['numerics'][1]['actual']);assert np.allclose(b,2*a,rtol=5e-5,atol=1e-8)
     self.result['checks']+=['actual_Linux_or_Windows_CCX_two_loads_match_independent_stiffness','included_material_frozen','special_character_paths_preserved','new_publication_and_old_Frd_bytes_retained'];self.save()
     self.put(path,DECK.replace('1,1,2,3,4,5,6,7,8','1,1,2,3,4,5,6,7,999'))
     jid=self.call('POST',self.prefix+'/calculix:solve',json={'input_path':path})['job_id'];job=self.settled(jid,'failed')
     assert job['attempt']['outputs'].get('publication',{}).get('status')!='committed'
     assert self.live(path[:-4]+'.frd')==second['frd'] and self.remote(first['base']+'/files/'+path[:-4]+'.frd')==first['frd']
     self.result['checks'].append('actual_invalid_model_failure_preserves_previous_results')
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
