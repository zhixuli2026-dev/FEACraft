"""Stop-time forks, independent consoles, slow cleanup and owner-call cancellation."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from app.services import owned_process as owned
from app.services import process_identity_native as identity
from owned_tree_smoke import TREE, capture, exited, native_records, wait_ready

SERVER=Path(__file__).resolve().parent
RACE=r'''import os,signal,subprocess,sys,time
from pathlib import Path
root=Path.cwd();label=sys.argv[1];mode=sys.argv[2]
stopping=False;child=None
def received(*_):
 global stopping
 stopping=True
signal.signal(signal.SIGBREAK if os.name=='nt' else signal.SIGTERM,received)
def spawn(label,detached=False):
 options={}
 if detached:
  if os.name=='nt':
   startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=subprocess.SW_HIDE
   options.update(creationflags=subprocess.CREATE_NEW_CONSOLE,startupinfo=startup)
  else: options['start_new_session']=True
 return subprocess.Popen([sys.executable,'-I','-S','-u',str(Path(__file__).resolve()),label,mode],**options)
def ready(label):
 for _ in range(1000):
  if (root/('node-'+label)).exists(): return
  time.sleep(.01)
 raise TimeoutError('Missing fixture child')
if label=='root':
 child=spawn('leaf',mode=='new_console');ready('leaf')
temp=root/('node-'+label+'.tmp');temp.write_text(str(os.getpid()));temp.replace(root/('node-'+label))
if label=='root': print('RACE_READY',flush=True)
for _ in range(3000):
 if stopping:
  (root/('received-'+label)).touch()
  if mode=='slow':
   time.sleep(.35)
   if child is not None: child.wait(timeout=5)
   (root/('cleaned-'+label)).touch()
   raise SystemExit(0)
  if mode=='new_console' and label=='root': raise SystemExit(0)
  if mode=='late_fork' and label=='root' and not (root/'late-ready').exists():
   spawn('late',True);ready('late');(root/'late-ready').touch()
 time.sleep(.02)
raise SystemExit(125)
'''

KILL_NATIVE=r'''import json,os,signal,sys
sys.path.insert(0,sys.argv[1])
import process_identity_native as identity
expected=json.load(sys.stdin)
assert identity.probe(expected)['state']=='match'
fd=os.pidfd_open(expected['pid'])
try:
 assert identity.probe(expected)['state']=='match'
 signal.pidfd_send_signal(fd,signal.SIGKILL)
finally: os.close(fd)
print('OWNED_NATIVE_SUPERVISOR_KILLED')
'''

DETAILS=r'''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import process_identity_native as identity
records=json.load(sys.stdin)
result=[]
for item in records:
 assert identity.probe(item)['state']=='match'
 result.append({'identity':item,'comm':Path('/proc/'+str(item['pid'])+'/comm').read_text().strip()})
print(json.dumps(result))
'''


def native_helper(code,request):
    result=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',code,
        owned.wsl_path(SERVER/'app/services')],input=json.dumps(request).encode(),capture_output=True,
        timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    return result.stdout


async def marker(path,timeout=5):
    async def wait():
        while not path.exists(): await asyncio.sleep(.02)
    await asyncio.wait_for(wait(),timeout)


def arguments(work,runtime,file,*args):
    if runtime=='windows': return [sys.executable,'-u',str(work/file),*args]
    command='cd '+shlex.quote(owned.wsl_path(work))+' && exec '+shlex.join(['python3','-u',file,*args])
    return ['wsl.exe','-d','vibeflow-linux','--exec','bash','-lc',command]


async def race_records(process,work,runtime):
    if runtime=='wsl': return await asyncio.to_thread(native_records,{'root':process.record['native']['process']})
    return [identity.observe(int(path.read_text())) for path in work.glob('node-*') if path.suffix!='.tmp']


async def main():
    root=Path(tempfile.mkdtemp(prefix='owned-tree-races-',dir=SERVER.parents[1]/'tmp'))
    checks=[];runs=[]
    base=getattr(sys,'_base_executable',sys.executable)
    sentinel_code='import sys; print("READY",flush=True); sys.stdin.buffer.read(1)'
    sentinels=[subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        for argv in ([base,'-I','-S','-u','-c',sentinel_code],
            ['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u','-c',sentinel_code])]
    assert all(child.stdout.readline().strip()==b'READY' for child in sentinels)
    def done(name,process,records,extra=None):
        assert all(child.poll() is None for child in sentinels)
        checks.append(name)
        runs.append({'name':name,'stop':process.stop_result,'observed':records,'extra':extra})
        (root/'progress.json').write_text(json.dumps({'completed':checks,'runs':runs},indent=2)+'\n',encoding='utf-8')
        print('PASS '+name,flush=True)
    try:
        for runtime in ('windows','wsl'):
            modes=['late_fork','slow','new_console','caller_cancel','control_eof','control_invalid']
            if runtime=='windows': modes.append('member_query_denied')
            for mode in modes:
                work=root/(runtime+'-'+mode);work.mkdir()
                process=owned.OwnedProcess();drain=None;operation=None
                race=mode in {'late_fork','slow','new_console'}
                filename='race.py' if race else 'tree.py'
                (work/filename).write_text(RACE if race else TREE,encoding='utf-8')
                try:
                    await process.prepare(arguments(work,runtime,filename,*(['root',mode] if race else ['2','ignore'])),
                        cwd=work,env=dict(os.environ),runtime=runtime)
                    await process.release()
                    if race:
                        assert (await asyncio.wait_for(process.proc.stdout.readline(),10)).strip()==b'RACE_READY'
                        records=await race_records(process,work,runtime)
                    else:
                        await wait_ready(process,work)
                        records=await capture(process,work,runtime)
                    drain=asyncio.create_task(process.proc.stdout.read())
                    if mode=='member_query_denied':
                        import ctypes
                        kernel=process.scope.kernel
                        class QueryDenied:
                            def __getattr__(self,name): return getattr(kernel,name)
                            def QueryInformationJobObject(self,handle,kind,*args):
                                if kind==3:
                                    ctypes.set_last_error(5)
                                    return 0
                                return kernel.QueryInformationJobObject(handle,kind,*args)
                        process.scope.kernel=QueryDenied()
                        try:
                            try: await process.stop(grace_ms=250)
                            except OSError: pass
                            else: raise AssertionError('Failed member observation was treated as verified')
                        finally: process.scope.kernel=kernel
                        result=process.stop_result
                        assert not result['verified'] and not result['scope_empty'] and result['error'],result
                        # TerminateJobObject is asynchronous. This failure must
                        # stay unverified while its fallback retires our members.
                        for _ in range(100):
                            try: await exited(records,runtime);break
                            except AssertionError: await asyncio.sleep(.05)
                        else: raise AssertionError('Observation failure left a known descendant alive')
                        await asyncio.wait_for(drain,5)
                        done(runtime+'_'+mode,process,records)
                        continue
                    if mode in {'control_eof','control_invalid'}:
                        if mode=='control_eof': process.proc.stdin.close()
                        else:
                            process.proc.stdin.write(b'{"action":"stop","nonce":"wrong","grace_ms":999999}\n')
                            await process.proc.stdin.drain()
                        await process.wait_root_exit(10)
                        result=await process.finish()
                        assert any(item['stop_received'] for item in result['control']),result
                    else:
                        operation=asyncio.create_task(process.stop(grace_ms=3000 if mode=='late_fork' else 1500))
                        if mode=='late_fork':
                            await marker(work/'late-ready')
                            late_records=await race_records(process,work,runtime)
                            assert len(late_records)>len(records),(records,late_records)
                            records=list({json.dumps(item,sort_keys=True):item for item in records+late_records}.values())
                        elif mode=='caller_cancel':
                            while not process.stop_result or not process.stop_result['request_sent']: await asyncio.sleep(.01)
                            operation.cancel();await asyncio.sleep(.05);operation.cancel()
                            assert not operation.done(),'Caller cancellation escaped before cleanup'
                            try: await operation
                            except asyncio.CancelledError: pass
                            else: raise AssertionError('Caller cancellation was swallowed')
                        if mode=='caller_cancel': result=process.stop_result
                        else: result=await operation
                    assert result['verified'] and result['scope_empty'] and result['control_valid'],result
                    if mode in {'late_fork','caller_cancel'} or (mode=='new_console' and runtime=='windows'):
                        assert result['forced'],result
                    if mode=='slow':
                        assert all((work/('cleaned-'+name)).exists() for name in ('root','leaf'))
                        assert not result['forced'],result
                    if mode=='new_console': assert not (work/'received-leaf').exists()
                    await asyncio.wait_for(drain,5)
                    await exited(records,runtime)
                    # Completed calls retain the same first reason and result.
                    assert await process.stop(reason='timeout',grace_ms=0)==result
                    done(runtime+'_'+mode,process,records)
                finally:
                    if operation is not None and not operation.done():
                        await asyncio.gather(operation,return_exceptions=True)
                    if drain is not None and not drain.done():
                        drain.cancel();await asyncio.gather(drain,return_exceptions=True)
                    await process.abort()
        for mode in ('native_supervisor_death','compiler','pandoc'):
            work=root/mode;work.mkdir();process=owned.OwnedProcess();drain=None
            try:
                if mode=='native_supervisor_death':
                    (work/'tree.py').write_text(TREE,encoding='utf-8')
                    argv=arguments(work,'wsl','tree.py','2','ignore')
                elif mode=='pandoc':
                    pandoc=SERVER.parents[1]/'extracted/share/vibeflow/tools/pandoc/pandoc.exe'
                    assert pandoc.is_file(),'Verified product Pandoc runtime is unavailable'
                    (work/'busy.md').write_text('# Stop during conversion\n',encoding='utf-8')
                    (work/'busy.lua').write_text("function Pandoc(doc)\n local f=assert(io.open('compiler-ready.txt','w')); f:write('BUSY_COMPILER_REACHED'); f:close()\n while true do end\n return doc\nend\n",encoding='utf-8')
                    (work/'pandoc_owner.py').write_text('import os,subprocess,sys\nfrom pathlib import Path\np=subprocess.Popen([sys.argv[1],"busy.md","--lua-filter=busy.lua","-o","busy.html"])\nPath("node-0").write_text(str(p.pid))\nPath("node-1").write_text(str(os.getpid()))\nraise SystemExit(p.wait())\n',encoding='utf-8')
                    argv=[sys.executable,'-u',str(work/'pandoc_owner.py'),str(pandoc)]
                else:
                    # TeX stdout may be block buffered. Its own immediate write
                    # and close prove it reached the loop without relying on EOF.
                    (work/'busy.tex').write_text('\\documentclass{article}\n\\begin{document}\n\\immediate\\openout15=compiler-ready.txt\n\\immediate\\write15{BUSY_COMPILER_REACHED}\n\\immediate\\closeout15\n\\loop\\iftrue\\repeat\n\\end{document}\n',encoding='utf-8')
                    command='cd '+shlex.quote(owned.wsl_path(work))+' && exec latexmk -pdf -interaction=nonstopmode -halt-on-error busy.tex'
                    argv=['wsl.exe','-d','vibeflow-linux','--exec','bash','-lc',command]
                runtime='windows' if mode=='pandoc' else 'wsl'
                await process.prepare(argv,cwd=work,env=dict(os.environ),runtime=runtime)
                await process.release()
                if mode=='native_supervisor_death': await wait_ready(process,work)
                else:
                    drain=asyncio.create_task(process.proc.stdout.read())
                    await marker(work/'compiler-ready.txt',20)
                    assert (work/'compiler-ready.txt').read_text().strip()=='BUSY_COMPILER_REACHED'
                    assert process.proc.returncode is None
                records=await race_records(process,work,runtime)
                if drain is None: drain=asyncio.create_task(process.proc.stdout.read())
                extra=None
                if mode=='native_supervisor_death':
                    reply=await asyncio.to_thread(native_helper,KILL_NATIVE,process.record['native']['process'])
                    assert reply.strip()==b'OWNED_NATIVE_SUPERVISOR_KILLED'
                    await process.wait_root_exit(10)
                    result=await process.finish()
                    assert result['root_exit_code']!=0
                else:
                    if mode=='compiler':
                        extra=json.loads(await asyncio.to_thread(native_helper,DETAILS,records))
                        assert any(item['comm']=='pdflatex' for item in extra),extra
                    else:
                        import hashlib
                        extra={'pandoc_sha256':hashlib.sha256(pandoc.read_bytes()).hexdigest(),'child_pid':int((work/'node-0').read_text())}
                        assert any(item['pid']==extra['child_pid'] for item in records)
                    assert not (work/('busy.html' if mode=='pandoc' else 'busy.pdf')).exists()
                    result=await process.stop(grace_ms=1000)
                assert result['verified'] and result['scope_empty'],result
                if runtime=='wsl': assert result['native_state']=='exited',result
                await asyncio.wait_for(drain,5)
                await exited(records,runtime)
                done(mode,process,records,extra)
            finally:
                if drain is not None and not drain.done():
                    drain.cancel();await asyncio.gather(drain,return_exceptions=True)
                await process.abort()
    finally:
        for child in sentinels: child.communicate(b'x',timeout=10)
    report={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(root)}
    (root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('OWNED_TREE_RACES_OK '+json.dumps({'count':len(checks),'evidence':str(root)}))


if __name__=='__main__': asyncio.run(main())
