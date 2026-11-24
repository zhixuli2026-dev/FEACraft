"""Real WSL export and public sandbox restore with 40 MiB subtree staging."""
import argparse
import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time
import uuid

import aiohttp
import httpx

from case_copy_final_stage_acceptance import interrupt_and_restart
from case_copy_source_lock_smoke import LARGE, RUNTIME, digest_response, read_observation, seed, writer_available
from s3testsupport import ObjectStore
from sandbox_recovery_ui_cleanup import verify as verify_cleanup
from sandbox_retirement_checks import verify as verify_retirement
from testsupport import IsolatedServer, SERVER


EDIT = 'Ref/updated-by-sandbox.txt'
SAVE = 'Ref/concurrent-save.txt'

FIXTURE = r'''
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from fastapi import Depends
from botocore.client import BaseClient
from app.deps import get_current_user
from app.main import app
from app.services import case_read_capacity, file_transactions, process_identity_native, tree_transactions
from app.services.case_write import _writes
from app.services.request_deadline import current

root = Path(__file__).parent
original_capture = file_transactions._capture
original_move = tree_transactions._move
original_read = tree_transactions.read_recovery
original_call = BaseClient._make_api_call
original_finish = file_transactions.finish_committed
original_deadline = case_read_capacity.file_read_deadline
short_recovery = ContextVar('short_recovery_acceptance', default=False)


def pause(name, size):
    label = root/('observe-'+name)
    if not label.exists():
        return
    label.unlink()
    temporary = root/('observed-'+name+'.new')
    observed = {'bytes':size,'has_case_lock':_writes.get() is not None}
    if name == 'cold-deadline':
        observed['deadline_remaining'] = current().remaining()
    temporary.write_text(json.dumps(observed), encoding='utf8')
    temporary.replace(root/('observed-'+name+'.json'))
    until = time.monotonic()+50
    while not (root/('release-'+name)).exists():
        if time.monotonic() >= until:
            raise TimeoutError('Actual tree observation was not released')
        time.sleep(.025)


def capture(path, destination=None, **kwargs):
    if path.name == 'archive-large.bin' and _writes.get() is not None:
        with (root/'archive-under-lock.txt').open('a', encoding='ascii') as stream:
            stream.write('capture\n')
    if path.name == 'archive-large.bin':
        if destination is not None:
            pause('tree', path.stat().st_size)
        elif 'before' in path.parts and 'pending' in path.parts:
            pause('cold-deadline', path.stat().st_size)
            pause('cold', path.stat().st_size)
        else:
            pause('preview', path.stat().st_size)
    return original_capture(path, destination, **kwargs)


def move(source, destination, **kwargs):
    out = original_move(source, destination, **kwargs)
    label = root/'exit-tree-restore'
    if label.exists() and destination.parent.name == 'before':
        label.unlink()
        os._exit(84)
    return out


def read_recovery(case):
    result = original_read(case)
    if result is not None:
        pause('recovery-ready', 0)
    return result


async def finish_committed(db, case):
    label = root/'exit-tree-settlement'
    if label.exists() and db.info.get('installed_local_trees', {}).get(case.id) is not None:
        label.unlink()
        os._exit(85)
    return await original_finish(db, case)


class Body:
    def __init__(self, body, size):
        self.body, self.size = body, size

    def __getattr__(self, name):
        return getattr(self.body, name)

    def read(self, amount):
        data = self.body.read(amount)
        if data:
            pause('preview', self.size)
        return data


def api_call(self, operation, args):
    result = original_call(self, operation, args)
    if (operation == 'GetObject' and args['Key'].endswith('/archive-large.bin')
            and (root/'observe-preview').exists()):
        result['Body'] = Body(result['Body'], result['ContentLength'])
    return result


@asynccontextmanager
async def deadline(code, label, *, seconds=None):
    if short_recovery.get() and code == 'file_recovery':
        seconds = 4
    async with original_deadline(code,label,seconds=seconds):
        yield


file_transactions._capture = capture
file_transactions.finish_committed = finish_committed
tree_transactions._move = move
tree_transactions.read_recovery = read_recovery
BaseClient._make_api_call = api_call
case_read_capacity.file_read_deadline = deadline
if (root/'legacy-tree').exists():
    # Exercise production cross-volume fallback without mounting a second drive.
    tree_transactions.staging_root = lambda prefix: None


@app.get('/fixture/tree-owner')
def observed_owner(user=Depends(get_current_user)):
    return {'identity':process_identity_native.observe(os.getpid())}


class ObserveRequests:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = short_recovery.set(b'x-fixture-cold-deadline' in dict(scope.get('headers', ())))
        try:
            return await self.app(scope, receive, send)
        finally:
            short_recovery.reset(token)


app.add_middleware(ObserveRequests)
'''


def observe(api, *, lock_expected, name='tree'):
    result = read_observation(api, 'observed-'+name+'.json')
    result['sqlite_writer_available'] = writer_available(api)
    assert result == {'bytes': 41943040, 'has_case_lock': lock_expected,
                      'sqlite_writer_available': not lock_expected}, result
    return result


def client(api):
    return httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization':'Bearer '+api.token},
                        trust_env=False, timeout=110)


async def preview_check(api, endpoint, selection):
    with client(api) as http, ThreadPoolExecutor(max_workers=1) as pool:
        (api.root/'observe-preview').touch()
        pending = pool.submit(http.post, endpoint+'/restore-plan', json=selection)
        try:
            evidence = await asyncio.to_thread(observe, api, lock_expected=False, name='preview')
            save = await asyncio.to_thread(http.put, api.prefix+'/file',
                                           json={'path':'Ref/during-preview.txt','content':'saved during preview'})
            assert save.status_code == 200, save.text
        finally:
            (api.root/'release-preview').touch()
        result = await asyncio.to_thread(pending.result, 100)
        assert result.status_code == 409 and result.json()['detail']['code']=='recovery_plan_stale',result.text
        fresh = await asyncio.to_thread(api.call,'POST',endpoint+'/restore-plan',selection)
        return {**evidence, 'concurrent_save_http':200, 'stale_preview_http':409, 'fresh_plan':fresh['plan_sha256']}


async def reject_changed_recovery(api, pending_tree):
    route = api.prefix+'/file-transactions:recover'
    target = pending_tree/'before/0/archive-large.bin'
    workspace = (await asyncio.to_thread(api.call, 'GET', '/workspaces'))[0]['id']
    another = await asyncio.to_thread(api.call,'POST',f'/workspaces/{workspace}/cases',{'name':'Concurrent recovery case'})
    with client(api) as http, ThreadPoolExecutor(max_workers=1) as pool:
        for marker in ('cold','recovery-ready'):
            (api.root/('observe-'+marker)).touch()
        pending = pool.submit(http.post, route)
        old = None
        try:
            evidence = await asyncio.to_thread(observe, api, lock_expected=False, name='cold')
            save = await asyncio.to_thread(http.put, f'/cases/{another["id"]}/file',
                                           json={'path':SAVE,'content':'saved while cold journal is read'})
            assert save.status_code==200,save.text
            (api.root/'release-cold').touch()
            await asyncio.to_thread(read_observation,api,'observed-recovery-ready.json')
            with target.open('r+b') as stream:
                old = stream.read(1)
                stream.seek(0)
                stream.write(bytes([old[0] ^ 0xff]))
            (api.root/'release-recovery-ready').touch()
            denied = await asyncio.to_thread(pending.result, 100)
            assert denied.status_code==409 and denied.json()['detail']['code']=='file_recovery_required',denied.text
            assert target.exists() and pending_tree.exists()
        finally:
            for marker in ('cold','recovery-ready'):
                (api.root/('release-'+marker)).touch()
            if old is not None:
                with target.open('r+b') as stream:
                    stream.write(old)
    return {**evidence,'concurrent_case_save_http':200,'changed_retained_version_http':409}


async def recovery_deadline(api, pending_tree):
    route = api.prefix+'/file-transactions:recover'
    original = (pending_tree/'manifest.json').read_bytes()
    with client(api) as http, ThreadPoolExecutor(max_workers=1) as pool:
        (api.root/'observe-cold-deadline').touch()
        pending = pool.submit(http.post, route, headers={'X-Fixture-Cold-Deadline':'1'})
        try:
            body = await asyncio.to_thread(read_observation, api, 'observed-cold-deadline.json')
            assert body['bytes']==41943040 and not body['has_case_lock']
            assert 0<body['deadline_remaining']<4 and writer_available(api)
            await asyncio.sleep(body['deadline_remaining'] + .15)
            assert not pending.done()
        finally:
            (api.root/'release-cold-deadline').touch()
        rejected = await asyncio.to_thread(pending.result, 30)
        assert rejected.status_code==504 and rejected.json()['detail']['code']=='file_recovery_timeout',rejected.text
        assert (pending_tree/'manifest.json').read_bytes()==original and (pending_tree/'before/0/archive-large.bin').exists()
    return {'http':504,'pending_retained':True,'worker_drained':True}


async def inspect_or_recover_cold_tree(api, pending_tree, *, diagnose):
    for name in ('observed-cold.json', 'release-cold'):
        (api.root/name).unlink(missing_ok=True)
    (api.root/'observe-cold').touch()
    route = '/file-transactions' if diagnose else '/file:version?path='+EDIT
    with client(api) as http, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(http.get, api.prefix+route)
        try:
            evidence = await asyncio.to_thread(observe, api, lock_expected=False, name='cold')
        finally:
            (api.root/'release-cold').touch()
        result = await asyncio.to_thread(pending.result, 100)
        assert result.status_code == 200, result.text
        if diagnose:
            assert result.json()['kind'] == 'tree' and result.json()['database_committed'] is False
            assert pending_tree.exists()
            evidence['read_only'] = True
        else:
            assert result.json()['content_sha256'] == hashlib.sha256(b'after').hexdigest()
            assert not pending_tree.exists()
            evidence['automatic_settlement'] = True
        return evidence


async def recovery(baseline, store=None):
    source = {}

    async def before(api):
        print('TREE_RECOVERY_EVIDENCE', api.root, flush=True)
        source['root'] = api.root
        if baseline:
            # Retirement restarts the same fixture before restoration.
            (api.root/'legacy-tree').touch()
        source['prefix'], source['body'] = await asyncio.to_thread(seed, api, store)
        await asyncio.to_thread(api.put, EDIT, 'before')

    async def after(api, sid, rid, proof):
        endpoint = f'/terminal/sessions/{sid}/recoveries/{rid}'
        selection = {'paths':[EDIT], 'semantics':'sandbox_tree_changes'}
        observations = {'passed':False, 'baseline':baseline, 'storage':'s3' if store else 'fs',
                        'actual_model_calls':0, 'mock_llm_calls':0}
        try:
            if not baseline:
                observations['preview'] = await preview_check(api,endpoint,selection)
            if store:
                observations['passed'] = True
                return 0
            assert await asyncio.to_thread(api.call,'POST',api.prefix+'/file-transactions:recover') == {'result':None}
            observations['empty_tree_recovery_http'] = 200
            initial = await asyncio.to_thread(api.call, 'POST', endpoint+'/restore-plan', selection)
            payload = {**selection,'request_id':str(uuid.uuid4()),'confirm':True,'plan_sha256':initial['plan_sha256']}
            with client(api) as http:
                (api.root/'observe-tree').touch()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(http.post, endpoint+'/restore', json=payload)
                    try:
                        observations['copy'] = await asyncio.to_thread(observe, api, lock_expected=baseline)
                        save = await asyncio.to_thread(http.put, api.prefix+'/file', json={'path':SAVE,'content':'independent save'})
                        observations['save_http'] = save.status_code
                        assert save.status_code == (409 if baseline else 200), save.text
                    finally:
                        (api.root/'release-tree').touch()
                    result = await asyncio.to_thread(pending.result, 100)
                observations['confirmed_old_plan_http'] = result.status_code
                assert result.status_code == (200 if baseline else 409), result.text
                if not baseline:
                    assert result.json()['detail']['code']=='recovery_plan_stale',result.text
                    assert (await asyncio.to_thread(http.get, api.prefix+'/raw', params={'path':EDIT})).text == 'before'
                    fresh = await asyncio.to_thread(api.call, 'POST', endpoint+'/restore-plan', selection)
                    payload.update(plan_sha256=fresh['plan_sha256'])
                    email = (await asyncio.to_thread(api.call, 'GET', '/auth/me'))['email']
                    (api.root/'exit-tree-restore').touch()
                    crash = await asyncio.to_thread(interrupt_and_restart, api, '/fixture/tree-owner', endpoint+'/restore',
                                                    payload, email, 'crash-first-rename', 84)
                    pending_tree = api.root/'files'/source['prefix']/'.vibeflow/tree-transactions/pending'
                    assert (pending_tree/'before/0/archive-large.bin').stat().st_size == source['body']['size']
                    await asyncio.to_thread(api.call, 'GET', endpoint+'/restores/'+payload['request_id'], expect=404)
                    observations['readonly_diagnosis'] = await inspect_or_recover_cold_tree(api,pending_tree,diagnose=True)
                    observations['cold_deadline'] = await recovery_deadline(api,pending_tree)
                    observations['cold_read'] = await reject_changed_recovery(api,pending_tree)
                    await asyncio.to_thread(api.call, 'POST', api.prefix+'/file-transactions:recover')
                    assert not pending_tree.exists()
                    assert await asyncio.to_thread(api.call, 'GET', api.prefix+'/raw?path='+EDIT, raw=True)==b'before'
                    observations['cold_rollback'] = crash
                    (api.root/'exit-tree-settlement').touch()
                    observations['committed_recovery'] = await asyncio.to_thread(interrupt_and_restart,api, '/fixture/tree-owner',
                        endpoint+'/restore',payload,email,'crash-before-settlement',85)
                    assert pending_tree.exists()
                    observations['automatic_cold_read'] = await inspect_or_recover_cold_tree(api,pending_tree,diagnose=False)
                    assert not pending_tree.exists()
                    assert await asyncio.to_thread(api.call, 'GET', api.prefix+'/raw?path='+EDIT, raw=True)==b'after'
                    assert await asyncio.to_thread(api.call, 'GET', api.prefix+'/raw?path='+SAVE, raw=True)==b'independent save'
                    with client(api) as reader:
                        assert await asyncio.to_thread(digest_response,reader,api.case_id,LARGE)==source['body']
                    replay = await asyncio.to_thread(api.call, 'POST', endpoint+'/restore', payload)
                    assert replay['replayed'] and replay['file_transaction_id']
                    assert not list((api.root/'files'/'.tree-transaction-staging').glob('*/owner.json'))
                    observations['retry_and_replay'] = True
            observations['passed'] = True
            return 0
        finally:
            (api.root/'local-tree-proof.json').write_text(json.dumps(observations, indent=2), encoding='utf8')

    try:
        await verify_retirement(store, fixture_prefix=FIXTURE, before_sandbox=before,
            sandbox_command=f"printf '%s' after > {EDIT}; ", after_retirement=after)
    finally:
        if store and 'root' in source:
            (source['root']/'retirement_fixture.py').unlink(missing_ok=True)


async def normal_export():
    api = IsolatedServer('sandbox-recovery-ui-tree-export', app='tree_fixture:app')
    (api.root/'tree_fixture.py').write_text(FIXTURE, encoding='utf8')
    (api.root/'ready.json').write_text(json.dumps({'root':str(api.root)}), encoding='utf8')
    result = {'passed':False,'actual_model_calls':0,'mock_llm_calls':0}
    print('TREE_EXPORT_EVIDENCE',api.root,flush=True)
    try:
        with api:
            api.create_case('Real WSL tree export')
            _, expected = seed(api, None)
            api.put(EDIT, 'before')
            original = api.call('GET',api.prefix+'/caseDict')['dict_revision']
            (api.root/'observe-tree').touch()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=230)) as session:
                async with session.ws_connect(api.base+'/ws/terminal',params={'token':api.token,'case':api.case_id,'shell':'wsl-sandbox'}) as ws:
                    ready = await ws.receive_json(timeout=90)
                    assert ready['type']=='ready', ready
                    await ws.send_json({'type':'input','data':f"printf '%s' after > {EDIT}; mkdir Ref/export-empty; "
                        "printf '\\n// actual local tree export\\n' >> 0-caseDict/caseDict; exit\r"})
                    try:
                        result['copy'] = await asyncio.to_thread(observe, api, lock_expected=False)
                    finally:
                        (api.root/'release-tree').touch()
                    async with asyncio.timeout(100):
                        async for message in ws:
                            if message.type==aiohttp.WSMsgType.TEXT:
                                event = json.loads(message.data)
                                assert event.get('type')!='error',event
                                if event.get('type')=='exit':
                                    assert event['code']==0,event
                                    break
                        else:
                            raise AssertionError('Terminal closed before delivering successful export')
            state = api.call('GET','/terminal/sessions/'+ready['session'])
            assert state['cleanup_verified'] is True, state
            assert not (api.root/'archive-under-lock.txt').exists()
            dictionary = api.call('GET',api.prefix+'/caseDict')
            assert dictionary['dict_revision']==original+1 and 'actual local tree export' in dictionary['content']
            assert api.call('GET',api.prefix+'/raw?path='+EDIT,raw=True)==b'after'
            assert any(row['path']=='Ref/export-empty' and row.get('kind')=='directory'
                       for row in api.call('GET',api.prefix+'/tree')['entries'])
            with client(api) as http:
                assert digest_response(http,api.case_id,LARGE)==expected
            result.update(passed=True,verified_native_stop=True,readback=expected,
                          ordinary_hashes_under_lock=0,dict_revision_advanced=True)
    finally:
        await verify_cleanup(api.root)
        (api.root/'local-tree-proof.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--export',action='store_true')
    parser.add_argument('--s3-preview',action='store_true')
    args=parser.parse_args()
    if args.s3_preview:
        with ObjectStore(executable=RUNTIME,volume_max=24,authenticated=True) as store:
            asyncio.run(recovery(False, store))
    else:
        asyncio.run(normal_export() if args.export else recovery(args.baseline))
