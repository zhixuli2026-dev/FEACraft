"""Real exit, paged private cache recovery and real DeepSeek evidence readback."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys


FIXTURE = '''
import asyncio
import os
import time
from types import SimpleNamespace
from fastapi import Depends
from app.config import get_settings
from app.deps import get_current_user
from app.services import private_cache, storage_maintenance

private_cache.RECOVERY_PAGE_SIZE=2
enable=(root/'enable-cache-sweep').exists()
get_settings().storage_maintenance_enabled=enable
if enable:
    get_settings().storage_maintenance_interval_seconds=1
(root/'cache-api-pid').write_text(str(os.getpid()))

def note(value):
    with (root/'cache-sweep-trace.jsonl').open('a',encoding='utf8') as stream:
        stream.write(json.dumps(value)+'\\n')

private_cache.get_settings=lambda:SimpleNamespace(storage_measurement_timeout_seconds=.4,storage_measurement_max_entries=8)
original_page=private_cache.RecoverySweep.page
def page(self):
    rows=original_page(self)
    note({'event':'page','rows':rows,'complete':self.complete,'open_streams':len(self.streams)})
    return rows
private_cache.RecoverySweep.page=page
original_close=private_cache.RecoverySweep.close
def close(self):
    count=len(self.streams)
    original_close(self)
    note({'event':'closed','open_streams_before':count,'open_streams_after':len(self.streams)})
private_cache.RecoverySweep.close=close

original_accounts=storage_maintenance.maintain_accounts
async def accounts(stop):
    await original_accounts(stop)
    note({'event':'accounts_completed'})
storage_maintenance.maintain_accounts=accounts

original_read=private_cache._fingerprint_path
def read(path,limit,deadline):
    if path.name=='slow.bin' and deadline is not None and not (root/'proof-was-delayed').exists():
        (root/'proof-was-delayed').touch()
        time.sleep(.5)
    return original_read(path,limit,deadline)
private_cache._fingerprint_path=read

@app.post('/api/v1/testing/private-cache-fixtures')
async def seed(user=Depends(get_current_user)):
    def cache(kind,paths):
        candidate=private_cache.Cache(kind)
        for path,payload in paths.items():candidate.write(path,payload)
        return candidate
    read_view, attempt='.case-read-views','.attempt-sources'
    healthy=cache(attempt,{'nested/verifiable.txt':b'complete old attempt cache'})
    healthy.directories(['empty'])
    slow=cache(read_view,{'slow.bin':b'known bytes after eventual proof'})
    changed=cache(attempt,{'changed.bin':b'original'})
    (changed.root/'changed.bin').write_bytes(b'changed!')
    unknown=cache(read_view,{'verified.bin':b'matching receipt'})
    (unknown.root/'unregistered.bin').write_bytes(b'no ownership proof')
    partial=cache(attempt,{'complete.txt':b'last complete file'})
    with (partial.container/'files.jsonl').open('ab') as file:file.write(b'{"path":"incomplete')
    large=cache(read_view,{f'{i}.txt':b'x' for i in range(10)})
    user_paths={'healthy':healthy.container,'slow':slow.container,'changed':changed.container,
                'unknown':unknown.container,'partial':partial.container,'large':large.container}
    for kind in (read_view,attempt):
        for i in range(3):
            legacy=private_cache.parent_for(kind)/('legacy-'+str(i))
            legacy.mkdir();(legacy/'preserve.txt').write_bytes(b'untracked directory')
    foreground=cache(attempt,{f'{i}.txt':b'current writer' for i in range(10)})
    assert foreground.cleanup()['status']=='removed' and not foreground.container.exists()
    return {'paths':{name:str(path) for name,path in user_paths.items()},'foreground_removed':True}

@app.post('/api/v1/testing/private-cache:stop')
async def stop_maintenance(user=Depends(get_current_user)):
    app.state.storage_maintenance_stop.set()
    task=app.state.storage_maintenance_task
    task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    return {'stopped':task.done()}
'''


LIVE_WRITER = '''
from pathlib import Path
import sys,time
from app.services.private_cache import Cache
cache=Cache('.case-read-views')
cache.write('live.txt',b'live owner, keep this cache')
Path(sys.argv[1]).write_text(str(cache.container),encoding='utf8')
time.sleep(125)
'''


def scenario(api, **_):
    from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
    from live_llm_outbox_restart_acceptance import restart
    from testsupport import SERVER

    setup = api.call('POST','/testing/private-cache-fixtures')
    assert setup['foreground_removed']
    fixtures = {key:Path(path) for key,path in setup['paths'].items()}
    owner = json.loads((fixtures['healthy']/'owner.json').read_text(encoding='utf8'))['owner']
    stable = {name: {path.relative_to(fixtures[name]).as_posix():path.read_bytes() for path in fixtures[name].rglob('*') if path.is_file()}
              for name in ('changed','unknown','partial','large')}
    handle = api.root/'active-cache-path'
    environment = {**api.env,'PYTHONPATH':str(SERVER)+os.pathsep+api.env['PYTHONPATH']}
    with (api.root/'cache-writer.log').open('wb') as output:
        child = subprocess.Popen([sys.executable,'-B','-c',LIVE_WRITER,str(handle)],cwd=api.root,env=environment,
                                 stdout=output,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            wait_for(handle.exists,'Actual live cache writer did not start',12)
            live_path = Path(handle.read_text(encoding='utf8'))
            assert child.poll() is None and (live_path/'data/live.txt').read_bytes() == b'live owner, keep this cache'
            (api.root/'enable-cache-sweep').touch()
            kill_owned_api(api,int((api.root/'cache-api-pid').read_text()))
            from app.services import process_identity_native as identity
            assert identity.probe(owner)['state']=='exited'
            restart(api)

            def trace():
                path = api.root/'cache-sweep-trace.jsonl'
                return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line] if path.exists() else []

            def reviewed(path,status):
                return any(row.get('name')==path.name and row['status']==status
                           for page in trace() if page['event']=='page' for row in page['rows'])

            wait_for(lambda: reviewed(live_path,'active') and reviewed(fixtures['slow'],'deferred'),
                     'Startup sweep did not exercise both the live owner and read timeout',30)
            wait_for(lambda: all(reviewed(fixtures[name],'removed') and not fixtures[name].exists() for name in ('healthy','slow')),
                     'Later maintenance did not finish the verifiable exited caches',30)
            assert child.poll() is None and live_path.exists()
            observed = trace()
            assert all(len(row['rows']) <= 2 for row in observed if row['event']=='page')
            assert [row['kind'] for row in observed[0]['rows']]==['.attempt-sources','.case-read-views'], observed[0]
            assert [row['event'] for row in observed[:3]]==['page','accounts_completed','page'], observed[:3]
            for name in stable:
                assert reviewed(fixtures[name],'recovery_required'), name
                assert {p.relative_to(fixtures[name]).as_posix():p.read_bytes() for p in fixtures[name].rglob('*') if p.is_file()} == stable[name]
            for kind in ('.case-read-views','.attempt-sources'):
                container = Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/kind
                assert all((container/f'legacy-{index}'/'preserve.txt').read_bytes()==b'untracked directory' for index in range(3))
        finally:
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=10)
    wait_for(lambda: reviewed(live_path,'removed') and not live_path.exists(),
             'The previously live cache was not recovered after its real process exited',20)
    wait_for(lambda: [row for row in trace() if row['event']=='page'][-1]['open_streams']>0,
             'No live directory cursor to exercise shutdown',10)
    assert api.call('POST','/testing/private-cache:stop')['stopped']
    closing, = [row for row in trace() if row['event']=='closed']
    assert closing['open_streams_before']>0 and closing['open_streams_after']==0, closing
    result = {'pages':trace(),'page_size_fixture':2,'initial_api_owner_closed':True,'active_child_exited':child.poll() is not None,
              'preserved_cache_types':list(stable),'unstamped_legacy_preserved':6,'account_maintenance_between_pages':True,
              'delayed_real_cache_read_revisited':True,'foreground_cleanup_above_background_limit':True,
              'live_cursors_closed_on_stop':closing['open_streams_before']}
    (api.root/'private-cache-maintenance.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__ == '__main__':
    from live_llm_job_repeat_acceptance import main

    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=6))
