"""Observe real sandbox-export startup and inspect the still-live original volume.

Infrastructure only: no model provider, CLI inference or substituted replies.
The optional diagnostic is a fixed read-only native helper in a retained owned
process; it never executes the terminal command or changes the pinned volume.
"""
import sys
import browser_lifecycle_trace_acceptance as lifecycle

HOST_DIAGNOSTIC = '''import subprocess,sys
result=subprocess.run(['wsl.exe','-d',sys.argv[1],'--exec','python3','-I','-S','-u',*sys.argv[2:]],capture_output=True,timeout=30)
sys.stdout.buffer.write(result.stdout);sys.stdout.buffer.flush()
sys.stderr.buffer.write(result.stderr);sys.stderr.buffer.flush()
raise SystemExit(result.returncode)
'''

DIAGNOSTIC = '''import importlib.util,json,os,sys,traceback
from pathlib import Path
spec=importlib.util.spec_from_file_location('actual_export',sys.argv[2])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
value=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
result={'stages':[],'helper_birth':module.load_volume().identity_module().observe(os.getpid())}
volume=None
try:
 module.validate(value);result['stages'].append('original_settings_validated')
 identity=module.load_volume().identity_module()
 result['original_gate_observed']=identity.observe(value['gate']['pid'])
 todo=[value['gate']['pid']];seen=set();candidates=[];result['process_tree']=[]
 while todo:
  pid=todo.pop()
  if pid in seen:continue
  seen.add(pid)
  if len(seen)>128:raise ValueError('Diagnostic descendant bound exceeded')
  folder=Path('/proc')/str(pid)
  try:
   birth=identity.observe(pid)
   args=(folder/'cmdline').read_bytes().split(b'\\0')
   children=[int(x) for x in (folder/'task'/str(pid)/'children').read_text().split()]
   result['process_tree'].append({'birth':birth,'program_prefix':[Path(x.decode('utf8','replace')).name for x in args[:5]],
    'wchan':(folder/'wchan').read_text().strip(),'children':children,'has_tmp_case':(folder/'root/tmp/case').is_dir()})
   if (folder/'root/tmp/case').is_dir():
    with module.load_volume().Volume(birth,filesystem=value.get('filesystem','tmpfs')) as candidate:
     candidates.append({'birth':birth,'binding':candidate.binding})
   todo.extend(children)
  except (FileNotFoundError,ProcessLookupError,PermissionError):continue
 result['live_descendants_with_sandbox_volume']=candidates
 volume=module.load_volume().Volume(value['gate'],filesystem=value.get('filesystem','tmpfs'))
 result['stages'].append('original_live_volume_pinned')
 manifest,encoded=volume.snapshot()
 result['stages'].append('read_only_live_snapshot')
 result['manifest_bytes']=len(encoded);result['manifest_keys']=list(manifest)
 result['passed']=True
except Exception as failure:
 result['passed']=False
 result['failure']={'type':type(failure).__name__,'message':str(failure)[:800],
  'errno':getattr(failure,'errno',None),'frames':[{'file':Path(x.filename).name,'line':x.lineno,'function':x.name} for x in traceback.extract_tb(failure.__traceback__)]}
finally:
 if volume is not None:volume.close()
print(json.dumps(result,ensure_ascii=False),flush=True)
'''

TRACE = '''
import os,sys
from app.services.sandbox_export_execution import SandboxExport,NATIVE
from app.services.native_request_execution import RequestExecutor
from app.services.terminal_sessions import TerminalSession
from app.services import control_identity
from app.services.owned_process import OwnedProcess,wsl_path
from app.services.async_ops import finish_before_cancel
diagnostics={'controls':[],'read_only_checks':[],'pump_events':[],'original_namespace_probes':[]}
original_init=TerminalSession.__init__
def session_init(self,*args,**kwargs):
 original_init(self,*args,**kwargs)
 self.transport.owned.acceptance_terminal=True
TerminalSession.__init__=session_init
original_pump=TerminalSession._pump
async def pump(self):
 try:
  result=await original_pump(self)
  diagnostics['pump_events'].append({'session_id':self.id,'result':'returned','exit_code':self.exit_code,'tail':''.join(self._buf)[-8192:]})
  return result
 except BaseException as failure:
  diagnostics['pump_events'].append({'session_id':self.id,'failure':{'type':type(failure).__name__,'message':str(failure)[:500]},'tail':''.join(self._buf)[-8192:]})
  raise
 finally:(root/'sandbox-native-diagnostics.json').write_text(json.dumps(diagnostics,indent=2),encoding='utf8')
TerminalSession._pump=pump
original_prepare=RequestExecutor._prepare
async def prepare(self,entry,*args,**kwargs):
 if self.kind=='terminal.export':entry.owned.acceptance_export=True
 return await original_prepare(self,entry,*args,**kwargs)
RequestExecutor._prepare=prepare
original_control=OwnedProcess._read_control
async def read_control(self):
 try:return await original_control(self)
 finally:
  if getattr(self,'acceptance_export',False) or getattr(self,'acceptance_terminal',False):
   diagnostics['controls'].append({'kind':'terminal.export' if getattr(self,'acceptance_export',False) else 'terminal', 'actual_root':self.record,
    'stderr':bytes(self._control_data).decode('utf8','replace'),'overflow':self._control_overflow})
   (root/'sandbox-native-diagnostics.json').write_text(json.dumps(diagnostics,indent=2),encoding='utf8')
OwnedProcess._read_control=read_control
original_start=SandboxExport.start
async def start(self):
 try:return await original_start(self)
 except Exception:
  if self.selected is not None:
   probes={'session_id':self.selected['session_id']}
   for name,expected in [('gate',self.selected['gate']),('native_root',self.selected['terminal_process']['native']['process'])]:
    try:probes[name]=await control_identity.read(self.selected['distro'],'identity',{'expected':expected})
    except Exception as error:probes[name]={'unverified':type(error).__name__}
   diagnostics['original_namespace_probes'].append(probes)
   process=OwnedProcess();observation={'session_id':self.selected['session_id'],'read_only':True}
   try:
    arguments=[sys.executable,'-I','-S','-u',str(root/'sandbox-host-diagnostic.py'),self.selected['distro'],
     wsl_path(root/'sandbox-read-only-diagnostic.py'),wsl_path(self.root/'settings.json'),wsl_path(NATIVE)]
    await process.prepare(arguments,cwd=root,env=dict(os.environ),runtime='windows')
    observation['actual_root']=process.record
    await process.release()
    output=await asyncio.wait_for(process.proc.stdout.read(1024*1024+1),40)
    if len(output)>1024*1024:raise ValueError('Read-only diagnostic exceeded its output bound')
    await process.wait_root_exit(5)
    observation['result']=json.loads(output.decode('utf8'))
    observation['actual_native_helper_exit']=await control_identity.read(self.selected['distro'],'identity',{'expected':observation['result']['helper_birth']})
   except Exception as failure:
    observation['diagnostic_failure']={'type':type(failure).__name__,'message':str(failure)[:300]}
   finally:
    try:
     if process.proc is not None:await finish_before_cancel(process.stop(reason='root_exit',grace_ms=0))
     else:await finish_before_cancel(process.abort())
    finally:
     observation['actual_stop']=process.stop_result
     process.close()
     diagnostics['read_only_checks'].append(observation)
     (root/'sandbox-native-diagnostics.json').write_text(json.dumps(diagnostics,indent=2),encoding='utf8')
  raise
SandboxExport.start=start
@app.get('/api/v1/test/sandbox-native-diagnostics')
async def sandbox_diagnostics(user=Depends(get_current_user)):
 if user.id!=1:raise HTTPException(403)
 return diagnostics
'''

APP = (lifecycle.APP + "\n(root/'sandbox-read-only-diagnostic.py').write_text(" + repr(DIAGNOSTIC)
       + ",encoding='utf8')\n(root/'sandbox-host-diagnostic.py').write_text(" + repr(HOST_DIAGNOSTIC) + ",encoding='utf8')\n" + TRACE)


def run():
    lifecycle.controls.APP = APP
    return lifecycle.controls.run()


if __name__ == '__main__':
    sys.exit(run())
