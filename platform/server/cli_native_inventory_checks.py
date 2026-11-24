"""Actual native inventory faults; no model output or filesystem result is replaced."""
FIXTURE=r'''
import asyncio,hashlib,json,subprocess,time,uuid
from pathlib import Path
from fastapi import Depends
from app.config import get_settings
from app.deps import get_current_user
from app.services import cli_native_inventory as inventory,cli_native_usage as native_usage
from app.services.storage_usage import Counter,MeasurementUnavailable,_Enough

@app.post('/api/v1/test/native-inventory-faults')
async def native_inventory_faults(user=Depends(get_current_user)):
 def checks():
  settings=get_settings();report={'passed':False,'real_model_requests':0,'mock_llm_calls':0,'checks':[]}
  destination=Path(__file__).parent/'native-inventory-faults.json'
  def save():destination.write_text(json.dumps(report,indent=2),encoding='utf8')
  def wsl(code,*args):
   result=subprocess.run(['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec','/usr/bin/python3','-I','-S','-c',code,*args],
    capture_output=True,text=True,encoding='utf8',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
   assert result.returncode==0,'Actual native fixture operation failed'
   return result.stdout
  selected=json.loads(wsl("import tempfile,os,sys,json; r=tempfile.mkdtemp(prefix='vibeflow-inventory-faults-',dir='/tmp'); p=r+'/users/'+sys.argv[1]; os.makedirs(p); open(p+'/plain','wb').write(b'x'*128); os.symlink('/absent-inventory-target',p+'/link'); print(json.dumps(r))",str(user.id)))
  assert selected.startswith('/tmp/vibeflow-inventory-faults-') and '/' not in selected[len('/tmp/'):]
  settings.codex_wsl_state_root=selected;report['native_root']=selected
  def scan(*,maximum=200000,stop_after=None,initial_bytes=0,initial_entries=0,owner_id=None):
   counter=Counter(settings.model_copy(update={'storage_measurement_max_entries':maximum}),time.monotonic()+30,stop_after)
   counter.bytes=initial_bytes;counter.entries=initial_entries
   try:native_usage.scan(settings,counter,user_id=user.id if owner_id is None else owner_id);kind='complete'
   except _Enough:kind='lower_bound'
   except MeasurementUnavailable as error:kind=str(error)
   return {'kind':kind,'bytes':counter.bytes,'entries':counter.entries}
  try:
   result=scan();assert result=={'kind':'complete','bytes':128+len('/absent-inventory-target'),'entries':2}
   report['checks'].append({'name':'actual_file_and_link_metadata','result':result});save()
   other=user.id+1
   other_path=selected+'/users/'+str(other)
   result=scan(owner_id=other,initial_bytes=17,initial_entries=3)
   assert result=={'kind':'complete','bytes':17,'entries':3}
   report['checks'].append({'name':'missing_owner_preserves_existing_counter','result':result});save()
   wsl("import os,sys; os.mkdir(sys.argv[1]); open(sys.argv[1]+'/data','wb').write(b'y'*31)",other_path)
   try:
    result=scan(owner_id=other);assert result=={'kind':'complete','bytes':31,'entries':1}
    report['checks'].append({'name':'new_owner_data_after_absence_is_measured','result':result});save()
   finally:wsl("import os,sys; os.unlink(sys.argv[1]+'/data'); os.rmdir(sys.argv[1])",other_path)
   wsl('import os,sys; os.symlink(sys.argv[2],sys.argv[1])',other_path,selected+'/users/'+str(user.id))
   try:
    result=scan(owner_id=other);assert result['kind']!='complete' and result['bytes']==0
    report['checks'].append({'name':'redirected_owner_is_refused','result':result});save()
   finally:wsl('import os,sys; os.unlink(sys.argv[1])',other_path)
   result=scan(stop_after=120,initial_bytes=100,initial_entries=7)
   assert result['kind']=='lower_bound' and result['bytes']>120 and result['entries']>7
   report['checks'].append({'name':'shared_counter_lower_bound_preserved','result':result});save()
   result=scan(maximum=1);assert result['kind']=='Storage measurement entry limit exceeded' and result['entries']==2
   report['checks'].append({'name':'native_entry_bound_preserved','result':result});save()
   fifo=selected+'/users/'+str(user.id)+'/fifo'
   wsl('import os,sys; os.mkfifo(sys.argv[1])',fifo)
   try:
    result=scan();assert result['kind']=='Storage measurement encountered a special file'
    report['checks'].append({'name':'actual_fifo_refused_without_opening','result':result});save()
   finally:wsl('import os,sys; os.unlink(sys.argv[1])',fifo)
   request={'schema':inventory.protocol.SCHEMA,'nonce':uuid.uuid4().hex+uuid.uuid4().hex,'distribution':'vibeflow-linux',
    'root':selected,'user_id':user.id,'max_entries':200000,'stop_after':None,'seconds':.000001,
    'helpers':{name:hashlib.sha256(inventory.READER.with_name(name).read_bytes()).hexdigest() for name in inventory.protocol.FILES}}
   result=asyncio.run(inventory.read(request,time.monotonic()+30))
   assert result['status']=='deadline' and result['reader']['uid']==65534
   report['checks'].append({'name':'actual_native_deadline_returns_verified_reader_exit','result':result});save()
   request['helpers'][inventory.protocol.FILES[0]]='0'*64
   try:asyncio.run(inventory.read(request,time.monotonic()+30))
   except ValueError:pass
   else:raise AssertionError('Changed helper hash was accepted')
   report['checks'].append({'name':'changed_helper_hash_refused','kind':'protocol_fault'});save()
   assert scan()=={'kind':'complete','bytes':128+len('/absent-inventory-target'),'entries':2}
   report['passed']=True;save();return report
  except BaseException as error:
   report['failure_type']=type(error).__name__;save();raise
 return await asyncio.to_thread(checks)
'''

def verify(api,**_):
    import httpx
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=150,trust_env=False) as client:
        response=client.post('/test/native-inventory-faults');response.raise_for_status();report=response.json()
    assert report['passed'] and len(report['checks'])==9
    print('ACTUAL_NATIVE_INVENTORY_FAULTS_PASS '+str(api.root),flush=True)
    return report
