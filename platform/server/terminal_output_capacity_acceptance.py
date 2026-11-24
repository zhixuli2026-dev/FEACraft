"""Real cmd/WSL output under a blocked websocket send; no model inference."""
import argparse,asyncio,hashlib,json,shlex,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
import websockets
from testsupport import IsolatedServer,SERVER
import testsupport
from terminal_shutdown_acceptance import RUNNER
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native

ROOT=SERVER.parents[1]
FIXTURE='''
import asyncio,json,os
from pathlib import Path
from fastapi import Depends,HTTPException
from starlette.websockets import WebSocket
from app.main import app
from app.deps import get_current_user
from app.services.terminal_sessions import TerminalSession
from app.services.process_identity_native import observe
root=Path(__file__).parent;observed={}
subscribe=TerminalSession.subscribe
def subscribed(self):
 q,snap=subscribe(self);sample={'queue':q,'session':self,'peak_bytes':0,'peak_items':0,'queued_bytes':0}
 observed.setdefault(self.id,[]).append(sample);put=q._put;get=q._get
 def record(value):
  put(value)
  sample['queued_bytes']+=len(value.encode('utf8')) if isinstance(value,str) else 0
  sample['peak_bytes']=max(sample['peak_bytes'],sample['queued_bytes'])
  sample['peak_items']=max(sample['peak_items'],q.qsize())
 def removed():
  value=get();sample['queued_bytes']-=len(value.encode('utf8')) if isinstance(value,str) else 0;return value
 q._put=record;q._get=removed
 return q,snap
TerminalSession.subscribe=subscribed
send=WebSocket.send_bytes
async def sending(self,data):
 selected=self.query_params.get('acceptance_hold')
 if selected in {'cmd','wsl'}:
  (root/('blocked-'+selected)).touch()
  while not (root/('release-'+selected)).exists():await asyncio.sleep(.01)
 return await send(self,data)
WebSocket.send_bytes=sending
@app.on_event('startup')
async def birth(): (root/'api-birth.json').write_text(json.dumps(observe(os.getpid())))
@app.get('/fixture/output/{sid}')
async def sample(sid:str,user=Depends(get_current_user)):
 rows=observed.get(sid)
 if not rows or rows[0]['session'].user_id!=user.id:raise HTTPException(404)
 return [{'peak_bytes':r['peak_bytes'],'peak_items':r['peak_items'],'queue_bytes':sum(len(x.encode('utf8')) for x in r['queue']._queue if isinstance(x,str)),
  'queue_items':r['queue'].qsize(),'overflow':bool(getattr(r['queue'],'overflow',None) and r['queue'].overflow.is_set()),
  'tail_bytes':len(''.join(r['session']._buf).encode('utf8')),'attached':r['queue'] in r['session']._subscribers} for r in rows]
'''

async def verify(require_bounded):
 api=IsolatedServer('terminal-output-capacity',app='output_fixture:app')
 (api.root/'output_fixture.py').write_text(FIXTURE,'utf8')
 (api.root/'graceful_server.py').write_text(RUNNER,'utf8')
 original=subprocess.Popen
 def launch(argv,**kwargs):
  assert argv[1:3]==['-m','uvicorn']
  return original([argv[0],'-X','utf8','-B',str(api.root/'graceful_server.py'),*argv[3:]],**kwargs)
 result={'passed':False,'model_calls':0,'mock_llm_calls':0,'shells':[],'observation_only':not require_bounded};active=[]
 def save():(api.root/'output-result.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
 async def call(method,path,*args,**kwargs):return await asyncio.to_thread(api.call,method,path,*args,**kwargs)
 async def until(predicate,label,seconds=45):
  end=time.monotonic()+seconds
  while not predicate():
   assert time.monotonic()<end,label
   await asyncio.sleep(.05)
 async def ready(ws):
  while True:
   msg=await asyncio.wait_for(ws.recv(),60)
   if isinstance(msg,str):
    v=json.loads(msg);assert v.get('type')!='error',v
    if v.get('type')=='ready':return v
 try:
  testsupport.subprocess.Popen=launch
  try:await asyncio.to_thread(api.__enter__)
  finally:testsupport.subprocess.Popen=original
  case=await asyncio.to_thread(api.create_case,'Actual terminal output capacity')
  with sqlite3.connect(api.root/'test.sqlite') as db:prefix=db.execute('SELECT s3_prefix FROM cases WHERE id=?',(case['id'],)).fetchone()[0]
  directory=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/prefix
  for shell in ['cmd','wsl']:
   nonce=uuid.uuid4().hex;marker='OUTPUT_DONE_'+nonce;script='Ref/output-'+shell+'.py'
   code="import sys\nfrom pathlib import Path\nfor n in range(1024):\n sys.stdout.write('Q'*4086+'汉字🙂');sys.stdout.flush()\nsys.stdout.write('\\n"+marker+"\\n');sys.stdout.flush()\nPath('Ref/produced-"+shell+".txt').write_text('"+nonce+"')\n"
   await call('PUT',api.prefix+'/file',{'path':script,'content':code})
   base=api.base.replace('http:','ws:')+'/ws/terminal?token='+api.token+'&shell='+shell+'&case='+str(case['id'])
   async with websockets.connect(base+'&acceptance_hold='+shell,proxy=None) as slow:
    sid=(await ready(slow))['session'];active.append(sid)
    await until(lambda:(api.root/('blocked-'+shell)).exists(),'Actual websocket send never blocked')
    seen={'bytes':0,'marker':False,'tail':b'','errors':[]};reader=None
    async with websockets.connect(base+'&session='+sid,proxy=None) as fast:
     await ready(fast)
     async def read_fast():
      try:
       async for message in fast:
        if isinstance(message,bytes):
         seen['bytes']+=len(message);seen['tail']=(seen['tail']+message)[-512:]
         if marker.encode() in seen['tail']:seen['marker']=True
        else:
         value=json.loads(message)
         if value.get('type')=='error':seen['errors'].append(value)
      except websockets.ConnectionClosed:pass
     reader=asyncio.create_task(read_fast())
     try:
      command='"'+sys.executable+'" -X utf8 -B '+script+'\r' if shell=='cmd' else 'python3 '+shlex.quote(script)+'\r'
      await fast.send(json.dumps({'type':'input','data':command}))
      await until(lambda:(directory/('Ref/produced-'+shell+'.txt')).exists(),'Real PTY did not finish output',60)
      assert (directory/('Ref/produced-'+shell+'.txt')).read_text()==nonce
      try:await until(lambda:seen['marker'] or seen['errors'],'Healthy consumer did not receive actual output')
      except BaseException:
       result['output_failure_observation']={'shell':shell,'healthy_bytes':seen['bytes'],'healthy_tail':seen['tail'].decode('utf8','replace'),'healthy_errors':seen['errors'],
        'buffers':await call('GET','/fixture/output/'+sid,api_prefix=False)};save();raise
      sample=await call('GET','/fixture/output/'+sid,api_prefix=False)
      entry={'shell':shell,'id':sid,'buffers':sample,'healthy_bytes':seen['bytes'],'healthy_errors':seen['errors'],'produced_nonce':nonce};result['shells'].append(entry);save()
      assert seen['marker'] and not seen['errors'] and seen['bytes']>=4*1024*1024
      current=await call('GET','/terminal/sessions/'+sid);entry['before_close']=current
      assert current['phase']=='active' and current['stop'] is None
      if shell=='wsl':assert current['process']['root']['native']['process']['uid']==65534
      if require_bounded:
       assert sample[0]['overflow'] and sample[0]['peak_bytes']<=1024*1024 and sample[0]['peak_items']<=256
       assert all(v['tail_bytes']<=256*1024 for v in sample)
       messages=[]
       try:
        while True:
         message=await asyncio.wait_for(slow.recv(),10)
         if isinstance(message,str):messages.append(json.loads(message))
       except websockets.ConnectionClosed as closed:
        assert closed.rcvd and closed.rcvd.code==1013
       assert any(v.get('code')=='terminal_output_limit' for v in messages);entry['slow_messages']=messages
       assert identity.probe(current['process']['root']['host'])['state']=='match'
       async with websockets.connect(base+'&session='+sid,proxy=None) as resumed:
        assert (await ready(resumed))['session']==sid
        snapshot=await asyncio.wait_for(resumed.recv(),10);assert isinstance(snapshot,bytes) and len(snapshot)<=256*1024 and marker.encode() in snapshot
        assert '汉字🙂' in snapshot.decode('utf8')
        resume_command='echo '+nonce+'>Ref/resumed-'+shell+'.txt\r' if shell=='cmd' else "printf '%s' '"+nonce+"' > Ref/resumed-"+shell+'.txt\r'
        await resumed.send(json.dumps({'type':'input','data':resume_command}))
        await until(lambda:(directory/('Ref/resumed-'+shell+'.txt')).exists(),'Original terminal did not continue after reconnect')
        assert (directory/('Ref/resumed-'+shell+'.txt')).read_text().strip()==nonce
       entry['original_session_reconnected']=True
       async with websockets.connect(base+'&session='+sid+'&acceptance_hold='+shell,proxy=None) as replay:
        assert (await ready(replay))['session']==sid
        messages=[];started=time.monotonic()
        try:
         while True:
          message=await asyncio.wait_for(replay.recv(),15)
          if isinstance(message,str):messages.append(json.loads(message))
        except websockets.ConnectionClosed as stopped:
         assert stopped.rcvd and stopped.rcvd.code==1013
        assert any(v.get('code')=='terminal_output_limit' for v in messages)
        sample=(await call('GET','/fixture/output/'+sid,api_prefix=False))[-1]
        assert sample['overflow'] and not sample['attached'] and sample['peak_bytes']<1024*1024
        entry['snapshot_send_timeout']={'elapsed':time.monotonic()-started,'messages':messages,'buffer':sample}
       assert (await call('GET','/terminal/sessions/'+sid))['phase']=='active'
      else:
       assert sample[0]['peak_bytes']>1024*1024;entry['unbounded_output_reproduced']=True
       (api.root/('release-'+shell)).touch()
      assert (await call('DELETE','/terminal/sessions/'+sid))['ok'];active.remove(sid)
      closed=await call('GET','/terminal/sessions/'+sid);entry['closed']=closed;assert closed['phase']=='closed' and closed['cleanup_verified']
      births={}
      def collect(v):
       if isinstance(v,dict):
        if v.get('schema')==identity.SCHEMA:births[json.dumps(v,sort_keys=True)]=v
        for child in v.values():collect(child)
       elif isinstance(v,list):
        for child in v:collect(child)
      collect(current['process']);entry['independent_exits']=[]
      for birth in births.values():
       observed=await probe_native('vibeflow-linux',birth) if birth['platform']=='linux' else identity.probe(birth)
       assert observed['state']=='exited';entry['independent_exits'].append({'identity':birth,'observed':observed})
      save()
     finally:
      if reader:reader.cancel();await asyncio.gather(reader,return_exceptions=True)
   print('ACTUAL_OUTPUT_'+shell.upper()+'_DONE',flush=True)
  result['passed']=require_bounded;result['reproduced']=not require_bounded
 except BaseException as error:
  result['failure_type']=type(error).__name__;raise
 finally:
  testsupport.subprocess.Popen=original
  for shell in ['cmd','wsl']:(api.root/('release-'+shell)).touch()
  for sid in active:
   try:await call('DELETE','/terminal/sessions/'+sid)
   except Exception as error:result.setdefault('cleanup_errors',[]).append(type(error).__name__)
  if api.proc is not None:
   (api.root/'graceful-stop').touch();await asyncio.to_thread(api.proc.wait,60)
   await asyncio.to_thread(api.__exit__,None,None,None)
   birth=json.loads((api.root/'api-birth.json').read_text('utf8'));result['api_exit']={'identity':birth,'observed':identity.probe(birth)}
   assert api.proc.returncode==0 and result['api_exit']['observed']['state']=='exited'
  save()
 print(json.dumps({'passed':result['passed'],'reproduced':result.get('reproduced',False),'model_calls':0,'evidence':str(api.root)}),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--review',type=Path,required=True);p.add_argument('--require-bounded',action='store_true');a=p.parse_args()
 review=json.loads(a.review.read_text('utf8'));assert review['entry']=='platform/server/terminal_output_capacity_acceptance.py'
 for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
 asyncio.run(verify(a.require_bounded))
