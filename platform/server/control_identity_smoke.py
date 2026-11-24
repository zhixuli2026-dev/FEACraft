"""Real bounded WSL control readers, including cancellation and API owner death."""
import json
from pathlib import Path
import subprocess
import sys
import time

from testsupport import IsolatedServer, SERVER
from professional_stop_smoke import wait_for
from mpi_scope_smoke import inspect_native
from app.services import owned_process as owned,process_identity_native as identity
from app.services import control_identity_reader as protocol


SENTINEL='''import json,os,subprocess,sys
sys.path.insert(0,sys.argv[1]);import process_identity_native as identity
child=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(120)'],stdin=subprocess.DEVNULL)
try:
 print(json.dumps({'root':identity.observe(os.getpid()),'child':identity.observe(child.pid)}),flush=True)
 sys.stdin.buffer.read(1)
finally:
 child.terminate();child.wait()
'''

CONTROLLED=r'''import asyncio,json,os
from pathlib import Path
from app.services import control_identity as control,process_identity_native as identity
root=Path(__file__).parent
control.READER=root/'helpers/control_identity_reader.py'
from app.main import app
task=None
@app.on_event('startup')
async def fixture():
    (root/'owner.json').write_text(json.dumps(identity.observe(os.getpid())))
    async def watch():
        while not (root/'exit-owner').exists():await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(watch())
@app.post('/api/v1/fixture/start')
async def start(body:dict):
    global task
    assert task is None or task.done()
    mode=body['mode'];(root/'helpers/mode').write_text(mode)
    observed=json.loads((root/'expected.json').read_text())
    kind='ancestry' if mode in {'ancestry','foreign_ancestry'} else 'identity'
    arguments={'root':observed['root'],'reported':observed['child']} if kind=='ancestry' else {'expected':observed['root']}
    if mode=='mismatch':arguments['expected']['start_ticks']=str(int(arguments['expected']['start_ticks'])+1)
    if mode=='foreign_ancestry':arguments={'root':observed['child'],'reported':observed['root']}
    distro='vibeflow-test-reader-absent' if mode=='not_running' else 'vibeflow-linux'
    operation=control.ControlRead(distro,kind,arguments)
    original=control.READER.read_bytes()
    if mode=='tamper':control.READER.write_bytes(original+b'\n# fixture changed selected implementation\n')
    async def execute():
        result=None;error=None;reason=None
        try:result=await operation.run()
        except BaseException as failure:
            error=type(failure).__name__;reason=getattr(failure,'reason',None)
        finally:
            if mode=='tamper':control.READER.write_bytes(original)
            data={'result':result,'error':error,'reason':reason,'record':operation.owned.record,
                  'stop':operation.owned.stop_result,'native_reader':operation.native_reader}
            pending=root/(mode+'-outcome.pending');pending.write_text(json.dumps(data))
            pending.replace(root/(mode+'-outcome.json'))
    task=asyncio.create_task(execute())
    return {'started':True}
@app.post('/api/v1/fixture/cancel')
async def cancel():
    task.cancel();await task
    return {'cancelled':True}
'''

FAULT='''    birth=identity.observe(os.getpid())
    fixture=Path(__file__).parent;mode=(fixture/'mode').read_text()
    pending=fixture/(mode+'-birth.pending');pending.write_text(json.dumps(birth))
    pending.replace(fixture/(mode+'-birth.json'))
    if mode=='overflow':
        print('x'*17000,flush=True)
    if mode in {'cancel','timeout','api_death','overflow'}:
        import time
        # Pause only after the real native deadline/owner watcher is armed.
        # A release file is independent test teardown, never a production hook.
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and not (fixture/'release').exists():time.sleep(.02)
'''


def main():
    api=IsolatedServer('control-reader',app='controlled_reader:app')
    helpers=api.root/'helpers';helpers.mkdir()
    for name in protocol.FILES:(helpers/name).write_bytes((SERVER/'app/services'/name).read_bytes())
    reader=helpers/'control_identity_reader.py';source=reader.read_text(encoding='utf8')
    assert source.count('    birth=identity.observe(os.getpid())\n')==1
    source=source.replace('    birth=identity.observe(os.getpid())\n',FAULT)
    source=source.replace('    return response(request,reader=birth,result=result)',
        "    reply=response(request,reader=birth,result=result)\n    if mode=='bad_reply':reply['nonce']='0'*64\n    return reply")
    reader.write_text(source,encoding='utf8')
    (api.root/'controlled_reader.py').write_text(CONTROLLED,encoding='utf8')
    sentinel=subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u','-c',SENTINEL,
        owned.wsl_path(SERVER/'app/services')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    expected=json.loads(sentinel.stdout.readline());(api.root/'expected.json').write_text(json.dumps(expected))
    checks=[];receipts=[];births=[]
    try:
        with api:
            for mode in ('normal','ancestry','mismatch','foreign_ancestry','not_running','tamper','bad_reply','overflow','cancel','timeout','api_death'):
                assert api.call('POST','/fixture/start',{'mode':mode})['started']
                capture=helpers/(mode+'-birth.json')
                birth=None
                if mode not in {'not_running','tamper'}:
                    wait_for(lambda:capture.exists(),seconds=8)
                    birth=json.loads(capture.read_text());births.append(birth)
                triggered=time.monotonic()
                if mode=='cancel':assert api.call('POST','/fixture/cancel')['cancelled']
                if mode=='api_death':
                    assert inspect_native({'probe':[birth]})[0]['state']=='match'
                    owner=json.loads((api.root/'owner.json').read_text())
                    assert identity.probe(owner)['state']=='match'
                    (api.root/'exit-owner').touch();wait_for(lambda:identity.probe(owner)['state']=='exited')
                    outcome=None
                else:
                    outcome_path=api.root/(mode+'-outcome.json')
                    wait_for(lambda:outcome_path.exists(),seconds=18)
                    outcome=json.loads(outcome_path.read_text())
                    if mode in {'normal','ancestry','mismatch'}:
                        assert outcome['error'] is None,outcome
                        assert outcome['native_reader']==birth
                        if mode=='ancestry':assert outcome['result']==expected['child']
                        else:assert outcome['result']['state']==('match' if mode=='normal' else 'mismatch')
                    else:
                        assert outcome['error'] is not None and outcome['result'] is None,outcome
                        if mode=='not_running':assert outcome['reason']=='distribution_not_running'
                        if mode=='bad_reply':assert outcome['reason']=='native_probe_invalid'
                        if mode=='cancel':assert outcome['error']=='CancelledError'
                    if mode=='tamper':assert outcome['record'] is None and outcome['stop'] is None
                    else:
                        assert outcome['stop']['verified'] and outcome['stop']['scope_empty'] and outcome['stop']['scope_observation']['complete'],outcome
                        assert identity.probe(outcome['record']['host'])['state']=='exited'
                if birth:wait_for(lambda:inspect_native({'probe':[birth]})[0]['state']=='exited',seconds=15)
                elapsed=time.monotonic()-triggered
                # Owner-pipe closure must be observable sooner than the native
                # 10-second watchdog; test that independently of deadline exit.
                if mode in {'cancel','api_death'}:assert elapsed<4,(mode,elapsed)
                if mode=='timeout':assert 8<elapsed<15,elapsed
                assert sentinel.poll() is None
                assert all(p['state']=='match' for p in inspect_native({'probe':list(expected.values())}))
                receipts.append({'mode':mode,'native_reader':birth,'outcome':outcome,'exit_observation_seconds':elapsed,
                    'unrelated_native_family_alive':True})
                checks.append('native_control_reader_'+mode+'_bounded_owned_exit')
                (api.root/'reader-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
    finally:
        (helpers/'release').touch()
        for birth in births:wait_for(lambda:inspect_native({'probe':[birth]})[0]['state']=='exited',seconds=15)
        sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
            'actual_wsl_readers':True,'fixture_delays_after_lifetime_armed':True,'api_death_terminal_receipt_not_fabricated':True}
    (api.root/'reader-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result))


if __name__=='__main__':main()
