"""Exercise completion of real uploads and measure concurrent SQL progress."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time

import httpx

from live_llm_job_repeat_acceptance import main
from live_llm_outbox_restart_acceptance import restart
from s3testsupport import ObjectStore
from testsupport import SERVER


FIXTURE = r'''
import os
import time
from pathlib import Path
from fastapi import Depends
from app.config import get_settings
from app.db import SessionLocal
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import durable_events, file_transactions, s3_uploads, s3_file_transactions, s3_staging_transactions, uploads
from app.services.storage_admission import lock_owner

root = Path(__file__).parent

def pause(name):
    ready, release = (root / (name + '-' + suffix) for suffix in ('ready','release'))
    if not (root / (name + '-armed')).exists() or ready.exists():
        return
    ready.touch()
    end = time.monotonic() + 45
    while not release.exists():
        if time.monotonic() >= end:
            raise TimeoutError('Upload observation not released')
        time.sleep(.025)

original_snapshot = s3_uploads.snapshot
def observed_snapshot(prefix, path, **kwargs):
    if '/chunks/' in path and path.endswith('/data') and kwargs.get('output') is not None:
        pause('assembly')
    return original_snapshot(prefix, path, **kwargs)
s3_uploads.snapshot = observed_snapshot

original_capture = file_transactions._capture
def observed_capture(path, *args, **kwargs):
    if path.name == 'data.part' and 'uploads' in path.parts and 'assembly' not in path.parts:
        pause('publication' if args or kwargs.get('destination') is not None else 'assembly')
    return original_capture(path, *args, **kwargs)
file_transactions._capture = observed_capture

original_after = s3_file_transactions._prepare_after
def observed_after(prefix, path, data, metadata):
    if getattr(data, 'relative', '').endswith('/data.part'):
        pause('publication')
    return original_after(prefix, path, data, metadata)
s3_file_transactions._prepare_after = observed_after

original_cleanup = s3_uploads.cleanup_completed
def observed_cleanup(prefix, identifier, receipt):
    pause('cleanup')
    if (root/'crash-after-commit').exists():
        os._exit(81)
    return original_cleanup(prefix, identifier, receipt)
s3_uploads.cleanup_completed = observed_cleanup

original_commit = s3_staging_transactions.commit_locked
async def observed_commit(db, case, document):
    if (root/'crash-before-commit').exists():
        os._exit(79)
    return await original_commit(db,case,document)
s3_staging_transactions.commit_locked = observed_commit

if hasattr(uploads, '_observe_completed_part'):
    original_local_cleanup = uploads._observe_completed_part
    def observed_local_cleanup(prefix, identifier, receipt):
        if (root/'crash-after-commit').exists():
            os._exit(81)
        pause('cleanup')
        return original_local_cleanup(prefix, identifier, receipt)
    uploads._observe_completed_part = observed_local_cleanup

original_append = durable_events.append_event
async def observed_append(db, case_id, kind, *args, **kwargs):
    if kind == uploads.COMPLETED_EVENT and not s3_uploads.storage._use_s3() and (root/'crash-before-commit').exists():
        os._exit(79)
    return await original_append(db, case_id, kind, *args, **kwargs)
durable_events.append_event = observed_append

@app.post('/api/v1/testing/upload-completion-lock/{case_id}')
async def commit_other_case(case_id:int, user=Depends(get_current_user)):
    async with SessionLocal() as db:
        await lock_owner(db, user.id)
        case = await get_owned_case(case_id,user,db)
        case.name += '.'
        await db.commit()
        return {'name':case.name}

@app.post('/api/v1/testing/upload-node-limit/{limit}')
async def set_node_limit(limit:int, user=Depends(get_current_user)):
    settings = get_settings()
    previous = settings.node_storage_limit_mb
    settings.node_storage_limit_mb = limit
    return {'previous':previous}
'''


def wait_for(path, future):
    deadline = time.monotonic() + 40
    while not path.exists():
        if future.done():
            response = future.result()
            raise AssertionError(('Upload did not reach pause', path.name, response.status_code, response.text[:400]))
        if time.monotonic() >= deadline:
            raise TimeoutError(path.name)
        time.sleep(.05)


def scenario(api, *, store=None, **_):
    baseline = os.environ.get('VIBEFLOW_UPLOAD_COMPLETION_BASELINE') == '1'
    api.create_case('Completed upload with concurrent SQL writes')
    prefix = api.call('GET',api.prefix)['storage_prefix']
    workspace = api.call('GET','/workspaces')[0]['id']
    other = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Other case of the same account'})['id']
    large = store is None and os.environ.get('VIBEFLOW_UPLOAD_COMPLETION_LOCAL_LARGE') == '1'
    size_mib = 40 if large else 2
    payload = b''.join(bytes([65+i])*(1024*1024) for i in range(size_mib))
    step = (8 if large else 1)*1024*1024
    sha = hashlib.sha256(payload).hexdigest()
    path = 'Ref/completed-upload.bin'
    with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                      timeout=120, trust_env=False) as client:
        def upload(target):
            initial = client.post(api.prefix+'/uploads',json={'path':target,'size':len(payload),'sha256':sha})
            initial.raise_for_status()
            route = api.prefix+'/uploads/'+initial.json()['id']
            for offset in range(0, len(payload), step):
                response = client.put(route,params={'offset':offset},content=payload[offset:offset+step])
                response.raise_for_status()
            return route

        route = upload(path)
        capacity = None
        if large:
            previous = client.post('/testing/upload-node-limit/96').json()['previous']
            try:
                denied = client.post(route+':complete')
                assert denied.status_code==409 and denied.json()['detail']['code']=='node_storage_full', denied.text
                assert client.get(api.prefix+'/raw',params={'path':path}).status_code==404
                assert client.get(route).json()['offset']==len(payload)
                capacity = denied.json()['detail']['code']
            finally:
                client.post(f'/testing/upload-node-limit/{previous}').raise_for_status()
        phases = ['assembly'] if baseline else ['assembly','publication','cleanup']
        for phase in phases:
            (api.root/(phase+'-armed')).touch()
        observed = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            completion = pool.submit(client.post,route+':complete')
            try:
                for phase in phases:
                    wait_for(api.root/(phase+'-ready'),completion)
                    assert not completion.done()
                    competing = client.post(f'/testing/upload-completion-lock/{other}',timeout=12)
                    assert competing.status_code == (409 if baseline else 200), (phase,competing.status_code,competing.text)
                    observed[phase] = competing.status_code
                    if phase == 'assembly' and not baseline and store is not None:
                        cancel = client.delete(route)
                        assert cancel.status_code==409 and cancel.json()['detail']['code']=='file_sync_busy', cancel.text
                    (api.root/(phase+'-release')).touch()
            finally:
                for phase in phases:
                    (api.root/(phase+'-release')).touch()
            result = completion.result(timeout=120)
            result.raise_for_status()
        replay = client.post(route+':complete')
        replay.raise_for_status()
        assert result.json() == replay.json() and result.json()['sha256']==sha and not result.json().get('cleanup_pending')
        downloaded = client.get(api.prefix+'/raw',params={'path':path})
        assert downloaded.status_code==200 and downloaded.content==payload
        exits = {}
        if os.environ.get('VIBEFLOW_UPLOAD_COMPLETION_CRASH') == '1':
            for flag, expected in [('crash-before-commit',79),('crash-after-commit',81)]:
                target = 'Ref/'+flag+'.bin'
                lost = upload(target)
                switch = api.root/flag
                switch.touch()
                former = api.proc
                try:
                    client.post(lost+':complete')
                except httpx.TransportError:
                    assert former.wait(timeout=15)==expected
                else:
                    raise AssertionError('Upload did not exit at '+flag)
                finally:
                    switch.unlink()
                assembly = api.root/'files'/prefix/'.vibeflow/uploads'/lost.rsplit('/',1)[-1]/'assembly'
                orphan = None
                if flag == 'crash-before-commit' and store is not None:
                    orphan, = assembly.iterdir()
                    assert (orphan/'owner.json').is_file() and (orphan/'data.part').stat().st_size==len(payload)
                    unowned = assembly/('0'*32)
                    unowned.mkdir()
                    (unowned/'keep.bin').write_bytes(b'unowned staging stays intact')
                if flag == 'crash-before-commit' and store is None:
                    journal = api.root/'files'/prefix/'.vibeflow/file-transactions/pending/manifest.json'
                    assert journal.is_file()
                restart(api)
                assert api.proc.pid != former.pid
                try:
                    retry = client.post(lost+':complete')
                except httpx.TransportError:
                    raise AssertionError('Isolated API exited again during upload retry') from None
                retry.raise_for_status()
                assert retry.json()['sha256']==sha and not retry.json().get('cleanup_pending')
                observed_file = client.get(api.prefix+'/raw',params={'path':target})
                assert observed_file.content==payload and observed_file.status_code==200
                assert client.post(lost+':complete').json()==retry.json()
                if orphan is not None:
                    assert not orphan.exists() and (unowned/'keep.bin').read_bytes()==b'unowned staging stays intact'
                exits[flag]=expected
        cancel_won = target_race_rejected = False
        if not baseline and store is None and not large:
            cancel_target = 'Ref/cancel-during-local-read.bin'
            cancellable = upload(cancel_target)
            (api.root/'assembly-ready').unlink()
            (api.root/'assembly-release').unlink()
            with ThreadPoolExecutor(max_workers=2) as pool:
                requested = pool.submit(client.post,cancellable+':complete')
                try:
                    wait_for(api.root/'assembly-ready',requested)
                    cancelled = client.delete(cancellable)
                    cancelled.raise_for_status()
                    assert cancelled.json()['cancelled']
                finally:
                    (api.root/'assembly-release').touch()
                assert requested.result(timeout=45).status_code in {404,409}
            assert client.get(api.prefix+'/raw',params={'path':cancel_target}).status_code==404
            cancel_won = True

            race_target = 'Ref/write-during-local-copy.bin'
            competing = upload(race_target)
            (api.root/'publication-ready').unlink()
            (api.root/'publication-release').unlink()
            with ThreadPoolExecutor(max_workers=2) as pool:
                requested = pool.submit(client.post,competing+':complete')
                try:
                    wait_for(api.root/'publication-ready',requested)
                    saved = client.put(api.prefix+'/file',json={'path':race_target,'content':'concurrent file version'})
                    saved.raise_for_status()
                finally:
                    (api.root/'publication-release').touch()
                reply = requested.result(timeout=45)
                assert reply.status_code==409 and reply.json()['detail']['code']=='file_conflict',reply.text
            assert client.get(api.prefix+'/raw',params={'path':race_target}).content==b'concurrent file version'
            assert client.get(competing).json()['offset']==len(payload)
            target_race_rejected = True
    receipt={'baseline':baseline,'competing_write_status':observed,'bytes':len(payload),'result_sha256':sha,
        'identical_replay':True,'target_read_back':True,'api_exit_restarts':exits,'local_cancel_won':cancel_won,
        'local_target_race_rejected':target_race_rejected,'low_node_capacity':capacity}
    (api.root/'upload-completion-lock.json').write_text(json.dumps(receipt,indent=2),encoding='utf8')
    return receipt


if __name__ == '__main__':
    executable = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    fixture, verify, volumes = FIXTURE, scenario, 8
    if os.environ.get('VIBEFLOW_UPLOAD_COMPLETION_ASSEMBLY_BUDGET') == '1':
        from s3_upload_assembly_budget_smoke import FIXTURE as BUDGET_FIXTURE, exercise

        fixture = BUDGET_FIXTURE + '''
from app.config import get_settings
get_settings().node_storage_limit_mb = 192
get_settings().node_storage_min_free_mb = 0
'''
        volumes = 24

        def verify(api, *, store, **_):
            report = {}
            exercise(api, store, report, baseline=False)
            (api.root/'upload-assembly-budget.json').write_text(json.dumps(report,indent=2),encoding='utf8')
            return report

    asyncio.run(main(selected_override=verify, fixture_override=fixture, store_volume_max=volumes,
        store_factory=lambda: ObjectStore(executable=executable,volume_max=volumes,authenticated=True)))
