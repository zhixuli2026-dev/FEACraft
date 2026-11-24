"""Real authorized DeepSeek runs an engineering case through the deployed Linux API."""
import argparse,asyncio,hashlib,io,json,secrets,subprocess,time,zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from live_llm_acceptance import source

SERVER=Path(__file__).resolve().parent;ROOT=SERVER.parents[1]
def sha(data):return hashlib.sha256(data).hexdigest()

def run(configuration,resume_from=None,finish_from=None):
 assert configuration['provider']=='deepseek' and urlsplit(configuration['base_url']).hostname=='api.deepseek.com'
 evidence=ROOT/'tmp'/('live-linux-engineering-'+secrets.token_hex(6));evidence.mkdir();(evidence/'private').mkdir()
 result={'passed':False,'provider':'deepseek','host':'api.deepseek.com','model':configuration['model'],'mock_llm_calls':0,'real_model_calls':0,'checks':[],'environment':'deployed Linux API, PostgreSQL and S3'}
 events=[]
 def write(name,value):
  data=json.dumps(value,ensure_ascii=False,indent=2)+'\n';assert configuration['key'] not in data
  (evidence/name).write_text(data,encoding='utf8')
 def save():write('result.json',result);write('events.json',events)
 print('LIVE_ENGINEERING_EVIDENCE '+str(evidence),flush=True)
 try:
  with httpx.Client(base_url='http://127.0.0.1:57700/api/v1',trust_env=False,timeout=660) as client:
   def call(method,path,**kwargs):
    until=time.monotonic()+45
    while True:
     response=client.request(method,path,**kwargs)
     if response.status_code!=409 or response.json().get('detail',{}).get('code') not in {'storage_quota_busy','validation_busy'}:break
     assert time.monotonic()<until;time.sleep(.3)
    assert response.status_code in [200,201,202],(path,response.status_code,response.text[:300].replace(configuration['key'],'[redacted]'))
    return response.json()
   previous_folder=finish_from or resume_from
   prior=json.loads((previous_folder/'result.json').read_text('utf8')) if previous_folder else None
   if prior:
    assert not prior['passed']
    previous=json.loads((previous_folder/'events.json').read_text('utf8'))
    previous_tools=[e for e in previous if e.get('type')=='tool.result']
    if finish_from:
     assert sum(e.get('tool')=='run_shell_command' and e.get('ok') for e in previous_tools)==1
    else:assert prior['real_model_calls']==0 and not previous_tools
    account=json.loads((previous_folder/'private/account.json').read_text('utf8'));write('private/account.json',account)
    client.headers['Authorization']='Bearer '+call('POST','/auth/login',json=account)['token']
    case={'id':prior['case_id']};result['finished_from' if finish_from else 'resumed_from']=str(previous_folder)
   else:
    account={'email':'linux-engineering-'+secrets.token_hex(8)+'@example.com','password':secrets.token_hex(24)};write('private/account.json',account)
    client.headers['Authorization']='Bearer '+call('POST','/auth/register',json={**account,'name':'Live engineering acceptance'})['token']
    workspace=call('GET','/workspaces')[0]['id'];case=call('POST',f'/workspaces/{workspace}/cases',json={'name':'Actual model-driven NACA engineering'})
   prefix='/cases/'+str(case['id']);result['case_id']=case['id']
   def put(path,text):
    if prior:
     existing=client.get(prefix+'/raw',params={'path':path});existing.raise_for_status();assert existing.content==text.encode('utf8')
    else:call('PUT',prefix+'/file',json={'path':path,'content':text})
   put('C-run/naca.cfg',(SERVER/'examples/su2/inv_NACA0012.cfg').read_text('utf8').replace('MESH_FILENAME= mesh_NACA0012_inv.su2','MESH_FILENAME= C-run/naca.su2'))
   put('C-run/naca.su2',(SERVER/'examples/su2/mesh_NACA0012_inv.su2').read_text('utf8'))
   for name in ['engineering_pipeline.py','naca_postprocess.py']:put('Ref/'+name,(SERVER/'examples/paraview'/name).read_text('utf8'))
   put('Ref/render.json',json.dumps({'factor':1}))
   guide=('这是隔离的 Linux 工程算例。运行已提供的真实流程：python Ref/engineering_pipeline.py。'
    '调用 run_shell_command 时 timeout_seconds=600，使用默认 runtime。'
    '脚本实际运行 SU2 8.5.0、ParaView 5.11.2 和 Pandoc，生成 VTK、PNG、DOCX 和 E-report/engineering-result.json。'
    '不要修改脚本、输入或伪造输出，不要安装软件，不访问外部网站。成功后必须使用 read_text_file 读取结果 JSON。'
    '最后引用 points、density_log10_residual、maximum_mach、sonic_points、运行生成的完整 proof 字符串和 DOCX/PNG 路径；明确这是数值执行核验，不是实验验证。')
   put('Ref/engineering-task.txt',guide)
   profile={'id':prior['profile_id']} if prior else call('POST','/provider-profiles',json={'provider':'deepseek','name':'Authorized real Linux engineering','base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
   result['profile_id']=profile['id']
   conversation={'id':prior['conversation_id']} if prior else call('POST','/conversations',json={'case_id':case['id'],'title':'Real DeepSeek engineering pipeline','provider_profile_id':profile['id'],'model':configuration['model'],'permission_mode':'full_access'})
   cid=conversation['id'];result['conversation_id']=cid;save()
   prompt='这是已审阅的隔离算例。请依次用 read_text_file 读取 Ref/engineering-task.txt、run_shell_command 执行说明中的现有脚本、read_text_file 读取生成的结果 JSON，然后报告。无需额外列目录、读脚本或创建计划；任何步骤失败就如实报告并停止，不自行修改脚本或输入。'
   if finish_from:
    existing=client.get(prefix+'/raw',params={'path':'E-report/engineering-result.json'});existing.raise_for_status()
    assert [step['returncode'] for step in existing.json()['steps']]==[0,0,0]
    prompt='上一轮工程计算已成功，随后模型网络连接失败。请只用 read_text_file 读取 E-report/engineering-result.json 并报告结果，禁止再次执行 Shell、计算或改文件。引用 points、density_log10_residual、maximum_mach、sonic_points、完整 proof、DOCX 和 PNG 路径，明确是数值执行核验而非实验验证。'
   @contextmanager
   def message_stream():
    until=time.monotonic()+45
    while True:
     with client.stream('POST',f'/conversations/{cid}/messages',json={'content':prompt,'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
      if response.status_code==409:
       response.read();detail=response.json().get('detail')
       if isinstance(detail,dict) and detail.get('code')=='storage_quota_busy' and time.monotonic()<until:
        result.setdefault('admission_rejections',[]).append({'status':409,'code':detail['code']});save();time.sleep(.3);continue
      yield response;return
   with message_stream() as response:
    if response.is_error:
     result['HTTP_rejection']={'status':response.status_code,'body':response.read().decode('utf8','replace').replace(configuration['key'],'[redacted]')};save()
    response.raise_for_status()
    for line in response.iter_lines():
     if line.startswith('data:'):
      event=json.loads(line[5:]);events.append(event)
      if event.get('type') in ['tool.result','run.completed','stream.error','request.failed','request.timed_out']:print('LIVE_EVENT '+json.dumps({k:event[k] for k in ['type','tool','ok'] if k in event}),flush=True);save()
      assert len(json.dumps(events))<8*1024*1024,'Evidence bound exceeded'
   history=call('GET',f'/conversations/{cid}');write('conversation.json',history)
   request_states=[v['payload'] for v in history['events'] if v['type']=='request_state'];write('request-states.json',request_states)
   run_ids={e.get('run_id') for e in events if e.get('run_id')}
   completed=[v for v in request_states if v['status']=='completed' and v['run_id'] in run_ids];result['real_model_calls']=len({v['request_id'] for v in completed});save()
   assert not [e for e in events if e.get('type') in ['stream.error','request.failed','request.timed_out','run.cancelled']]
   tools=[e for e in events if e.get('type')=='tool.result'];assert tools and all(e['ok'] for e in tools)
   assert sum(e['tool']=='run_shell_command' for e in tools)==(0 if finish_from else 1)
   assert sum(e['tool']=='read_text_file' for e in tools)>=(1 if finish_from else 2)
   assert any(e.get('type')=='run.completed' for e in events) and result['real_model_calls']>=(2 if finish_from else 3)
   def raw(path):
    response=client.get(prefix+'/raw',params={'path':path});response.raise_for_status();return response.content
   metrics=json.loads(raw('E-report/engineering-result.json'));write('engineering-result.json',metrics)
   assert metrics['points']==5233 and metrics['cells']==10216 and metrics['density_log10_residual']<-8 and metrics['sonic_points']==148 and metrics['pressure_ratio_max_absolute_error']<1e-12
   assert [v['tool'] for v in metrics['steps']]==['su2','paraview','pandoc'] and all(v['returncode']==0 for v in metrics['steps'])
   for path,record in metrics['outputs'].items():
    data=raw(path);assert sha(data)==record['sha256'] and len(data)==record['bytes'];(evidence/Path(path).name).write_bytes(data)
   with zipfile.ZipFile(io.BytesIO(raw('E-report/engineering.docx'))) as doc:
    assert metrics['proof'] in doc.read('word/document.xml').decode()
    assert any(sha(doc.read(n))==metrics['outputs']['D-post/pressure.png']['sha256'] for n in doc.namelist() if n.startswith('word/media/'))
   answer=''.join(e.get('text','') for e in events if e.get('type')=='text.delta');assert metrics['proof'] in answer and '5233' in answer and '148' in answer and 'engineering.docx' in answer
   result['answer']=answer;result['tools']=[{'tool':e['tool'],'ok':e['ok'],'tool_call_id':e['tool_call_id']} for e in tools]
   query='SELECT coalesce(json_agg(t),\'[]\') FROM (SELECT id,phase,children_json FROM native_requests WHERE case_id='+str(int(case['id']))+') t'
   command=subprocess.run(['docker','exec','vf-report-s3-20261117-postgres','psql','-U','vibeflow','-d','vibeflow','-tAc',query],capture_output=True,text=True,timeout=20,check=True)
   records=json.loads(command.stdout);write('native-requests.json',records);assert len(records)==1 and records[0]['phase']=='succeeded'
   children=json.loads(records[0]['children_json'])['children'];assert children
   for child in children.values():assert child['stop']['verified'] and child['stop']['scope_empty']
   result['checks']=['real_model_selected_tools_and_completed_conversation','actual_SU2_ParaView_Pandoc_pipeline','PG_S3_staging_outputs_and_embedded_report_image','native_execution_scope_stopped','model_read_runtime_random_proof_and_numerics'];result['passed']=True
 except BaseException as error:
  result['failure']={'type':type(error).__name__,'message':str(error).replace(configuration['key'],'[redacted]')[:1500]};raise
 finally:save()

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('review',type=Path);p.add_argument('--source-profile-id',type=int,default=26);mode=p.add_mutually_exclusive_group();mode.add_argument('--resume-from',type=Path);mode.add_argument('--finish-from',type=Path);a=p.parse_args();review=json.loads(a.review.read_text('utf8'));assert review['classification']=='real_llm'
 for name,digest in review['sources'].items():assert sha((ROOT/name).read_bytes())==digest,name
 run(asyncio.run(source(a.source_profile_id)),a.resume_from,a.finish_from)
