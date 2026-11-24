"""Real files/processes and optional real S3 allocation admission checks."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading
from types import SimpleNamespace

from testsupport import SERVER,IsolatedServer


def configure(root,limit=1):
    os.environ.update(VIBEFLOW_STORAGE_FS_ROOT=str(root),VIBEFLOW_NODE_STORAGE_LIMIT_MB=str(limit),
        VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0',VIBEFLOW_STORAGE_BACKEND='fs')
    from app.config import get_settings
    get_settings.cache_clear()


def rejected(function,code='node_storage_full'):
    from app.services.case_write import CaseWriteConflict
    try:function()
    except CaseWriteConflict as error:assert error.code==code,(error.code,code)
    else:raise AssertionError('Allocation was incorrectly admitted')


def child(root):
    configure(root)
    from app.services.node_storage_budget import reserve
    with reserve(800*1024):
        (root/'partial.bin').write_bytes(b'p'*(200*1024))
        (root.parent/'ready').touch()
        while True:time.sleep(.05)


def stop(process):
    # A Windows venv launcher can have a different PID from its Python child.
    if process.poll() is None:
        if os.name=='nt':
            result=subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
            assert result.returncode==0,result.stderr
        else:process.kill()
    process.wait(timeout=10)


def core(report):
    from app.services import node_storage_budget as budget
    from app.services.private_cache import Cache
    checks=[]
    configure(report/'files')
    cache=Cache('.attempt-sources');cache.write('first.bin',b'a'*(700*1024))
    called=[]
    rejected(lambda:cache.fetch('second.bin',400*1024,lambda:called.append(True)))
    assert not called and not (cache.root/'second.bin').exists()
    assert (cache.root/'first.bin').stat().st_size==700*1024
    cache.write('small.bin',b'b'*(200*1024))
    cache.cleanup()
    assert not list((report/'files'/budget.DIRECTORY).glob('*.json'))
    checks.append('actual_retained_bytes_and_journal_overhead_reject_before_reader_then_smaller_write_succeeds')
    configure(report/'process-files')
    log=(report/'child.log').open('w',encoding='utf8')
    process=subprocess.Popen([sys.executable,'-X','utf8','-B',__file__,'--child',str(report/'process-files')],
        stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        deadline=time.monotonic()+15
        while not (report/'ready').exists():
            assert process.poll() is None,'Claim child exited; inspect child.log'
            assert time.monotonic()<deadline
            time.sleep(.02)
        rejected(lambda:budget._admit(400*1024))
        stop(process)
        assert (report/'process-files/partial.bin').stat().st_size==200*1024
        with budget.reserve(700*1024):pass
        rejected(lambda:budget._admit(900*1024))
        checks.append('live_peer_claim_blocks_overcommit_and_dead_peer_release_still_counts_partial_files')
    finally:
        if process.poll() is None:stop(process)
        log.close()
    configure(report/'unknown-files')
    with budget.reserve(800*1024):
        path=next((report/'unknown-files'/budget.DIRECTORY).glob('*.json'))
        original=path.read_bytes();row=json.loads(original);row['owner']['host_id']='f'*64
        path.write_text(json.dumps(row),encoding='utf8')
        try:rejected(lambda:budget._admit(400*1024))
        finally:path.write_bytes(original)
        row=json.loads(original);row['bytes']=-1;path.write_text(json.dumps(row),encoding='utf8')
        try:rejected(lambda:budget._admit(1),'node_storage_unavailable')
        finally:path.write_bytes(original)
    checks.append('unverified_owner_is_retained_and_corrupt_claim_fails_closed')
    configure(report/'free-space-files')
    disk_usage=budget.shutil.disk_usage
    budget.shutil.disk_usage=lambda _:SimpleNamespace(free=1)
    try:rejected(lambda:budget._admit(2))
    finally:budget.shutil.disk_usage=disk_usage
    checks.append('filesystem_free_space_is_checked_independently_of_configured_capacity')
    configure(report/'failure-files')
    cache=Cache('.case-read-views')
    def fail():raise OSError('injected network failure')
    try:cache.fetch('failed.bin',100,fail)
    except OSError:pass
    else:raise AssertionError('Expected reader failure')
    assert not list((report/'failure-files'/budget.DIRECTORY).glob('*.json'))
    assert not (cache.root/'failed.bin').exists()
    cache.cleanup();checks.append('failed_reader_releases_claim_after_io_scope_without_creating_payload')
    return {'passed':True,'count':len(checks),'checks':checks}


async def cancellation(report):
    from app.services import node_storage_budget as budget
    from app.services.private_cache import Cache
    from app.services.async_ops import finish_before_cancel
    configure(report/'cancel-files')
    cache=Cache('.attempt-sources');started=threading.Event();release=threading.Event()
    def reader():
        started.set()
        if not release.wait(10):raise TimeoutError('Cancellation test did not drain')
        return b'c'*(700*1024)
    task=asyncio.create_task(finish_before_cancel(asyncio.to_thread(cache.fetch,'input.bin',700*1024,reader)))
    try:
        deadline=time.monotonic()+5
        while not started.is_set():
            assert time.monotonic()<deadline and not task.done()
            await asyncio.sleep(.01)
        task.cancel();await asyncio.sleep(.05)
        assert not task.done()
        rejected(lambda:budget._admit(400*1024))
    finally:release.set()
    try:await task
    except asyncio.CancelledError:pass
    else:raise AssertionError('Cancellation was swallowed')
    assert (cache.root/'input.bin').stat().st_size==700*1024
    assert not list((report/'cancel-files'/budget.DIRECTORY).glob('*.json'))
    rejected(lambda:budget._admit(400*1024))
    cache.cleanup()
    return {'passed':True,'count':1,'checks':['cancelled_waiter_retains_claim_until_real_reader_and_disk_write_drain']}


EXTRA='''
import asyncio
from app.services import node_storage_budget as node_budget
from app.services.async_ops import finish_before_cancel
admit=node_budget._admit
def admitted(size):
 get_settings().node_storage_limit_mb=int((root/'node-limit').read_text())
 get_settings().node_storage_min_free_mb=0
 return admit(size)
node_budget._admit=admitted
prior_read=objects.read
def payload_read(prefix,path,**kwargs):
 if '/inputs/' in path:record('input_payload_download',path=path)
 return prior_read(prefix,path,**kwargs)
objects.read=payload_read
@app.post('/api/v1/fixture/hydrate/{case_id}')
async def hydrate(case_id:int,manifest:dict,db=Depends(get_db),user=Depends(get_current_user)):
 case=await get_owned_case(case_id,user,db)
 await finish_before_cancel(asyncio.to_thread(objects.ensure_inputs,case.s3_prefix,manifest))
 return {'hydrated':True}
'''


def api_for(store,peer=None):
    from seal_storage_budget_smoke import FIXTURE
    api=IsolatedServer('node-storage-budget',app='node_storage_fixture:app')
    settings=store.settings()
    if peer:
        settings.update({key:peer.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')})
        api.token=peer.token;api.case_id=peer.case_id;api.prefix=peer.prefix
    (api.root/'node_storage_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE+EXTRA,encoding='utf8')
    (api.root/'limit').write_text('16');(api.root/'node-limit').write_text('1');(api.root/'mode').write_text('trace')
    return api


def remote(store,report):
    from seal_storage_budget_smoke import trace
    checks=[]
    with api_for(store) as api:
        case=api.create_case('Node capacity');api.put('Ref/input.txt','x'*(2*1024*1024))
        prefix=api.call('GET',f"/fixture/prefix/{case['id']}")
        body={'command':'echo NODE_CAPACITY','input_paths':['Ref/input.txt']}
        error=api.call('POST',api.prefix+'/jobs',body,expect=409)
        assert error['detail']['code']=='node_storage_full',error
        assert not any(row['kind']=='source_download' for row in trace(api))
        assert api.call('GET',api.prefix+'/jobs')==[]
        checks.append('real_HTTP_S3_source_rejected_before_payload_download_or_job_commit')
        api.put('Ref/input.txt','x'*(700*1024));(api.root/'trace.jsonl').unlink()
        error=api.call('POST',api.prefix+'/jobs',body,expect=409)
        assert error['detail']['code']=='node_storage_full',error
        assert sum(row['kind']=='source_download' for row in trace(api))==1
        assert not list((api.root/'files'/prefix).rglob('manifest.json'))
        recovered=api.call('POST',f"/fixture/recover/{case['id']}")
        assert any(row['status']=='rolled_back' for row in recovered),recovered
        checks.append('two_sealed_local_copies_are_budgeted_and_failed_seal_recovers')
        (api.root/'node-limit').write_text('8')
        api.call('POST',api.prefix+'/jobs',body)
        manifest=json.loads((api.root/'sealed.json').read_text())
        with api_for(store,api) as peer:
            error=peer.call('POST',f"/fixture/hydrate/{case['id']}",manifest,expect=409)
            assert error['detail']['code']=='node_storage_full',error
            assert not any(row['kind']=='input_payload_download' for row in trace(peer))
            assert not list((peer.root/'files').rglob('.restoring-*'))
            (peer.root/'node-limit').write_text('8')
            assert peer.call('POST',f"/fixture/hydrate/{case['id']}",manifest)['hydrated']
            local=peer.root/'files'/prefix/manifest['inputs_path']/'Ref/input.txt'
            assert hashlib.sha256(local.read_bytes()).hexdigest()==manifest['files']['Ref/input.txt']['sha256']
            checks.append('fresh_node_retry_hydration_checks_two_copies_before_download_then_restores_real_digest')
    # Publication hydration uses the same node ledger, independently of jobs.
    from s3_publication_objects_smoke import fixture
    from app.config import get_settings
    from app import storage
    from app.services import artifact_publication as pub,attempt_workspace as workspace,publication_objects as bundles,attempt_objects as objects
    root,context=fixture(store,'node-publication')
    (root/'run/a.txt').write_bytes(b'p'*(2*1024*1024))
    configure(report/'publication-cache');os.environ.update(store.settings());get_settings.cache_clear();storage._client=None
    row=SimpleNamespace(**context['row']);records=workspace.inventory(root/'run',['a.txt','nested'])
    row.manifest_sha256=pub.prepare_bundle(context['prefix'],row,root/'run',records,context['descriptor'])
    observed=[];original=objects.read
    def observed_read(prefix,path,**kwargs):
        if '/files/' in path:observed.append(path)
        return original(prefix,path,**kwargs)
    objects.read=observed_read
    try:
        rejected(lambda:bundles.read_manifest(context['prefix'],row))
        assert not observed,'Publication payload was read before node admission'
        get_settings().node_storage_limit_mb=8
        folder,manifest=bundles.read_manifest(context['prefix'],row)
        assert (folder/'files/a.txt').read_bytes()==(root/'run/a.txt').read_bytes()
        assert observed and not list((report/'publication-cache'/'.node-storage-budget').glob('*.json'))
    finally:objects.read=original
    checks.append('publication_bundle_capacity_is_checked_before_payload_download_and_success_releases_claim')
    return {'passed':True,'count':len(checks),'checks':checks,'api':str(api.root)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--child',type=Path);parser.add_argument('--s3',action='store_true');parser.add_argument('--cancellation',action='store_true');args=parser.parse_args()
    if args.child:child(args.child);return
    report=Path(tempfile.mkdtemp(prefix='node-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
    result={'cancellation':asyncio.run(cancellation(report))} if args.cancellation else {'core':core(report),'cancellation':asyncio.run(cancellation(report))}
    (report/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('CORE_PASS '+str(report),flush=True)
    if args.s3:
        from s3testsupport import ObjectStore
        from s3_typed_publication_smoke import RUNTIME
        with ObjectStore(executable=RUNTIME) as store:result['s3']=remote(store,report)
    (report/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('NODE_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':main()
