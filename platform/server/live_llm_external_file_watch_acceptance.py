"""Actual external writes, native notifications, shared observation and replay."""
import asyncio
from contextlib import AsyncExitStack, closing
from functools import partial
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.parse import quote

import aiohttp
from botocore.exceptions import ClientError
import httpx

from live_llm_event_topics_acceptance import buffered, receive
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main
from live_llm_publication_restart_acceptance import worker_peer


FIXTURE = '''
from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import case_file_watch
import watchfiles

if case_file_watch.native_enabled():
    # A 60-second fallback cannot explain a notification in the 10-second
    # deadline. The Rust watcher and its real notifications stay active.
    case_file_watch.POLL_SECONDS = 60
    real_awatch = watchfiles.awatch
    async def observed_watch(*args, **kwargs):
        async for events in real_awatch(*args, **kwargs):
            with (root/'native-watch-events.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps(sorted([int(kind), str(path)] for kind, path in events))+'\\n')
            yield events
    watchfiles.awatch = observed_watch

@app.get('/api/v1/testing/file-observer/{case_id}')
async def state(case_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    case = await get_owned_case(case_id, user, db, write=False)
    monitor = case_file_watch._monitors.get(case_id)
    return {'prefix': case.s3_prefix, 'clients': monitor.clients if monitor else 0,
            'native': monitor.native if monitor else False}
'''


def scenario(api, *, configuration, network_peer=False, store=None, **_):
    route = api.prefix
    state_route = f'/testing/file-observer/{api.case_id}'
    prefix = api.call('GET', state_route)['prefix']
    relative = 'Ref/external-watch.txt'
    edited = 'Ref/offline-external-watch.txt'
    before = 'watch'+uuid.uuid4().hex
    after = 'watch'+uuid.uuid4().hex
    index_path = Path(api.env['VIBEFLOW_INDEX_ROOT'])/f'case-{api.case_id}.sqlite'
    assert api.call('POST', route+'/index:rebuild', {})['ok']
    stranger = api.call('POST', '/auth/register', {'name': 'Foreign file watcher',
        'email': uuid.uuid4().hex+'@example.com', 'password': 'file-watch-guest-123'})['token']
    report = {'backend': 's3' if store else 'fs', 'mutations': {}}
    versions = {}
    overrides = {}
    if network_peer:
        assert store is None and sys.platform == 'win32'
        local = Path(api.env['VIBEFLOW_STORAGE_FS_ROOT']).resolve(strict=True)
        network = Path('\\\\127.0.0.1\\'+local.drive[0]+'$').joinpath(*local.parts[1:])
        assert network.is_dir() and (network/prefix/'Ref').is_dir()
        overrides.update(VIBEFLOW_STORAGE_FS_ROOT=str(network), VIBEFLOW_FILE_WATCH_NATIVE='false')
        report['network_backend'] = 'Windows SMB loopback'

    def external(path, data, *, network_write=False):
        if store is not None:
            args = {'Bucket':store.bucket, 'Key':prefix+'/'+path}
            condition = {'IfMatch':versions[path]} if path in versions else {'IfNoneMatch':'*'}
            deadline = time.monotonic()+12
            while True:
                try:
                    if data is None:
                        store.client.delete_object(**args, **condition)
                        versions.pop(path)
                    else:
                        result = store.client.put_object(**args, Body=data.encode(), **condition)
                        versions[path] = result['ETag']
                    return
                except ClientError as error:
                    if error.response['ResponseMetadata']['HTTPStatusCode'] != 412 or time.monotonic() >= deadline:
                        raise
                    time.sleep(.10)  # Retry with the original object precondition.
        files_root = overrides['VIBEFLOW_STORAGE_FS_ROOT'] if network_write else api.env['VIBEFLOW_STORAGE_FS_ROOT']
        target = Path(files_root)/prefix/path
        subprocess.run([sys.executable, '-I', '-B', '-c',
            'from pathlib import Path; import sys; p=Path(sys.argv[1]); '
            'p.unlink() if sys.argv[2]=="delete" else p.write_text(sys.argv[3],encoding="utf-8")',
            str(target),'delete' if data is None else 'write',data or ''], check=True, timeout=12)

    def events():
        return api.call('GET', route+'/workflow-events?after=0&limit=200')['events']

    def cache(term, path):
        with closing(sqlite3.connect(index_path.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
            rows = conn.execute('SELECT path FROM file_fts WHERE file_fts MATCH ?', (term,)).fetchall()
        return rows == [(path,)] if path else not rows

    def agent_write():
        path = 'Ref/actual-agent-watch.txt'
        marker = 'livewatch'+uuid.uuid4().hex[:10]
        profile = api.call('POST','/provider-profiles',{'provider':'deepseek', 'name':'File watch live model',
            'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
        model = {'provider_profile_id':profile['id'],'model':configuration['model']}
        conv = api.call('POST','/conversations',{'case_id':api.case_id,'title':'Live watcher file mutation',
            'permission_mode':'full_access',**model})
        body = {'content':f'只调用一次 propose_file_update，写入路径 {path}，完整正文为 {marker}。'
                          '写入成功后只回复完成，不要再调用工具。',**model}
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                          timeout=90,trust_env=False) as client:
            with client.stream('POST',f'/conversations/{conv["id"]}/messages',json=body) as response:
                response.raise_for_status()
                frames = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith('data:')]
        result, = [row for row in frames if row['type']=='tool.result']
        assert result['ok'] and result['tool']=='propose_file_update'
        assert result['args']['path'] == path and result['args']['content'] == marker
        assert api.call('GET',route+'/raw?path='+quote(path),raw=True)==marker.encode()
        return path, marker

    async def verify(peer):
        async with aiohttp.ClientSession(trust_env=False) as client:
            def connect(server, token, cursor=None):
                params = {'case_id':api.case_id, 'token':token}
                if cursor is not None:
                    params['after'] = cursor
                return client.ws_connect(server.base+'/ws/events', params=params)

            try:
                forbidden = await connect(api, stranger)
            except aiohttp.WSServerHandshakeError as error:
                assert error.status == 403
            else:
                await forbidden.close()
                raise AssertionError('Foreign account subscribed to case file observations')

            def select(kind, path):
                return lambda event: event.get('source') == 'file_watch' and {'path':path, 'kind':kind} in event['changes']

            async def mutation(sockets, kind, path):
                receipts = await asyncio.gather(*(receive(s, 'files.changed', select(kind, path), seconds=18) for s in sockets))
                assert len({row['event_id'] for row in receipts}) == len({row['seq'] for row in receipts}) == 1
                assert all(row['case_id'] == api.case_id for row in receipts)
                report['mutations'][kind] = receipts[0]['event_id']
                return receipts[0]

            async with AsyncExitStack() as stack:
                primary = await stack.enter_async_context(buffered(await connect(api, api.token)))
                duplicate = await stack.enter_async_context(buffered(await connect(api, api.token)))
                ready = await asyncio.gather(*(receive(s,'ready') for s in (primary,duplicate)))
                native = ready[0]['native_notifications']
                assert all(row['native_notifications'] == native for row in ready)
                assert not native or store is None
                await asyncio.to_thread(wait_for, lambda: any(row.get('source')=='file_watch' and row.get('initial')
                    for row in events()), 'No observed baseline in the event log', 35)
                await asyncio.to_thread(wait_for, lambda: api.call('GET',state_route)['clients']==2,
                                      'Local listeners did not share one monitor')
                await asyncio.to_thread(external, relative, before)
                created = await mutation((primary,duplicate), 'created', relative)
                await asyncio.to_thread(wait_for, lambda: cache(before,relative),'No index update before a search',35)
                if native:
                    def signal():
                        log = api.root/'native-watch-events.jsonl'
                        return log.is_file() and 'external-watch.txt' in log.read_text(encoding='utf8')
                    await asyncio.to_thread(wait_for, signal,'Native watcher did not deliver a real notification',5)
                remote = await stack.enter_async_context(buffered(await connect(peer, api.token, created['seq']-1)))
                remote_ready = await receive(remote,'ready')
                assert remote_ready['native_notifications'] == (native and not network_peer)
                remote_created = await receive(remote,'files.changed',select('created',relative))
                assert remote_created['event_id'] == created['event_id']
                await asyncio.to_thread(external, relative, after)
                modified = await mutation((primary,duplicate,remote), 'modified', relative)
                assert modified['seq'] > created['seq']
                await asyncio.to_thread(wait_for, lambda: cache(after,relative) and cache(before,None),
                                        'No index update for same-length overwrite',35)
                await asyncio.to_thread(external, relative, None)
                deleted = await mutation((primary,duplicate,remote), 'deleted', relative)
                await asyncio.to_thread(wait_for, lambda: cache(after,None),'No index update for delete',35)
                assert deleted['seq'] > modified['seq']
                report.update(native=native, shared_local_clients=2, three_sockets_same_receipts=True)
                if network_peer:
                    agent_path, marker = await asyncio.to_thread(agent_write)
                    notices = await asyncio.gather(*(receive(socket,'files.changed',select('created',agent_path),seconds=18)
                        for socket in (primary,remote)))
                    assert notices[0]['event_id'] == notices[1]['event_id']
                    await asyncio.to_thread(wait_for,lambda: cache(marker,agent_path),'Agent file did not enter index',35)
                    report['agent_watch_event'] = notices[0]['event_id']

            await asyncio.to_thread(wait_for, lambda: api.call('GET',state_route)['clients']==0,
                                    'Last socket did not release its monitor',15)
            await asyncio.to_thread(external, edited, after, network_write=network_peer)
            async with buffered(await connect(peer, api.token, deleted['seq'])) as reopened:
                assert (await receive(reopened,'ready'))['durable_cursor'] == deleted['seq']
                observed = await receive(reopened,'files.changed',select('created',edited),seconds=25)
                assert observed['seq'] > deleted['seq']
            await asyncio.to_thread(wait_for, lambda: cache(after,edited),'Reconnect event did not reach index',35)
            receipts = await asyncio.to_thread(events)
            watchers = [row for row in receipts if row.get('source')=='file_watch']
            assert len([row for row in watchers if row['initial']]) == 1
            for kind, identifier in report['mutations'].items():
                matches = [row for row in watchers if select(kind,relative)(row)]
                assert [row['event_id'] for row in matches] == [identifier]
            assert [row['event_id'] for row in watchers if select('created',edited)(row)] == [observed['event_id']]
            report.update(reconnected_event=observed['event_id'], durable_events=len(watchers),
                          outbox_indexed_before_search=True, foreign_rejected=True)

    try:
        with worker_peer(api, store, environment_overrides=overrides) as peer:
            asyncio.run(verify(peer))
    finally:
        (api.root/'external-file-watch.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__ == '__main__':
    network_peer = '--network-peer' in sys.argv
    if network_peer:
        sys.argv.remove('--network-peer')
    asyncio.run(main(selected_factory=lambda config: partial(scenario, configuration=config, network_peer=network_peer),
                     fixture_override=FIXTURE))
