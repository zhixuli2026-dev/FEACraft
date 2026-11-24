"""Actual same-host shared-index delivery/restart/rebuild and real model readback."""
import argparse
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer, SERVER
from app.services.process_identity_native import probe

ROOT = SERVER.parents[1]

# Only the writer's background delivery is paused to establish which actual
# process consumes its event. No generated replies, fake tools or fake storage.
OBSERVER = '''
from app.services import durable_events
from app.services.process_identity_native import observe
@app.get('/fixture/process')
async def process_birth():
    return observe(os.getpid())
original_publish = durable_events.publish_to_local_bus
async def observed_publish(event, factory):
    await original_publish(event, factory)
    with (root/'delivered.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'id': event.id, 'type': event.type})+'\\n')
durable_events.publish_to_local_bus = observed_publish
if PAUSED:
    async def paused_delivery(factory, stop, **kwargs):
        await stop.wait()
    durable_events.dispatch_forever = paused_delivery
'''


class SharedIndexServer(IsolatedServer):
    def __enter__(self):
        super().__enter__()
        self.birth = self.call('GET', '/fixture/process', api_prefix=False)
        return self

    def __exit__(self, error_type, error, traceback):
        # Windows venv Python may have a launcher parent. End this disposable
        # API's tree before waiting on its launcher; never target other servers.
        if sys.platform == 'win32' and self.proc and self.proc.poll() is None:
            subprocess.run(['taskkill', '/PID', str(self.proc.pid), '/T', '/F'],
                           capture_output=True, timeout=15)
        super().__exit__(error_type, error, traceback)


def configure(api, overrides, paused):
    (api.root/'live_fixture.py').write_text(
        'import os\nos.environ.update('+repr(overrides)+')\n'+FIXTURE+
        OBSERVER.replace('PAUSED', repr(paused)), encoding='utf-8')


def verify(configuration):
    writer = SharedIndexServer('live-shared-index', app='live_fixture:app')
    peer = SharedIndexServer('live-shared-index-peer', app='live_fixture:app')
    report = {'passed': False, 'topology': 'two actual same-host APIs, shared SQLite metadata/fs/index',
              'real_llm_calls': 0, 'mock_llm_calls': 0, 'checks': [], 'api_exits': []}
    proof = 'INDEX_'+uuid.uuid4().hex
    filename = 'Ref/shared-index-proof.txt'
    configure(writer, {}, True)
    def save():
        trace = peer.root/'upstream.jsonl'
        report['upstream'] = [json.loads(x) for x in trace.read_text(encoding='utf-8').splitlines()] if trace.exists() else []
        report['real_llm_calls'] = sum(x['kind']=='http' for x in report['upstream'])
        raw = json.dumps(report, ensure_ascii=False, indent=2)
        assert configuration['key'] not in raw
        (writer.root/'shared-index-result.json').write_text(raw+'\n', encoding='utf-8')
    def passed(name):
        report['checks'].append(name)
        save()
        print('SHARED_INDEX_PASS '+name, flush=True)
    def exited(api):
        deadline = time.monotonic()+10
        actual_probe = probe(api.birth)
        while actual_probe['state']=='match' and time.monotonic()<deadline:
            time.sleep(.05)
            actual_probe = probe(api.birth)
        report['api_exits'].append({'pid': api.proc.pid, 'exit_code': api.proc.poll(),
                                    'actual_process': api.birth, 'actual_probe': actual_probe,
                                    'confirmed_exited': api.proc.poll() is not None and actual_probe['state']=='exited'})
        assert report['api_exits'][-1]['confirmed_exited'], 'Actual API process has not exited'
    def receipt(index, expected):
        deadline = time.monotonic()+40
        while time.monotonic()<deadline:
            with closing(sqlite3.connect(writer.root/'test.sqlite')) as db:
                rows = db.execute("SELECT e.id FROM durable_events e JOIN event_outbox o ON o.event_id=e.id "
                                  "WHERE e.case_id=? AND e.type='files.changed' AND o.status='delivered'",
                                  (writer.case_id,)).fetchall()
            with closing(sqlite3.connect(index)) as db:
                content = db.execute('SELECT content FROM file_fts WHERE path=?', (filename,)).fetchone()
                matched = [r[0] for r in rows if db.execute('SELECT 1 FROM consumed_events WHERE event_id=?', r).fetchone()]
            observed = peer.root/'delivered.jsonl'
            identities = {json.loads(x)['id'] for x in observed.read_text(encoding='utf-8').splitlines()} if observed.exists() else set()
            if content and content[0] == expected and set(matched)&identities:
                return sorted(set(matched)&identities)
            time.sleep(.15)
        raise AssertionError('Peer did not commit actual content and durable event receipt')
    def search(api, word):
        result = api.call('GET', writer.prefix+'/search?q='+word)
        assert any(x['path']==filename and x['via']=='content' for x in result['hits']), result
    print('SHARED_INDEX_EVIDENCE '+str(writer.root), flush=True)
    try:
        try:
            with writer:
                writer.create_case('Shared index deployment')
                writer.call('POST', writer.prefix+'/index:rebuild')
                shared = {k: writer.env[k] for k in ('VIBEFLOW_DATABASE_URL', 'VIBEFLOW_STORAGE_FS_ROOT',
                          'VIBEFLOW_INDEX_ROOT', 'VIBEFLOW_JWT_SECRET')}
                configure(peer, shared, False)
                peer.token, peer.case_id, peer.prefix = writer.token, writer.case_id, writer.prefix
                index = Path(shared['VIBEFLOW_INDEX_ROOT'])/f'case-{writer.case_id}.sqlite'
                try:
                    with peer:
                        writer.put(filename, proof)
                        report['peer_event_ids'] = receipt(index, proof)
                        # Receipt/content checked before any API search can repair it.
                        passed('writer_event_consumed_by_other_actual_process_before_search')
                        search(writer, proof); search(peer, proof)
                        passed('both_actual_apis_search_same_committed_content')
                finally:
                    exited(peer)
                try:
                    with peer:
                        search(peer, proof)
                        replacement = proof+' updated'
                        writer.put(filename, replacement)
                        receipt(index, replacement)
                        search(writer, proof); search(peer, proof)
                        passed('peer_restart_retains_index_and_consumes_next_actual_write')
                finally:
                    exited(peer)
        finally:
            exited(writer)
        # Simulate a lost derived volume only after every actual API has exited;
        # preserve the old directory as evidence, keep all original source files.
        index_dir = Path(shared['VIBEFLOW_INDEX_ROOT'])
        index_dir.rename(writer.root/'index-before-rebuild')
        configure(peer, shared, False)
        try:
            with peer:
                before = peer.call('GET', writer.prefix+'/index/status')
                assert before == {'indexed': False}, before
                assert peer.call('GET', writer.prefix+'/raw?path='+filename, raw=True)==replacement.encode()
                built = peer.call('POST', writer.prefix+'/index:rebuild')
                assert built['entries']>0
                search(peer, proof)
                passed('missing_derived_index_rebuilt_from_unchanged_actual_source')
                profile = peer.call('POST', '/provider-profiles', {'provider': 'deepseek',
                    'name': 'Shared index actual readback', 'base_url': configuration['base_url'],
                    'default_model': configuration['model'], 'api_key': configuration['key']})
                conversation = peer.call('POST', '/conversations', {'case_id': peer.case_id,
                    'title': 'Shared index restored file', 'provider_profile_id': profile['id'],
                    'model': configuration['model'], 'permission_mode': 'plan'})
                events = []
                with httpx.Client(base_url=peer.base+'/api/v1', headers={'Authorization': 'Bearer '+peer.token},
                                  timeout=180, trust_env=False) as client:
                    with client.stream('POST', f'/conversations/{conversation["id"]}/messages', json={
                        'content': '请调用 read_text_file 读取 '+filename+'，只调用一次这个工具，原样回复文件内容，不修改文件。',
                        'provider_profile_id': profile['id'], 'model': configuration['model']}) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if line.startswith('data:'): events.append(json.loads(line[5:]))
                            assert len(json.dumps(events))<2*1024*1024
                raw = json.dumps(events, ensure_ascii=False, indent=2)
                assert configuration['key'] not in raw
                (writer.root/'model-events.json').write_text(raw, encoding='utf-8')
                tools = [x for x in events if x.get('type')=='tool.result']
                assert len(tools)==1 and tools[0]['tool']=='read_text_file' and tools[0]['ok']
                assert replacement in tools[0]['result']
                assert replacement in ''.join(x.get('text','') for x in events if x.get('type')=='text.delta')
                assert any(x.get('type')=='run.completed' for x in events)
                assert not any(x.get('type') in {'stream.error','request.failed','request.timed_out'} for x in events)
                history = peer.call('GET', f'/conversations/{conversation["id"]}')
                assert any(x['type']=='tool_result' and replacement in x['payload'].get('result','') for x in history['events'])
                assert peer.call('GET', writer.prefix+'/raw?path='+filename, raw=True)==replacement.encode()
                passed('real_model_reads_unknown_file_proof_after_index_recovery')
        finally:
            exited(peer)
        save()
        calls = [x for x in report['upstream'] if x['kind']=='http']
        assert len(calls)==2 and all(x['host']=='api.deepseek.com' and x['status']==200 for x in calls)
        assert len(report['api_exits'])==4 and all(x['confirmed_exited'] for x in report['api_exits'])
        report.update(passed=True, model=configuration['model'], peer_evidence=str(peer.root),
                      reported_tokens=sum(x.get('usage',{}).get('total_tokens',0) for x in report['upstream']))
        save()
        print('SHARED_INDEX_OK '+str(writer.root/'shared-index-result.json'), flush=True)
        return 0
    except BaseException as error:
        report.update(failure_type=type(error).__name__, failure=str(error).replace(configuration['key'], '[REDACTED]')[:1000])
        save()
        print('SHARED_INDEX_FAILED '+report['failure_type'], flush=True)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf-8'))
    assert review['entry']=='platform/server/live_llm_shared_index_acceptance.py'
    assert review['classification']=='real_llm'
    for name, digest in review['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest, name
    return verify(asyncio.run(source(args.source_profile_id)))


if __name__=='__main__':
    sys.exit(main())
