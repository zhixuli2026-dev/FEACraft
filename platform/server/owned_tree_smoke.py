"""Actual owned Windows/WSL descendants, graceful stop, escalation and owner death."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SERVER=Path(__file__).resolve().parent
from app.services import owned_process as owned
from app.services import process_identity_native as identity

TREE=r'''import json,os,signal,subprocess,sys,time
from pathlib import Path
level=int(sys.argv[1]);mode=sys.argv[2]
root=Path.cwd()
if os.name=='nt': target=signal.SIGBREAK
else: target=signal.SIGTERM
def stop(*_):
 (root/('stopped-'+str(level))).touch()
 raise SystemExit(0)
signal.signal(target,signal.SIG_IGN if mode=='ignore' else stop)
if level:
 options={}
 if mode=='detach':
  if os.name=='nt': options['creationflags']=subprocess.CREATE_NEW_PROCESS_GROUP
  else: options['start_new_session']=True
 subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve()),str(level-1),mode],**options)
(root/('node-'+str(level))).write_text(str(os.getpid()))
if level==2:
 for _ in range(500):
  if all((root/('node-'+str(n))).exists() for n in range(3)): break
  time.sleep(.01)
 else: raise RuntimeError('Missing test descendant')
 print('TREE_READY',flush=True)
 print('USER_STDERR_NOT_CONTROL',file=sys.stderr,flush=True)
 if mode=='root_exit':
  for _ in range(2000):
   if (root/'release-root').exists(): break
   time.sleep(.01)
  else: raise RuntimeError('Root exit not released')
  raise SystemExit(0)
for _ in range(2000):
 (root/('heartbeat-'+str(level))).write_text(str(time.monotonic()))
 time.sleep(.03)
raise SystemExit(125)
'''

WALK=r'''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import process_identity_native as identity
request=json.load(sys.stdin)
if 'probe' in request:
 print(json.dumps([identity.probe(value) for value in request['probe']]))
else:
 root=request['root']
 assert identity.probe(root)['state']=='match'
 pending=[root['pid']];seen=set();result=[]
 while pending:
  pid=pending.pop()
  if pid in seen: continue
  assert len(seen)<64
  seen.add(pid)
  try:
   result.append(identity.observe(pid))
   pending.extend(map(int,Path('/proc/'+str(pid)+'/task/'+str(pid)+'/children').read_text().split()))
  except (FileNotFoundError,ProcessLookupError): pass
 print(json.dumps(result))
'''


def native_records(request):
    result=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',WALK,
        owned.wsl_path(SERVER/'app/services')],input=json.dumps(request).encode(),capture_output=True,timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    return json.loads(result.stdout)


async def wait_ready(process,root):
    lines=[]
    for _ in range(20):
        line=await asyncio.wait_for(process.proc.stdout.readline(),timeout=10)
        lines.append(line)
        if line.strip()==b'TREE_READY': break
        assert line,b''.join(lines)
    else: raise AssertionError('No tree readiness marker')
    assert all((root/('node-'+str(n))).exists() for n in range(3))
    return b''.join(lines)


async def capture(process,root,runtime):
    if runtime=='windows':
        return [identity.observe(int((root/('node-'+str(n))).read_text())) for n in range(3)]
    return await asyncio.to_thread(native_records,{'root':process.record['native']['process']})


async def exited(records,runtime):
    values=[identity.probe(value) for value in records] if runtime=='windows' else await asyncio.to_thread(native_records,{'probe':records})
    assert all(value['state']=='exited' for value in values),values


async def owner_child(root,runtime):
    process=owned.OwnedProcess()
    argv=[sys.executable,'-u',str(root/'tree.py'),'2','ignore'] if runtime=='windows' else [
        'wsl.exe','-d','vibeflow-linux','--exec','python3','-u',owned.wsl_path(root/'tree.py'),'2','ignore']
    await process.prepare(argv,cwd=root,env=dict(os.environ),runtime=runtime)
    await process.release()
    await wait_ready(process,root)
    (root/'owner.json').write_text(json.dumps({'owner':identity.observe(os.getpid()),'record':process.record}))
    await asyncio.sleep(60)
    await process.abort()


async def main():
    root=Path(tempfile.mkdtemp(prefix='owned-tree-evidence-',dir=SERVER.parents[1]/'tmp'))
    checks=[];reports=[]
    def done(name): checks.append(name);print('PASS '+name,flush=True)
    base=getattr(sys,'_base_executable',sys.executable)
    sentinel=subprocess.Popen([base,'-I','-S','-u','-c','import sys; print("READY",flush=True); sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    assert sentinel.stdout.readline().strip()==b'READY'
    native_sentinel=subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u','-c',
        'import sys; print("READY",flush=True); sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert native_sentinel.stdout.readline().strip()==b'READY'
    try:
        for runtime in ('windows','wsl'):
            for mode in ('cooperate','ignore','detach','root_exit'):
                work=root/(runtime+'-'+mode);work.mkdir()
                (work/'tree.py').write_text(TREE,encoding='utf-8')
                process=owned.OwnedProcess()
                drain=None
                argv=[sys.executable,'-u',str(work/'tree.py'),'2',mode] if runtime=='windows' else [
                    'wsl.exe','-d','vibeflow-linux','--exec','bash','-lc',
                    'cd '+owned.wsl_path(work)+' && exec python3 -u tree.py 2 '+mode]
                try:
                    await process.prepare(argv,cwd=work,env=dict(os.environ),runtime=runtime)
                    await process.release()
                    await wait_ready(process,work)
                    records=await capture(process,work,runtime)
                    assert len(records)>=3
                    drain=asyncio.create_task(process.proc.stdout.read())
                    if mode=='root_exit':
                        (work/'release-root').touch()
                        await process.wait_root_exit(5)
                        result=await process.finish()
                    else:
                        results=await asyncio.gather(*[process.stop(grace_ms=600) for _ in range(3)])
                        assert all(value==results[0] for value in results)
                        result=results[0]
                    output=await asyncio.wait_for(drain,timeout=5)
                    assert result['verified'] and result['scope_empty'] and result['control_valid'],result
                    assert b'USER_STDERR_NOT_CONTROL' in output,output
                    if mode=='ignore': assert result['forced'],result
                    if mode=='cooperate':
                        assert (work/'stopped-2').exists()
                        assert not result['forced'],result
                    await exited(records,runtime)
                    assert sentinel.poll() is None and native_sentinel.poll() is None
                    reports.append({'runtime':runtime,'mode':mode,'stop':result,'descendants':records})
                    done(runtime+'_'+mode+'_all_observed_descendants_exit_and_unrelated_sentinels_survive')
                finally:
                    if drain is not None and not drain.done():
                        drain.cancel();await asyncio.gather(drain,return_exceptions=True)
                    await process.abort()
            # Kill the actual owner interpreter, not a venv redirector. The
            # owner alone holds its scopes; no surrounding test Job kills them.
            work=root/(runtime+'-owner-death');work.mkdir()
            (work/'tree.py').write_text(TREE,encoding='utf-8')
            # Pure stdlib child: -I/-S plus explicit repository app import path.
            code='import runpy,sys; sys.path.insert(0,'+repr(str(SERVER))+'); sys.argv='+repr([
                str(Path(__file__).resolve()),'--owner',str(work),runtime])+'; runpy.run_path(sys.argv[0],run_name="__main__")'
            child=subprocess.Popen([base,'-I','-S','-u','-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                for _ in range(1000):
                    if (work/'owner.json').exists(): break
                    assert child.poll() is None,child.communicate()[1].decode('utf-8','replace')
                    await asyncio.sleep(.02)
                else: raise TimeoutError('Owner did not become ready')
                snapshot=json.loads((work/'owner.json').read_text())
                assert snapshot['owner']==identity.observe(child.pid)
                records=[identity.observe(int((work/('node-'+str(n))).read_text())) for n in range(3)] if runtime=='windows' else await asyncio.to_thread(native_records,{'root':snapshot['record']['native']['process']})
                child.kill();await asyncio.to_thread(child.communicate,timeout=10)
                for _ in range(60):
                    try: await exited(records,runtime);break
                    except AssertionError: await asyncio.sleep(.1)
                else: raise AssertionError('Owner death left an observed descendant alive')
                assert sentinel.poll() is None and native_sentinel.poll() is None
                done(runtime+'_abrupt_actual_owner_death_retires_only_its_tree')
            finally:
                if child.poll() is None: child.kill()
                output,error=child.communicate(timeout=10)
                (work/'owner.log').write_bytes(output+b'\n'+error)
    finally:
        for child in (sentinel,native_sentinel):
            child.communicate(b'x',timeout=10)
    report={'passed':True,'count':len(checks),'checks':checks,'runs':reports,'evidence':str(root)}
    (root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('OWNED_TREE_OK '+json.dumps({'count':len(checks),'evidence':str(root)}))


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--owner': asyncio.run(owner_child(Path(sys.argv[2]),sys.argv[3]))
    else: asyncio.run(main())
