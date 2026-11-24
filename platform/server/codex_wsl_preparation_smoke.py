"""Real WSL initializer faults through durable high-level CLI startup.

Only the private fixture copy of the fixed initializer is delayed/faulted;
production launch gates, ownership, cancellation and receipts remain in use.
"""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from testsupport import IsolatedServer
from codex_session_smoke import CONTROLLED, WORKER, wait_for
from codex_wsl_session_smoke import NATIVE
from codex_wsl_test_support import NativeStateFixture
from app.services import owned_stdio, process_identity_native as identity
from app.services.owned_process import probe_native, wsl_path


CONTROL = r'''
import os
from app.services import owned_stdio
owned_stdio.BRIDGE=root/'stdio_bridge.py'
owned_stdio.NATIVE_STATE=root/'codex_wsl_state.py'
sessions.BRIDGE=owned_stdio.BRIDGE
sessions.NATIVE_STATE=owned_stdio.NATIVE_STATE
sessions.HEARTBEAT_INTERVAL=60
prepared=sessions.Session.state_prepared
async def state_prepared(self):
    if (root/'preparation-mode').read_text() in {'revoke','owner_changed'}:
        (root/'prepared-gate').write_text(self.id)
        while not (root/'release-launch').exists():await asyncio.sleep(.02)
    return await prepared(self)
sessions.Session.state_prepared=state_prepared
@app.post('/api/v1/fixture/revoke/{cid}')
async def revoke(cid:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(Conversation,cid);row.permission_mode='full_access';await db.commit()
    return {'ok':True}
@app.post('/api/v1/fixture/replace-owner/{identifier}')
async def replace_owner(identifier:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeCliSession,identifier);row.owner_token_sha256='0'*64;await db.commit()
    return {'ok':True}
@app.on_event('startup')
async def owner_exit():
    (root/'actual-owner.json').write_text(json.dumps(records.identity.observe(os.getpid())))
    async def watch():
        while not (root/'exit-owner').exists():await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(watch())
'''


def native_exited(record):
    return asyncio.run(probe_native('vibeflow-linux', record))['state'] == 'exited'


def run():
    api = IsolatedServer('cli-wsl-preparation', app='controlled_preparation:app')
    (api.root/'cli.py').write_text(WORKER, encoding='utf8')
    (api.root/'controlled_preparation.py').write_text(CONTROLLED+NATIVE+CONTROL, encoding='utf8')
    shutil.copyfile(owned_stdio.BRIDGE, api.root/'stdio_bridge.py')
    shutil.copyfile(owned_stdio.IDENTITY, api.root/'process_identity_native.py')
    helper = owned_stdio.NATIVE_STATE.read_text(encoding='utf-8-sig').replace('def initialize(request):', 'def original_initialize(request):')
    helper += '''
def initialize(request):
    import time
    root=Path(ROOT_PLACEHOLDER)
    mode=(root/'preparation-mode').read_text()
    (root/'initializer-entered').write_text(json.dumps({'home':home_path(request),'uid':os.getuid()}))
    if mode in {'cancel','timeout','api_death'}:
        while not (root/'release-initializer').exists():time.sleep(.02)
    if mode=='failure':raise ValueError('isolated initializer failure')
    return original_initialize(request)
'''.replace('ROOT_PLACEHOLDER', repr(wsl_path(api.root)))
    (api.root/'codex_wsl_state.py').write_text(helper, encoding='utf8')
    marker = 'import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'
    sentinel = subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',
        '/usr/bin/python3','-I','-S','-u','-c',marker], stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert sentinel.stdout.readline().strip()==b'READY'
    checks, receipts = [], []
    try:
        with NativeStateFixture(), api:
            for mode in ('root_registration','bridge_registration','normal','cancel','timeout','failure','revoke','owner_changed','api_death'):
                for name in ('initializer-entered','prepared-gate','release-launch','release-initializer'):
                    (api.root/name).unlink(missing_ok=True)
                (api.root/'preparation-mode').write_text(mode)
                case=api.create_case('WSL preparation '+mode)
                api.call('POST','/fixture/mode/'+{'root_registration':'root_commit','bridge_registration':'bridge_commit'}.get(mode,'normal'))
                conv=api.call('POST','/conversations',{'case_id':case['id'],'permission_mode':'ask'})
                prefix='/codex/conversations/'+str(conv['id'])+'/session-requests'
                body={'client_request_id':uuid.uuid4().hex,'runtime':'wsl','purpose':'inspect',
                      'provider_profile_id':None,'provider_wire_api':None}
                before={p.name for p in api.root.glob('cli-*.json')}
                value=api.call('POST',prefix,body)
                path='/codex/session-requests/'+value['id']
                if mode in {'root_registration','bridge_registration'}:
                    final=wait_for(lambda:api.call('GET',path),lambda r:r['phase'] not in {'admitted','starting'},seconds=30)
                    assert final['phase']=='failed' and final['cleanup_verified'],final
                    assert not (api.root/'initializer-entered').exists()
                    assert {p.name for p in api.root.glob('cli-*.json')}==before
                    assert sentinel.poll() is None
                    checks.append(mode+'_commit_failure_blocks_initializer_itself')
                    receipts.append({'mode':mode,'final':final})
                    continue
                if mode=='normal':
                    final=wait_for(lambda:api.call('GET',path),lambda r:r['phase']=='completed',seconds=30)
                    assert final['session']['phase']=='active'
                    closed=api.call('DELETE','/codex/sessions/'+final['session']['id'])
                    assert closed['cleanup_verified'] and closed['stop']['native_state']=='exited'
                    final=api.call('GET',path)
                    assert final['cleanup_verified']
                    assert len({p.name for p in api.root.glob('cli-*.json')}-before)==1
                    checks.append('owned_initializer_completes_then_explicit_cli_release_and_verified_close')
                    receipts.append({'mode':mode,'final':final})
                    continue
                if mode in {'revoke','owner_changed'}:
                    wait_for(lambda:(api.root/'prepared-gate').exists(),bool,seconds=15)
                elif mode!='failure':
                    wait_for(lambda:(api.root/'initializer-entered').exists(),bool,seconds=15)
                initial=api.call('GET',path)
                if mode!='failure':
                    assert initial['phase']=='starting' and initial['session']['phase']=='starting',initial
                    process=initial['session']['process']
                    assert process['root'] is not None and process['bridge'] is not None
                    assert {p.name for p in api.root.glob('cli-*.json')}==before
                    # Replays/read-only discovery cannot create a second initializer.
                    assert api.call('POST',prefix,body)['id']==value['id']
                    assert api.call('GET',prefix+'?client_request_id='+body['client_request_id'])[0]['id']==value['id']
                if mode=='cancel':
                    final=api.call('POST',path+':cancel')
                    assert final['phase']=='cancelled',final
                elif mode=='api_death':
                    owner=json.loads((api.root/'actual-owner.json').read_text())
                    assert identity.probe(owner)['state']=='match'
                    (api.root/'exit-owner').touch()
                    wait_for(lambda:identity.probe(owner)['state']=='exited',bool)
                    wait_for(lambda:native_exited(process['root']['native']['process']),bool)
                    wait_for(lambda:native_exited(process['bridge']),bool)
                    final=None
                else:
                    if mode in {'revoke','owner_changed'}:
                        api.call('POST',('/fixture/revoke/'+str(conv['id']) if mode=='revoke'
                                        else '/fixture/replace-owner/'+initial['session']['id']))
                        (api.root/'release-launch').touch()
                    final=wait_for(lambda:api.call('GET',path),lambda r:r['phase'] not in {'admitted','starting'},seconds=30)
                    assert final['phase']==('recovery_required' if mode=='owner_changed' else 'failed'),final
                if final is not None:
                    if mode!='owner_changed':
                        assert final['cleanup_verified'] and final['session']['stop']['native_state']=='exited',final
                    else:
                        assert not final['cleanup_verified'], 'An obsolete owner must not invent a durable stop receipt'
                    process=final['session']['process']
                    wait_for(lambda:native_exited(process['root']['native']['process']),bool)
                    wait_for(lambda:native_exited(process['bridge']),bool)
                assert {p.name for p in api.root.glob('cli-*.json')}==before
                assert sentinel.poll() is None
                checks.append(mode+'_during_owned_preparation_no_cli_and_native_births_exited')
                receipts.append({'mode':mode,'initial':initial,'final':final,
                                 'sentinel_alive':True,'api_death_has_no_fabricated_receipt':mode=='api_death'})
                (api.root/'preparation-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
    finally:
        sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
            'actual_wsl':True,'simulated_cli':True,'initializer_faults_in_private_fixture_copy':True}
    (api.root/'preparation-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result))


if __name__=='__main__':run()
