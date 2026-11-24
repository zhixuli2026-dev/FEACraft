"""Actual native runtime cancellation/cleanup, with hash-bound reviewed sources.

No model/provider/CLI inference is invoked or simulated. The fixture pauses real
native preparation and real close completion to make repeated cancellation and
an explicit post-close cleanup failure observable; native results are untouched.
"""
import argparse,ast,hashlib,json,sqlite3,sys,time,traceback
from pathlib import Path
from testsupport import IsolatedServer,SERVER

ROOT=SERVER.parents[1]
APP='''
import asyncio,gc,json,os
from pathlib import Path
from fastapi import Depends,HTTPException
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.services.native_request_execution import RequestExecutor
from app.services.runtime_probe_execution import python_probe
from app.services.process_identity_native import observe
root=Path.cwd();state={};unhandled=[]
original_prepare=RequestExecutor._prepare
original_close=RequestExecutor.close
original_run_sync=RequestExecutor.run_sync

async def run_sync(self,*args,**kwargs):
 self.acceptance_in_run_sync=True
 try:return await original_run_sync(self,*args,**kwargs)
 finally:self.acceptance_in_run_sync=False
RequestExecutor.run_sync=run_sync

async def prepare(self,entry,argv,env):
 result=await original_prepare(self,entry,argv,env)
 if self.kind=='runtime.python_probe' and state.get('mode')!='normal':
  state['request_id']=self.owner.id
  state['actual_prepare_ready'].set()
  await state['release_prepare'].wait()
 return result
RequestExecutor._prepare=prepare

async def close(self):
 result=await original_close(self)
 if (self.kind=='runtime.python_probe' and state.get('mode')!='normal'
     and getattr(self,'acceptance_in_run_sync',False) and not state.get('close_held')):
  state['close_held']=True;state['actual_close_finished'].set()
  await state['release_close'].wait()
  if state['mode']=='close-error':raise OSError('Explicit isolated error after actual native close')
 return result
RequestExecutor.close=close

@app.get('/api/v1/test/native-birth')
async def birth():return observe(os.getpid())

@app.post('/api/v1/test/native-cancel/start')
async def start(body:dict,user=Depends(get_current_user)):
 if user.id!=1:raise HTTPException(403)
 if state.get('task') is not None and not state['task'].done():raise HTTPException(409,'Original probe is still running')
 mode=body.get('mode')
 if mode not in {'normal','double-cancel','close-error'}:raise HTTPException(400)
 loop=asyncio.get_running_loop()
 if not state.get('handler_installed'):
  old_handler=loop.get_exception_handler()
  def handler(loop,context):
   unhandled.append({'message':context.get('message'),'exception':type(context.get('exception')).__name__})
   if old_handler is not None:old_handler(loop,context)
   else:loop.default_exception_handler(context)
  loop.set_exception_handler(handler)
 state.clear();state.update(mode=mode,handler_installed=True,actual_prepare_ready=asyncio.Event(),release_prepare=asyncio.Event(),actual_close_finished=asyncio.Event(),release_close=asyncio.Event(),baseline_unhandled=len(unhandled))
 state['task']=asyncio.create_task(python_probe(user.id))
 return {'started':True,'mode':mode}

@app.post('/api/v1/test/native-cancel/{operation}')
async def control(operation:str,user=Depends(get_current_user)):
 if user.id!=1:raise HTTPException(403)
 task=state.get('task')
 if task is None:raise HTTPException(409)
 if operation=='cancel':task.cancel()
 elif operation=='release-prepare':state['release_prepare'].set()
 elif operation=='release-close':state['release_close'].set()
 elif operation=='cleanup':
  state['release_prepare'].set();state['release_close'].set()
  if not task.done():task.cancel()
  await asyncio.gather(task,return_exceptions=True)
 else:raise HTTPException(400)
 return {'accepted':True,'task_done':task.done()}

@app.get('/api/v1/test/native-cancel/state')
async def inspect(user=Depends(get_current_user)):
 if user.id!=1:raise HTTPException(403)
 task=state.get('task');gc.collect();await asyncio.sleep(.02)
 value={'request_id':state.get('request_id'),'task_done':task.done() if task else False,
   'actual_prepare_ready':state.get('actual_prepare_ready',asyncio.Event()).is_set(),
   'actual_close_finished':state.get('actual_close_finished',asyncio.Event()).is_set(),
   'unhandled':unhandled[state.get('baseline_unhandled',0):]}
 if task and task.done():
  if task.cancelled():value['cancelled']=True
  else:
   try:value['result']=task.result()
   except BaseException as error:value['error']=type(error).__name__
 return value
'''

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def run():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--review',type=Path,required=True)
 parser.add_argument('--modes',default='double-cancel,close-error,normal')
 args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
 assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
 for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
 modes=args.modes.split(',');assert modes and set(modes)<={'double-cancel','close-error','normal'}
 api=IsolatedServer('native-request-cancel',app='native_cancel_fixture:app')
 (api.root/'native_cancel_fixture.py').write_text(APP,encoding='utf8')
 result={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'scenarios':{},'review_sha256':sha(args.review)}
 handle=None;entered=False
 def observe():return api.call('GET','/test/native-cancel/state')
 def until(predicate,seconds=20):
  end=time.monotonic()+seconds;value=None
  while True:
   value=observe()
   if predicate(value):return value
   if value['task_done']:raise RuntimeError('Actual owned probe became terminal before the expected observation boundary')
   if time.monotonic()>=end:
    result.setdefault('verified_waits',[]).append({'same_request_id':value.get('request_id'),'task_done':False})
    print('NATIVE_CANCEL_WAIT same actual probe is still running',flush=True)
    end=time.monotonic()+seconds
   time.sleep(.05)
 try:
  api.__enter__();entered=True
  from app.services.windows_process_scope import ProcessHandle
  result['API_birth']=api.call('GET','/test/native-birth');handle=ProcessHandle.query(result['API_birth'])
  api.create_case('Actual native request cancellation')
  for mode in modes:
   api.call('POST','/test/native-cancel/start',{'mode':mode})
   if mode!='normal':
    until(lambda x:x['actual_prepare_ready'])
    api.call('POST','/test/native-cancel/cancel',{})
    api.call('POST','/test/native-cancel/release-prepare',{})
    until(lambda x:x['actual_close_finished'])
    if mode=='double-cancel':api.call('POST','/test/native-cancel/cancel',{})
    api.call('POST','/test/native-cancel/release-close',{})
   value=until(lambda x:x['task_done'],seconds=75 if mode=='normal' else 30)
   # Collect only already terminal native work, never restart on an observation timeout.
   for _ in range(5):time.sleep(.05);value=observe()
   with sqlite3.connect('file:'+(api.root/'test.sqlite').as_posix()+'?mode=ro',uri=True) as db:
    rows=db.execute("SELECT id,phase,error,children_json FROM native_requests WHERE kind='runtime.python_probe' ORDER BY created_at").fetchall()
   native=rows[-1];children=json.loads(native[3])['children']
   proof=[{'phase':x['phase'],'stop':x.get('stop'),'record':x.get('record')} for x in children.values()]
   checked={'observed':value,'native_request_id':native[0],'phase':native[1],'error':native[2],'actual_children':proof,'all_actual_stops_verified':bool(proof) and all(x['stop'] and x['stop']['verified'] is True for x in proof)}
   checked['passed']=not value['unhandled'] and checked['all_actual_stops_verified'] and (bool((value.get('result')or{}).get('reachable')) if mode=='normal' else native[1] in {'failed','cancelled'})
   result['scenarios'][mode]=checked
  result['passed']=all(x['passed'] for x in result['scenarios'].values())
 except BaseException as error:
  result['failure']={'type':type(error).__name__,'message':str(error)[:700],'frames':[{'file':Path(x.filename).name,'line':x.lineno} for x in traceback.extract_tb(error.__traceback__)]}
 finally:
  if entered:
   try:api.call('POST','/test/native-cancel/cleanup',{})
   except Exception as error:result['cleanup_failure']=type(error).__name__
   api.__exit__(None if result['passed'] else RuntimeError,None,None)
  if handle is not None:handle.wait_exit(15);handle.close();result['API_exit_verified']=True
  result['passed']=result['passed'] and result.get('API_exit_verified') is True and 'cleanup_failure' not in result
  (api.root/'native-cancel-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
  print('NATIVE_CANCEL_RESULT '+str(api.root/'native-cancel-result.json'),flush=True)
  print(json.dumps({'passed':result['passed'],'scenarios':{k:{'passed':v['passed'],'unhandled':v['observed']['unhandled'],'stops_verified':v['all_actual_stops_verified']} for k,v in result['scenarios'].items()},'failure':result.get('failure'),'API_exit_verified':result.get('API_exit_verified')}),flush=True)
 return 0 if result['passed'] else 1

if __name__=='__main__':sys.exit(run())
