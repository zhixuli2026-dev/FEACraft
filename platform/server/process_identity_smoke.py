"""Real owned OS birth identities, PID mismatch, handles and scope containment."""
import argparse
import copy
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from app.services import process_identity_native as identity
from testsupport import SERVER

CHILD="import sys; print('READY',flush=True); sys.stdin.buffer.read(1)"

LINUX='''import copy,ctypes,json,os,subprocess,sys,uuid
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import process_identity_native as identity
checks=[]
child=None
sentinel=None
pidfd=None
try:
    sentinel=subprocess.Popen([sys.executable,'-c',"import sys; print('READY',flush=True); sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,start_new_session=True)
    assert sentinel.stdout.readline()==b'READY\\n'
    code="import ctypes,sys; ctypes.CDLL(None).prctl(15,b'odd ) name',0,0,0); print('READY',flush=True); sys.stdin.buffer.read(1)"
    child=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,start_new_session=True)
    assert child.stdout.readline()==b'READY\\n'
    value=identity.observe(child.pid)
    assert value['process_group']==value['session']==child.pid
    assert identity.probe(value)['state']=='match'
    checks.append('live_linux_birth_boot_namespace_and_group_match_with_parentheses_in_comm')
    for key,replacement in [('start_ticks',str(int(value['start_ticks'])+1)),
            ('boot_id',str(uuid.uuid4())),('host_id','0'*64),('uid',value['uid']+1),
            ('pid_namespace',str(int(value['pid_namespace'])+1)),('process_group',child.pid+1),('session',child.pid+1)]:
        changed=dict(value,**{key:replacement})
        assert identity.probe(changed)['state']=='mismatch',(key,changed)
        assert child.poll() is None and sentinel.poll() is None
    checks.append('changed_linux_birth_boot_host_user_namespace_group_and_session_never_match')
    pidfd=os.pidfd_open(child.pid)
    assert identity.probe(value)['state']=='match'
    child.communicate(b'x',timeout=5)
    assert identity.probe(value)['state']=='exited'
    assert sentinel.poll() is None
    checks.append('exited_owned_linux_process_and_surviving_sentinel')
    for value in ({'pid':sentinel.pid},None,{},dict(identity.observe(sentinel.pid),start_ticks=1),
            dict(identity.observe(sentinel.pid),pid=True)):
        assert identity.probe(value)['state']=='unverified'
    checks.append('malformed_and_legacy_linux_identity_fail_closed')
    cli=subprocess.run([sys.executable,str(Path(identity.__file__)),'--probe'],
        input=json.dumps(identity.observe(sentinel.pid)).encode(),capture_output=True,timeout=5)
    assert cli.returncode==0 and json.loads(cli.stdout)['state']=='match',cli.stderr
    checks.append('standalone_WSL_probe_has_no_application_dependency')
    print(json.dumps({'passed':True,'count':len(checks),'checks':checks}))
finally:
    if pidfd is not None: os.close(pidfd)
    for proc in (child,sentinel):
        if proc is not None:
            if proc.poll() is None:
                try: proc.communicate(b'x',timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill();proc.communicate(timeout=5)
'''


def windows():
    from app.services.windows_process_scope import ProcessHandle,WindowsScope,ExtendedLimits
    checks=[]
    scopes=[]
    handles=[]
    children=[]
    sentinel=child=descendant=None
    # A Windows venv python.exe can be a redirector which starts the real
    # interpreter before Python code runs. The trusted launch gate must itself
    # be the process represented by the creation handle, before any payload.
    base_python=str(Path(sys._base_executable).resolve())
    try:
        sentinel=subprocess.Popen([sys.executable,'-c',CHILD],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(sentinel)
        assert sentinel.stdout.readline()==b'READY\r\n'
        sent_handle=ProcessHandle(int(sentinel._handle))
        handles.append(sent_handle)
        sentinel_identity=sent_handle.identity()
        assert identity.observe(sentinel.pid)==sentinel_identity and identity.probe(sentinel_identity)['state']=='match'
        checks.append('Windows_creation_handle_and_readonly_PID_probe_agree')
        for key,value in (('creation_time_100ns',str(int(sentinel_identity['creation_time_100ns'])+1)),('host_id','0'*64)):
            assert identity.probe(dict(sentinel_identity,**{key:value}))['state']=='mismatch'
            assert sentinel.poll() is None
        for value in ({'pid':sentinel.pid},None,{},dict(sentinel_identity,pid=True),
                      dict(sentinel_identity,creation_time_100ns=int(sentinel_identity['creation_time_100ns']))):
            assert identity.probe(value)['state']=='unverified'
        checks.append('Windows_birth_host_and_malformed_identity_do_not_authorize_PID_reuse')

        nonce=uuid.uuid4().hex+uuid.uuid4().hex
        scope=WindowsScope(nonce)
        scopes.append(scope)
        assert ctypes.sizeof(ExtendedLimits)==144
        assert not scope.contains(sent_handle)
        code=("import subprocess,sys; print('READY',flush=True); sys.stdin.buffer.read(1); "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW); "
              "print(p.pid,flush=True); sys.stdin.buffer.read(1)")
        child=subprocess.Popen([base_python,'-I','-S','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(child)
        assert child.stdout.readline()==b'READY\r\n'
        handle=ProcessHandle(int(child._handle))
        handles.append(handle)
        expected=handle.identity()
        try:
            scope.assign(handle,dict(expected,creation_time_100ns=str(int(expected['creation_time_100ns'])+1)))
            raise AssertionError('Mismatched creation identity was assigned')
        except ValueError:
            assert not scope.contains(handle) and child.poll() is None
        scope.assign(handle,expected)
        assert scope.contains(handle) and not scope.contains(sent_handle)
        try:
            WindowsScope(nonce)
            raise AssertionError('Existing named scope was claimed')
        except FileExistsError:
            assert child.poll() is None
        checks.append('fresh_nested_Windows_scope_checks_identity_and_rejects_name_collision')
        child.stdin.write(b'x');child.stdin.flush()
        descendant=int(child.stdout.readline())
        descendant_identity=identity.observe(descendant)
        descendant_handle=ProcessHandle.query(descendant_identity)
        handles.append(descendant_handle)
        assert scope.contains(descendant_handle)
        assert identity.probe(descendant_identity)['state']=='match'
        scope.terminate(124)
        scope.wait_empty(5)
        descendant_handle.wait_exit(5)
        assert child.wait(timeout=5)==124
        assert identity.probe(expected)['state']=='exited'
        assert identity.probe(descendant_identity)['state']=='exited',identity.probe(descendant_identity)
        assert sentinel.poll() is None
        checks.append('explicit_owned_scope_termination_reaches_descendant_and_preserves_unassigned_sentinel')
        handle.close()
        handle.close()

        second=WindowsScope(uuid.uuid4().hex+uuid.uuid4().hex)
        scopes.append(second)
        child=subprocess.Popen([base_python,'-I','-S','-c',CHILD],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(child)
        assert child.stdout.readline()==b'READY\r\n'
        handle=ProcessHandle(int(child._handle));handles.append(handle)
        expected=handle.identity()
        second.assign(handle,expected)
        second.close()
        child.wait(timeout=5)
        assert identity.probe(expected)['state']=='exited' and sentinel.poll() is None
        checks.append('closing_last_owned_scope_handle_terminates_only_its_processes')
        return {'passed':True,'count':len(checks),'checks':checks}
    finally:
        for scope in scopes: scope.close()
        for proc in children:
            if proc.poll() is None:
                try: proc.communicate(b'x',timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill();proc.communicate(timeout=5)
        for handle in handles: handle.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--distro',default='vibeflow-linux')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9-]+',args.distro):
        raise ValueError('Invalid test distribution')
    report=Path(tempfile.mkdtemp(prefix='process-identity-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'windows':windows()}
    print('WINDOWS_PROCESS_IDENTITY_OK '+json.dumps(results['windows']),flush=True)
    script=report/'linux-smoke.py'
    script.write_text(LINUX,encoding='utf-8')
    def wsl(path): return '/mnt/'+path.drive[0].lower()+path.as_posix()[2:]
    completed=subprocess.run(['wsl.exe','-d',args.distro,'--exec','python3',wsl(script),wsl(Path(identity.__file__).parent)],
        capture_output=True,text=True,encoding='utf-8',timeout=45)
    (report/'linux.log').write_text(completed.stdout+completed.stderr,encoding='utf-8')
    assert completed.returncode==0,completed.stderr
    results['wsl']=json.loads(completed.stdout)
    assert results['wsl']['passed']
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('PROCESS_IDENTITY_OK '+json.dumps({'evidence':str(report),'results':results}))


if __name__=='__main__':
    import re
    main()
