"""Real short-lived members and fail-closed lifetime/retirement faults."""
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from app.services import owned_process as owned
from app.services import process_identity_native as identity
from app.services.process_stop import validate
from app.services.windows_process_scope import ProcessHandle
from owned_tree_smoke import TREE,capture,exited,wait_ready

SERVER=Path(__file__).resolve().parent
CHURN=r'''import json,os,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import process_identity_native as identity
signal.signal(signal.SIGBREAK,signal.SIG_IGN)
children=[]
with Path('births.jsonl').open('w',encoding='utf-8') as log:
 for i in range(int(sys.argv[2])):
  child=subprocess.Popen([sys.executable,'-I','-S','-c','pass'])
  children.append(child)
  log.write(json.dumps(identity.windows_identity(int(child._handle)))+'\n');log.flush()
  if i==0:Path('ready').touch()
  time.sleep(.005)
 for child in children:child.wait()
 children.clear();del child
Path('finished').touch()
for _ in range(2000):time.sleep(.01)
'''
EXIT_BEFORE_BIRTH=r'''import json,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import process_identity_native as identity
signal.signal(signal.SIGBREAK,signal.SIG_IGN)
child=subprocess.Popen([sys.executable,'-I','-S','-c','import sys;sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE)
birth=identity.windows_identity(int(child._handle))
Path('target-birth.json').write_text(json.dumps(birth))
while not Path('release-child').exists():time.sleep(.01)
child.stdin.close();child.wait(timeout=5)
try:identity.windows_identity(int(child._handle))
except ProcessLookupError:pass
else:raise AssertionError('Public identity accepted an exited process')
retained=identity.windows_identity(int(child._handle),allow_exited=True)
assert retained==birth
Path('retained-birth.json').write_text(json.dumps(retained))
Path('finished').touch()
for _ in range(2000):time.sleep(.01)
'''


async def until(predicate,timeout=10):
    async def wait():
        while not predicate(): await asyncio.sleep(.02)
    await asyncio.wait_for(wait(),timeout)


def births(work):
    records=[]
    for line in (work/'births.jsonl').read_text(encoding='utf-8').splitlines():
        try: records.append(json.loads(line))
        except json.JSONDecodeError: break  # A forced stop may interrupt the last line.
    return records


class FaultKernel:
    def __init__(self,real,kind,foreign_pid=None): self.real=real;self.kind=kind;self.foreign_pid=foreign_pid
    def __getattr__(self,name): return getattr(self.real,name)
    def OpenProcess(self,*args):
        if self.kind=='open': ctypes.set_last_error(5);return 0
        if self.kind=='foreign': return self.real.OpenProcess(args[0],args[1],self.foreign_pid)
        return self.real.OpenProcess(*args)
    def SetInformationJobObject(self,handle,kind,*args):
        if self.kind=='seal' and kind==9: ctypes.set_last_error(5);return 0
        return self.real.SetInformationJobObject(handle,kind,*args)
    def QueryInformationJobObject(self,handle,kind,*args):
        if self.kind=='accounting' and kind==1: ctypes.set_last_error(5);return 0
        return self.real.QueryInformationJobObject(handle,kind,*args)


async def main():
    root=Path(tempfile.mkdtemp(prefix='process-lifetime-',dir=SERVER.parents[1]/'tmp'))
    checks=[];runs=[]
    base=getattr(sys,'_base_executable',sys.executable)
    sentinel_code='import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'
    sentinels=[subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        for argv in ([base,'-I','-S','-u','-c',sentinel_code],
            ['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u','-c',sentinel_code])]
    assert all(child.stdout.readline().strip()==b'READY' for child in sentinels)
    try:
        for mode in ('exited_before_birth_read','churn_before_stop','churn_during_stop','delayed_notifications','missing_notification_with_duplicate',
                'observer_permission','observer_foreign_generation','seal_denied','member_open_denied','accounting_denied','member_wait_timeout','native_probe_unknown'):
            work=root/mode;work.mkdir();process=owned.OwnedProcess();drain=None
            saved_scope=saved_observer=None
            saved_contains=None
            saved_wait=ProcessHandle.wait_exit;saved_probe=owned.probe_native
            release_notifications=threading.Event()
            runtime='wsl' if mode=='native_probe_unknown' else 'windows'
            churn=mode.startswith('churn') or mode in {'delayed_notifications','missing_notification_with_duplicate'}
            birth_race=mode=='exited_before_birth_read'
            try:
                if birth_race:
                    (work/'birth-race.py').write_text(EXIT_BEFORE_BIRTH,encoding='utf-8')
                    argv=[base,'-I','-S','-u',str(work/'birth-race.py'),str(SERVER/'app/services')]
                elif churn:
                    (work/'churn.py').write_text(CHURN,encoding='utf-8')
                    argv=[base,'-I','-S','-u',str(work/'churn.py'),str(SERVER/'app/services'),
                        '600' if mode=='churn_during_stop' else '64']
                else:
                    (work/'tree.py').write_text(TREE,encoding='utf-8')
                    argv=[base,'-I','-S','-u',str(work/'tree.py'),'2','ignore'] if runtime=='windows' else [
                        'wsl.exe','-d','vibeflow-linux','--exec','bash','-lc',
                        'cd '+owned.wsl_path(work)+' && exec python3 -u tree.py 2 ignore']
                await process.prepare(argv,cwd=work,env=dict(os.environ),runtime=runtime)
                await until(lambda:process.scope.observer.summary()['observed']>=1)
                dropped=[]
                if birth_race:
                    saved_contains=process.scope.contains
                    def exiting_member(handle):
                        member=saved_contains(handle)
                        pid=process.scope.kernel.GetProcessId(handle.handle)
                        if member and pid!=process.proc.pid:
                            import time
                            deadline=time.monotonic()+5
                            while not (work/'target-birth.json').exists():
                                if time.monotonic()>=deadline:raise TimeoutError('Birth race fixture did not start')
                                time.sleep(.01)
                            target=json.loads((work/'target-birth.json').read_text())
                            if pid==target['pid']:
                                (work/'release-child').touch()
                                handle.wait_exit(5)
                        return member
                    process.scope.contains=exiting_member
                elif mode=='missing_notification_with_duplicate':
                    original=process.scope.observer._capture
                    def skip_one(pid):
                        if pid!=process.proc.pid and not dropped: dropped.append(pid);return
                        original(pid)
                    process.scope.observer._capture=skip_one
                elif mode=='delayed_notifications':
                    original=process.scope.observer._capture
                    def delayed(pid):
                        if not release_notifications.wait(15): raise TimeoutError('Fixture did not release notifications')
                        original(pid)
                    process.scope.observer._capture=delayed
                elif mode in {'observer_permission','observer_foreign_generation'}:
                    saved_observer=process.scope.observer.kernel
                    process.scope.observer.kernel=FaultKernel(saved_observer,
                        'open' if mode=='observer_permission' else 'foreign',sentinels[0].pid)
                await process.release()
                if birth_race:
                    drain=asyncio.create_task(process.proc.stdout.read())
                    await until(lambda:(work/'finished').exists())
                    records=[json.loads((work/'target-birth.json').read_text())]
                    assert json.loads((work/'retained-birth.json').read_text())==records[0]
                elif churn:
                    drain=asyncio.create_task(process.proc.stdout.read())
                    await until(lambda:(work/'ready').exists())
                    if mode!='churn_during_stop':
                        await until(lambda:(work/'finished').exists())
                        records=births(work)
                        assert len(records)==64
                        await exited(records,'windows')
                        if mode=='delayed_notifications':
                            await asyncio.sleep(.15)
                            release_notifications.set()
                    else: await asyncio.sleep(.15)
                else:
                    await wait_ready(process,work)
                    records=await capture(process,work,runtime)
                    drain=asyncio.create_task(process.proc.stdout.read())
                if mode=='missing_notification_with_duplicate':
                    assert dropped
                    kernel=process.scope.observer.kernel
                    kernel.PostQueuedCompletionStatus.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_size_t,ctypes.c_void_p]
                    kernel.PostQueuedCompletionStatus.restype=wintypes.BOOL
                    assert kernel.PostQueuedCompletionStatus(process.scope.observer.port,6,1,process.proc.pid)
                elif mode in {'seal_denied','member_open_denied','accounting_denied'}:
                    saved_scope=process.scope.kernel
                    process.scope.kernel=FaultKernel(saved_scope,{'seal_denied':'seal','member_open_denied':'open','accounting_denied':'accounting'}[mode])
                elif mode=='member_wait_timeout':
                    def timed_out(handle,timeout=5):
                        if timeout>0: raise TimeoutError('Injected retained member wait failure')
                        return saved_wait(handle,timeout)
                    ProcessHandle.wait_exit=timed_out
                elif mode=='native_probe_unknown':
                    async def unknown(*args): return {'state':'unverified','reason':'injected_probe_failure'}
                    owned.probe_native=unknown
                should_fail=not (mode.startswith('churn') or mode=='delayed_notifications' or birth_race)
                try:
                    result=await process.stop(grace_ms=250)
                    assert not should_fail,(mode,result)
                except (OSError,RuntimeError,TimeoutError):
                    assert should_fail,mode
                    result=process.stop_result
                    assert not result['verified'] and result['error'],result
                finally:
                    ProcessHandle.wait_exit=saved_wait
                    owned.probe_native=saved_probe
                if saved_scope is not None: process.scope.kernel=saved_scope
                if saved_observer is not None: process.scope.observer.kernel=saved_observer
                await asyncio.wait_for(drain,5)
                if churn: records=births(work)
                for _ in range(100):
                    try: await exited(records,runtime);break
                    except AssertionError: await asyncio.sleep(.05)
                else: raise AssertionError('Stop fault left an observed descendant alive')
                validate(result,process.record)
                observation=result['scope_observation']
                if not should_fail:
                    assert result['verified'] and observation['complete'] and observation['pending']==0,result
                    assert observation['total']==observation['observed']>=len(records)+2,observation
                    if mode=='churn_during_stop': assert not (work/'finished').exists()
                    if mode=='delayed_notifications': assert observation['gone_before_open']>0,observation
                    if birth_race:
                        assert (records[0]['pid'],records[0]['creation_time_100ns']) in process.scope.observer.seen
                        assert observation['exit_verified']==3 and observation['gone_before_open']==0,observation
                elif mode=='missing_notification_with_duplicate':
                    assert observation['total']==observation['observed']+1,observation
                    assert not observation['complete']
                assert all(child.poll() is None for child in sentinels)
                checks.append(mode);runs.append({'mode':mode,'stop':result,'observed_births':records})
                (root/'progress.json').write_text(json.dumps({'checks':checks,'runs':runs},indent=2)+'\n',encoding='utf-8')
                print('PASS '+mode,flush=True)
            finally:
                release_notifications.set()
                ProcessHandle.wait_exit=saved_wait;owned.probe_native=saved_probe
                if saved_scope is not None: process.scope.kernel=saved_scope
                if saved_observer is not None: process.scope.observer.kernel=saved_observer
                if saved_contains is not None:process.scope.contains=saved_contains
                if drain is not None and not drain.done():
                    drain.cancel();await asyncio.gather(drain,return_exceptions=True)
                await process.abort()
    finally:
        for child in sentinels: child.communicate(b'x',timeout=10)
    report={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(root)}
    (root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('PROCESS_LIFETIME_OK '+json.dumps({'count':len(checks),'evidence':str(root)}))


if __name__=='__main__': asyncio.run(main())
