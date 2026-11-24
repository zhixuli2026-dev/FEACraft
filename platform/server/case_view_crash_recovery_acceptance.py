"""Recover bound streaming stages after real API loss with authenticated SeaweedFS."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import time

from case_read_view_streaming_acceptance import ASSET, FIXTURE, RUNTIME, SIZE, install, source_file, stop_api
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


RECOVERY = r'''
@app.post('/fixture/view:recover')
async def recover(user=Depends(get_current_user)):
    from app.services import private_cache, node_storage_budget
    return {'rows':await asyncio.to_thread(private_cache.recover),
            'reserved_bytes':(await asyncio.to_thread(node_storage_budget.observe))['reserved_bytes']}
'''


def configure(api, settings):
    module = api.root/'case_recovery_fixture.py'
    module.write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE+RECOVERY,encoding='utf-8')
    return module


def stage_paths(parent):
    return sorted(path for path in parent.glob('owned-*/stream-*.part') if path.stat().st_size > 0)


def view(api, case_id, mode):
    return api.call('POST',f'/fixture/view/{case_id}',{'paths':[ASSET],'mode':mode},api_prefix=False)


def objects(container):
    return {path.relative_to(container).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
            for path in container.rglob('*') if path.is_file()}


def abort_during_read(api, case_id, process, report):
    from app.services import process_identity_native

    cache_parent = (api.root/'files' / '.case-read-views').resolve()
    with ThreadPoolExecutor(max_workers=3) as readers:
        opened = [readers.submit(view,api,case_id,'replace') for _ in range(3)]
        try:
            deadline = time.monotonic()+11
            while time.monotonic()<deadline:
                stages = stage_paths(cache_parent)
                if len(stages)==3 or any(future.done() for future in opened):
                    break
                time.sleep(.03)
            assert len(stages)==3 and not any(future.done() for future in opened)
            receipts = []
            for stage in stages:
                owner = json.loads((stage.parent/'owner.json').read_text(encoding='utf-8'))
                journal = (stage.parent/'files.jsonl').read_text(encoding='utf-8').splitlines()
                records = [json.loads(line) for line in journal]
                assert owner['owner']==process and stage.resolve().is_relative_to(cache_parent)
                assert len(records)==1 and records[0]['stage'] is True
                assert records[0]['path']==stage.name and records[0]['size']==SIZE
                assert len(records[0]['file_id'])==64 and 0<stage.stat().st_size<SIZE
                receipts.append({'cache':stage.parent.name,'bytes_durable_before_stop':stage.stat().st_size,
                                 'journal_record':records[0]})
            active = api.call('POST','/fixture/view:recover',api_prefix=False)
            assert len(active['rows'])==3 and {row['status'] for row in active['rows']}=={'active'},active
            assert active['reserved_bytes']>=3*SIZE,active
            report.update({'stages':receipts,'live_recovery':active})
            assert process_identity_native.probe(process)['state']=='match'
            os.kill(process['pid'],signal.SIGTERM if os.name=='nt' else signal.SIGKILL)
            api.proc.wait(timeout=10)
            report['lost_api_exit'] = process_identity_native.probe(process)
            assert report['lost_api_exit']['state']=='exited'
            for future in opened:
                try:
                    future.result(timeout=10)
                except OSError:
                    pass
                else:
                    raise AssertionError('Interrupted case view still returned a successful HTTP request')
        finally:
            (api.root/'copy-release').touch()
    assert len(stage_paths(cache_parent))==3
    return stages


def run():
    from app.services import process_identity_native

    api = IsolatedServer('case-view-process-loss',app='case_recovery_fixture:app')
    report = {'passed':False,'real_model_requests':0,'mock_llm_calls':0}
    print('CASE_VIEW_PROCESS_LOSS '+str(api.root),flush=True)
    try:
        with ObjectStore(executable=RUNTIME,volume_max=12,authenticated=True) as store:
            module = configure(api,store.settings())
            try:
                with api:
                    case = api.create_case('Case view with physical stage recovery')
                    identifier = case['id']
                    binding = api.call('GET',f'/fixture/view/{identifier}/binding',api_prefix=False)
                    process = binding['process']
                    try:
                        asset,sha256 = source_file(api.root)
                        try:
                            install(api,store,binding['prefix'],ASSET,asset)
                        finally:
                            asset.unlink()
                        stages = abort_during_read(api,identifier,process,report)
                    finally:
                        if process_identity_native.probe(process)['state']=='match':
                            stop_api(api,process)
            finally:
                module.unlink(missing_ok=True)
            original, replaced, unbound = stages
            kept = api.root/'original-stage-retained-by-fixture.bin'
            replaced.rename(kept)
            with kept.open('rb') as source, replaced.open('xb') as destination:
                shutil.copyfileobj(source,destination)
            assert kept.read_bytes()==replaced.read_bytes() and not os.path.samefile(kept,replaced)
            (unbound.parent/'files.jsonl').write_bytes(b'')
            displaced_before = objects(replaced.parent)
            unbound_before = objects(unbound.parent)

            peer = IsolatedServer('case-view-recovery-peer',app='case_recovery_fixture:app')
            saved = {key:api.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')}
            peer_module = configure(peer,{**saved,**store.settings()})
            peer.token = api.token
            try:
                with peer:
                    actual = peer.call('GET',f'/fixture/view/{identifier}/binding',api_prefix=False)['process']
                    try:
                        assert actual!=process
                        recovered = peer.call('POST','/fixture/view:recover',api_prefix=False)
                        by_name = {row['name']:row['status'] for row in recovered['rows']}
                        assert by_name=={original.parent.name:'removed',replaced.parent.name:'recovery_required',
                                         unbound.parent.name:'recovery_required'},recovered
                        assert recovered['reserved_bytes']==0 and not original.parent.exists()
                        assert objects(replaced.parent)==displaced_before and objects(unbound.parent)==unbound_before
                        fresh = view(peer,identifier,'measure')
                        assert fresh['entered'] and fresh['root_is_private_cache']
                        assert fresh['files']==fresh['journal']==[{'path':ASSET,'size':SIZE,'sha256':sha256}]
                        assert len(fresh['stage_journal'])==1 and fresh['reserved_bytes']==0
                        assert fresh['remaining_cache_containers']==2
                        repeated = peer.call('POST','/fixture/view:recover',api_prefix=False)
                        assert len(repeated['rows'])==2 and {row['status'] for row in repeated['rows']}=={'recovery_required'}
                        report.update({'restarted_recovery':recovered,'repeat_recovery':repeated,
                            'fresh_view':fresh,'replaced_same_bytes_preserved':True,'unbound_bytes_preserved':True})
                    finally:
                        stop_api(peer,actual)
                report['replacement_api_exit'] = process_identity_native.probe(actual)
                assert report['replacement_api_exit']['state']=='exited'
            finally:
                peer_module.unlink(missing_ok=True)
        assert store.proc.poll() is not None
        report.update({'s3_stopped':True,'s3_receipt':str(store.root/'store.json'),'passed':True})
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__,'message':str(error)[:1200]}
        raise
    finally:
        paths = (Path(__file__).resolve(),SERVER/'case_read_view_streaming_acceptance.py',
                 SERVER/'app/services/private_cache.py',SERVER/'app/services/case_read_view.py',
                 SERVER/'app/services/attempt_objects.py')
        report['source_sha256'] = {path.relative_to(SERVER).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in paths}
        (api.root/'case-view-process-loss.json').write_text(
            json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('CASE_VIEW_PROCESS_LOSS_OK '+str(api.root),flush=True)


if __name__=='__main__':
    run()
