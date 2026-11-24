"""Real SU2 execution: cancellation, deadline and API owner death.

Read-only Job Object snapshots capture the actual solver's birth identity.
No extra Job handle is retained across owner death, which would alter cleanup.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from app.services import process_identity_native as identity
from professional_launch_smoke import settled
from testsupport import IsolatedServer, SERVER


CONTROLLED = '''import asyncio,json,os
from pathlib import Path
from app.services import jobrunner
from app.services import process_identity_native as identity
root=Path(__file__).parent
original_init=jobrunner.JobProcess.__init__
def init(self,*args,**kwargs):
    if (root/'mode').read_text()=='timeout': kwargs['timeout_seconds']=8
    original_init(self,*args,**kwargs)
jobrunner.JobProcess.__init__=init
from app.main import app
@app.on_event('startup')
async def owned_test_exit():
    (root/'api-identity.json').write_text(json.dumps(identity.observe(os.getpid())))
    async def watch():
        while not (root/'exit-owned-api').exists(): await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(watch())
'''


def members(name):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenJobObjectW': ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                       ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'IsProcessInJob': ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
    }
    for function, (arguments, result) in signatures.items():
        getattr(kernel, function).argtypes = arguments
        getattr(kernel, function).restype = result

    class Members(ctypes.Structure):
        _fields_ = [('assigned', wintypes.DWORD), ('returned', wintypes.DWORD),
                    ('pids', ctypes.c_size_t * 128)]

    job = kernel.OpenJobObjectW(4, False, name)  # JOB_OBJECT_QUERY only
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    found = []
    try:
        info = Members()
        if not kernel.QueryInformationJobObject(job, 3, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        assert info.returned == info.assigned and info.returned <= 128
        for pid in info.pids[:info.returned]:
            process = kernel.OpenProcess(0x1000 | 0x00100000, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                member = wintypes.BOOL()
                assert kernel.IsProcessInJob(process, job, ctypes.byref(member)) and member.value
                birth = identity.windows_identity(process)
                path, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                assert kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length))
                found.append({'identity': birth, 'executable': path.value})
            except ProcessLookupError:
                continue
            finally:
                kernel.CloseHandle(process)
    finally:
        kernel.CloseHandle(job)
    return found


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.03)
    raise TimeoutError('Professional scope observation timed out')


def main():
    assert os.name == 'nt', 'Installed SU2 fixture requires Windows'
    server = IsolatedServer('professional-stop', app='controlled_professional:app')
    (server.root / 'controlled_professional.py').write_text(CONTROLLED, encoding='utf8')
    (server.root / 'mode').write_text('cancel')
    sentinel = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-I', '-S', '-u', '-c',
        'import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    assert sentinel.stdout.readline().strip() == b'READY'
    checks, receipts = [], []
    try:
        with server as api:
            for mode in ('cancel', 'timeout', 'api_death'):
                (api.root / 'mode').write_text(mode)
                api.create_case('Actual SU2 ' + mode)
                example = SERVER / 'examples/su2'
                config = (example / 'inv_NACA0012.cfg').read_text().replace('ITER= 250', 'ITER= 10000000')
                config = config.replace('CONV_STARTITER= 10', 'CONV_STARTITER= 10000000')
                config = config.replace('OUTPUT_WRT_FREQ= 250', 'OUTPUT_WRT_FREQ= 10000000')
                config = config.replace('MESH_FILENAME= mesh_NACA0012_inv.su2', 'MESH_FILENAME= C-run/input.su2')
                api.put('C-run/input.cfg', config)
                api.put('C-run/input.su2', (example / 'mesh_NACA0012_inv.su2').read_text())
                job_id = api.call('POST', api.prefix + '/su2:solve', {'config_path': 'C-run/input.cfg'})['job_id']
                row = api.call('GET', f'/jobs/{job_id}')
                record = row['attempt']['process_identity']
                directory=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                progress=directory/row['attempt']['workspace']['run_path']/'history.csv'

                def solver_members():
                    children=api.call('GET',f'/jobs/{job_id}')['attempt']['child_processes']['children']
                    running=[c for c in children.values() if c['phase']=='running' and c['identity']]
                    if not running:return None
                    current = members(running[0]['identity']['scope'])
                    if not progress.exists() or len(progress.read_text(errors='replace').splitlines())<3:
                        return None
                    return current if any(Path(member['executable']).name.lower() == 'su2_cfd.exe'
                                          for member in current) else None

                captured = wait_for(solver_members, seconds=5)
                assert all(identity.probe(member['identity'])['state'] == 'match' for member in captured)
                if mode == 'api_death':
                    owner = json.loads((api.root / 'api-identity.json').read_text())
                    assert identity.probe(owner)['state'] == 'match'
                    (api.root / 'exit-owned-api').touch()
                    wait_for(lambda: identity.probe(owner)['state'] == 'exited')
                    final, stop = None, None
                else:
                    if mode == 'cancel':
                        assert api.call('POST', f'/jobs/{job_id}:cancel')['ok']
                    final = settled(api, job_id)
                    assert final['status'] == ('cancelled' if mode == 'cancel' else 'failed'), final
                    stop = final['attempt']['process_stop']
                    assert stop['reason'] == ('cancelled' if mode == 'cancel' else 'timeout'), stop
                    assert stop['verified'] and stop['scope_empty'] and stop['scope_observation']['complete']
                    children=final['attempt']['child_processes']['children']
                    assert children and all(c['stop'] and c['stop']['verified'] for c in children.values())
                assert not (directory/'history.csv').exists(),'Interrupted output leaked into live case'
                wait_for(lambda: all(identity.probe(member['identity'])['state'] == 'exited' for member in captured))
                assert sentinel.poll() is None
                receipts.append({'mode': mode, 'job_id': job_id, 'identity': record, 'members': captured,
                                 'final': final, 'stop': stop, 'unrelated_sentinel_alive': True,
                                 'api_death_terminal_receipt_not_fabricated': mode == 'api_death'})
                checks.append('actual_su2_' + mode + '_all_captured_births_exited')
                (api.root / 'stop-receipts.json').write_text(json.dumps(receipts, indent=2), encoding='utf8')
                print('PASS ' + checks[-1], flush=True)
    finally:
        sentinel.communicate(b'x', timeout=10)
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'evidence': str(server.root),
              'actual_solver': 'SU2', 'full_professional_fault_matrix_complete': False}
    (server.root / 'stop-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
