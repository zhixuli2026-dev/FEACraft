"""Current deployed ParaView API: real larger data, concurrency, cancel and restart. No LLM."""
import argparse,hashlib,io,json,secrets,subprocess,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from PIL import Image
import numpy as np
from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
from vtkmodules.util.numpy_support import vtk_to_numpy
ROOT=Path(__file__).resolve().parents[2]
SCRIPT='''import json,time,os
from pathlib import Path
from paraview.simple import Sphere,CreateView,Show,Render,SaveData,SaveScreenshot,_DisableFirstRenderCameraReset
root=Path(os.environ['VIBEFLOW_CASE_ROOT']);started=time.time();print('PV_LARGE_STARTED',started,flush=True)
s=Sphere(ThetaResolution=1024,PhiResolution=512,Radius=2);s.UpdatePipeline();time.sleep(5)
_DisableFirstRenderCameraReset();v=CreateView('RenderView');v.ViewSize=[640,480];Show(s,v);v.ResetCamera();Render(v)
SaveData(str(root/'D-post/large.vtp'),proxy=s);SaveScreenshot(str(root/'D-post/large.png'),view=v)
info=s.GetDataInformation();(root/'D-post/large.json').write_text(json.dumps({'points':info.GetNumberOfPoints(),'cells':info.GetNumberOfCells(),'started':started,'finished':time.time()}))
'''
CANCEL='''import time
from paraview.simple import Sphere
s=Sphere();s.UpdatePipeline();print('PV_CANCEL_READY',flush=True);time.sleep(120)
raise RuntimeError('Cancellation did not stop the real tool')
'''
def run():
 folder=ROOT/'tmp'/('paraview-full-'+secrets.token_hex(6));folder.mkdir();(folder/'private').mkdir()
 result={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'checks':[],'jobs':[]};print('PARAVIEW_FULL_EVIDENCE '+str(folder),flush=True)
 def save(name,value):(folder/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
 with httpx.Client(base_url='http://127.0.0.1:57700/api/v1',trust_env=False,timeout=180) as c:
  def call(method,path,expected=(200,201,202),**kwargs):
   deadline=time.monotonic()+45
   while True:
    r=c.request(method,path,**kwargs)
    if r.status_code==409 and isinstance(r.json().get('detail'),dict) and r.json()['detail'].get('code')=='storage_quota_busy' and time.monotonic()<deadline:time.sleep(.2);continue
    assert r.status_code in expected,(path,r.status_code,r.text[:300]);return r.json()
  def raw(cid,path):
   r=c.get(f'/cases/{cid}/raw',params={'path':path});r.raise_for_status();return r.content
  def put(cid,path,content):call('PUT',f'/cases/{cid}/file',json={'path':path,'content':content})
  def settled(jid,status):
   deadline=time.monotonic()+240
   while True:
    v=call('GET',f'/jobs/{jid}')
    if v.get('ended_at') and not v['completion_pending'] and v.get('attempt',{}).get('process_stop'):break
    assert time.monotonic()<deadline,('Job still active',jid);time.sleep(.3)
   save(f'job-{jid}.json',v);result['jobs'].append({'id':jid,'status':v['status']});save('result.json',result)
   assert v['status']==status and v['attempt']['process_stop']['verified'] and v['attempt']['process_stop']['scope_empty']
   children=v['attempt']['child_processes']['children'];assert children
   for child in children.values():assert child['stop']['verified'] and child['stop']['scope_empty']
   return v
  try:
   account={'email':'paraview-full-'+secrets.token_hex(8)+'@example.com','password':secrets.token_hex(24)};save('private/account.json',account)
   c.headers['Authorization']='Bearer '+call('POST','/auth/register',json={**account,'name':'ParaView full API acceptance'})['token']
   workspace=call('GET','/workspaces')[0]['id'];cases=[call('POST',f'/workspaces/{workspace}/cases',json={'name':'ParaView '+b})['id'] for b in ('pvpython','pvbatch')]
   result['cases']=cases;save('result.json',result)
   other=call('POST','/auth/register',json={'email':'pv-other-'+secrets.token_hex(8)+'@example.com','password':secrets.token_hex(24),'name':'Unrelated user'})['token']
   for cid in cases:put(cid,'Ref/large.py',SCRIPT)
   prefix=f'/cases/{cases[0]}/paraview:run'
   for body,status in [({'script_path':'Ref/large.py','backend':'bad'},400),({'script_path':'../outside.py'},400),({'script_path':'Ref/not.txt'},400),({'script_path':'Ref/missing.py'},404)]:call('POST',prefix,expected=(status,),json=body)
   call('POST',prefix,expected=(404,),json={'script_path':'Ref/large.py'},headers={'Authorization':'Bearer '+other})
   put(cases[0],'Ref/oversized.py','#'*(1024*1024+1));call('POST',prefix,expected=(413,),json={'script_path':'Ref/oversized.py'})
   result['checks'].append('backend_path_missing_size_and_cross_user_admission');save('result.json',result)
   def submit(item):
    cid,backend=item;return call('POST',f'/cases/{cid}/paraview:run',json={'script_path':'Ref/large.py','backend':backend},headers={'Idempotency-Key':'pv-full-'+secrets.token_hex(8)})['job_id']
   with ThreadPoolExecutor(max_workers=2) as pool:jids=list(pool.map(submit,zip(cases,('pvpython','pvbatch'))))
   result['submitted']=jids;save('result.json',result)
   records=[];digests={}
   for cid,jid in zip(cases,jids):
    job=settled(jid,'succeeded');assert job['attempt']['outputs']['publication']['status']=='committed'
    call('GET',f'/jobs/{jid}',expected=(404,),headers={'Authorization':'Bearer '+other})
    stats=json.loads(raw(cid,'D-post/large.json'));assert stats['points']==1024*510+2 and stats['cells']==2*1024*510;records.append(stats)
    for name in ['large.vtp','large.png','large.json']:
     data=raw(cid,'D-post/'+name);digests[f'{cid}/{name}']=hashlib.sha256(data).hexdigest();(folder/f'{cid}-{name}').write_bytes(data)
    reader=vtkXMLPolyDataReader();reader.SetFileName(str(folder/f'{cid}-large.vtp'));reader.Update();mesh=reader.GetOutput()
    assert mesh.GetNumberOfPoints()==stats['points'] and mesh.GetNumberOfCells()==stats['cells']
    radii=np.linalg.norm(vtk_to_numpy(mesh.GetPoints().GetData()).astype(float),axis=1);assert float(np.max(np.abs(radii-2)))<2e-6
    with Image.open(folder/f'{cid}-large.png') as pic:assert pic.size==(640,480) and len(np.unique(np.array(pic.convert('RGB')).reshape(-1,3),axis=0))>64
   assert max(x['started'] for x in records)<min(x['finished'] for x in records),'Submitted jobs did not actually overlap'
   result.update(numerics=records,output_sha256=digests);result['checks'].append('both_backends_overlapping_522242_point_render_and_independent_radius_check');save('result.json',result)
   cid=cases[0];put(cid,'Ref/cancel.py',CANCEL);jid=call('POST',f'/cases/{cid}/paraview:run',json={'script_path':'Ref/cancel.py','backend':'pvpython'})['job_id']
   deadline=time.monotonic()+90
   while True:
    job=call('GET',f'/jobs/{jid}')
    if 'PV_CANCEL_READY' in job.get('log_backlog',''):break
    assert time.monotonic()<deadline and not job.get('ended_at');time.sleep(.3)
   call('POST',f'/jobs/{jid}:cancel');settled(jid,'cancelled')
   for name in ['large.vtp','large.png','large.json']:assert hashlib.sha256(raw(cid,'D-post/'+name)).hexdigest()==digests[f'{cid}/{name}']
   result['checks'].append('cancel_actual_running_ParaView_scope_and_preserve_previous_outputs');save('result.json',result)
   q="SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
   assert subprocess.check_output(['docker','exec','vf-report-s3-20261117-postgres','psql','-U','vibeflow','-d','vibeflow','-tAc',q],text=True).strip()=='0'
   subprocess.run(['docker','restart','vf-report-s3-20261117-api'],check=True,capture_output=True,timeout=60)
   deadline=time.monotonic()+60
   while True:
    try:r=c.get('http://127.0.0.1:57700/api/health');healthy=r.status_code==200
    except httpx.HTTPError:healthy=False
    if healthy:break
    assert time.monotonic()<deadline;time.sleep(.5)
   for cid,jid in zip(cases,jids):
    assert call('GET',f'/jobs/{jid}')['status']=='succeeded'
    for name in ['large.vtp','large.png','large.json']:assert hashlib.sha256(raw(cid,'D-post/'+name)).hexdigest()==digests[f'{cid}/{name}']
   result['checks'].append('actual_API_restart_preserves_PG_job_history_and_all_S3_output_bytes');result['passed']=True
  except Exception as e:result['failure']={'type':type(e).__name__,'message':str(e)[:1200]};raise
  finally:save('result.json',result)
 print(json.dumps(result))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('review',type=Path);a=p.parse_args();v=json.loads(a.review.read_text('utf8'));assert v['classification']=='no_llm'
 for n,h in v['sources'].items():assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h,n
 run()
