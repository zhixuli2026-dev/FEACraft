"""Complete actual cancellation/restart checks using an existing large-data case. No LLM."""
import argparse,hashlib,json,secrets,subprocess,time
from pathlib import Path,PurePosixPath
import httpx
ROOT=Path(__file__).resolve().parents[2]
def run(previous):
 old=json.loads((previous/'result.json').read_text('utf8'));assert 'both_backends_overlapping_522242_point_render_and_independent_radius_check' in old['checks']
 b=ROOT/'tmp'/('paraview-cancel-restart-'+secrets.token_hex(6));b.mkdir();result={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'previous':str(previous),'checks':[]}
 def save(name,value):(b/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
 print('PARAVIEW_RECOVERY_EVIDENCE '+str(b),flush=True)
 with httpx.Client(base_url='http://127.0.0.1:57700/api/v1',trust_env=False,timeout=180) as c:
  def call(method,path,**kw):
   until=time.monotonic()+45
   while True:
    r=c.request(method,path,**kw)
    if r.status_code==409 and isinstance(r.json().get('detail'),dict) and r.json()['detail'].get('code')=='storage_quota_busy' and time.monotonic()<until:time.sleep(.2);continue
    r.raise_for_status();return r.json()
  def outputs():
   hashes={}
   for cid in old['cases']:
    for name in ('large.vtp','large.png','large.json'):
     r=c.get(f'/cases/{cid}/raw',params={'path':'D-post/'+name});r.raise_for_status();hashes[f'{cid}/{name}']=hashlib.sha256(r.content).hexdigest()
   assert hashes==old['output_sha256'];return hashes
  try:
   c.headers['Authorization']='Bearer '+call('POST','/auth/login',json=json.loads((previous/'private/account.json').read_text('utf8')))['token']
   outputs();cid=old['cases'][0];marker='pv-ready-'+secrets.token_hex(8)
   script="from pathlib import Path\nimport os,time\nfrom paraview.simple import Sphere\ns=Sphere();s.UpdatePipeline()\nPath(os.environ['VIBEFLOW_CASE_ROOT'],'D-post',"+repr(marker)+").write_text('actual ParaView ready')\ntime.sleep(120)\nraise RuntimeError('Real cancellation did not stop the tool')\n"
   call('PUT',f'/cases/{cid}/file',json={'path':'Ref/cancel-ready.py','content':script})
   jid=call('POST',f'/cases/{cid}/paraview:run',json={'script_path':'Ref/cancel-ready.py','backend':'pvpython'})['job_id'];result['job_id']=jid;save('result.json',result)
   prefix=subprocess.check_output(['docker','exec','vf-report-s3-20261117-postgres','psql','-U','vibeflow','-d','vibeflow','-tAc','SELECT s3_prefix FROM cases WHERE id='+str(int(cid))],text=True).strip()
   assert not PurePosixPath(prefix).is_absolute() and '..' not in PurePosixPath(prefix).parts
   deadline=time.monotonic()+90
   while True:
    job=call('GET',f'/jobs/{jid}');run_path=job.get('attempt',{}).get('workspace',{}).get('run_path')
    if run_path:
     path=str(PurePosixPath('/data/files')/prefix/run_path/'D-post'/marker)
     code="from pathlib import Path;import sys; p=Path(sys.argv[1]); print('ready' if p.is_file() and p.read_text()=='actual ParaView ready' else 'waiting')"
     seen=subprocess.check_output(['docker','exec','vf-report-s3-20261117-arq-worker','python','-c',code,path],text=True).strip()
     if seen=='ready':break
    assert time.monotonic()<deadline and not job.get('ended_at'),('Tool readiness missing',jid,job['status']);time.sleep(.3)
   save('running-job.json',job);result['readiness_file']=path
   call('POST',f'/jobs/{jid}:cancel')
   deadline=time.monotonic()+90
   while True:
    job=call('GET',f'/jobs/{jid}')
    if job.get('ended_at') and not job['completion_pending'] and job.get('attempt',{}).get('process_stop'):break
    assert time.monotonic()<deadline;time.sleep(.3)
   save('cancelled-job.json',job);assert job['status']=='cancelled'
   assert job['attempt']['process_stop']['verified'] and job['attempt']['process_stop']['scope_empty']
   children=job['attempt']['child_processes']['children'];assert children
   for child in children.values():assert child['stop']['verified'] and child['stop']['scope_empty']
   assert job['attempt']['outputs'].get('publication',{}).get('status')!='committed'
   outputs();result['checks'].append('actual_ParaView_ready_then_cancelled_with_verified_scope_and_old_outputs');save('result.json',result)
   q="SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
   assert subprocess.check_output(['docker','exec','vf-report-s3-20261117-postgres','psql','-U','vibeflow','-d','vibeflow','-tAc',q],text=True).strip()=='0'
   subprocess.run(['docker','restart','vf-report-s3-20261117-api'],check=True,capture_output=True,timeout=60)
   deadline=time.monotonic()+60
   while True:
    try:healthy=c.get('http://127.0.0.1:57700/api/health').status_code==200
    except httpx.HTTPError:healthy=False
    if healthy:break
    assert time.monotonic()<deadline;time.sleep(.5)
   for prior in old['submitted']:assert call('GET',f'/jobs/{prior}')['status']=='succeeded'
   result['output_sha256_after_restart']=outputs();result['checks'].append('actual_API_restart_retains_PG_history_and_all_S3_bytes');result['passed']=True
  except Exception as e:result['failure']={'type':type(e).__name__,'message':str(e)[:1000]};raise
  finally:save('result.json',result)
 print(json.dumps(result))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('review',type=Path);p.add_argument('previous',type=Path);a=p.parse_args();v=json.loads(a.review.read_text('utf8'));assert v['classification']=='no_llm'
 for n,h in v['sources'].items():assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h,n
 run(a.previous)
