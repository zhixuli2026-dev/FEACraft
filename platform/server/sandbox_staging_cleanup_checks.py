"""Actual S3/WSL retirement and API death during staged-cache cleanup; no LLM.

The explicit live_llm entry may pass a real-model continuation. The filesystem
fault hook performs the original unlink, then actually terminates the isolated
API; it neither fabricates process evidence nor intercepts any model request.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from sandbox_retirement_checks import verify

CRASH_FIXTURE = '''
from pathlib import Path
import os
from app.services import sandbox_staging_cleanup as cleanup
_remove_entry = cleanup.remove_entry
def crash_after_real_unlink(root, name, entry, files):
 _remove_entry(root, name, entry, files)
 armed=Path('arm-cleanup-crash')
 if entry['kind']=='file' and armed.exists():
  armed.unlink()
  Path('cleanup-crash.json').write_text(__import__('json').dumps({'removed':name,'pid':os.getpid()}))
  os._exit(77)
cleanup.remove_entry=crash_after_real_unlink
'''


async def after_retirement(api, sid, rid, proof, continuation=None):
    report = {'passed': False, 'llm_calls': 0, 'mock_llm_calls': 0, 'checks': []}
    endpoint = '/terminal/sessions/' + sid + '/staging-cleanup'
    body = {'recovery_id': rid, 'confirm': True}
    def save():
        trace = api.root / 'upstream.jsonl'
        if trace.exists():
            rows = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            report['llm_calls'] = sum(row['kind']=='http' and row['path'].endswith('/chat/completions') for row in rows)
        (api.root / 'staging-cleanup-checks.json').write_text(json.dumps(report, indent=2))
    def record(name, kind='actual_infrastructure'):
        report['checks'].append({'name': name, 'kind': kind})
        save()
        print('STAGING_CLEANUP_PASS ' + name, flush=True)
    async def call(method, path, **kwargs):
        return await asyncio.to_thread(api.call, method, path, **kwargs)
    async def admin(expected):
        return await call('GET', f'/fixture/{api.case_id}/admin', api_prefix=False, expect=expected)
    def query(sql, args=()):
        with sqlite3.connect(api.root / 'test.sqlite') as db:
            return db.execute(sql, args).fetchall()
    async def start():
        api.proc = subprocess.Popen(api.proc.args, cwd=api.root, env=api.env, stdout=api.log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        async with asyncio.timeout(40):
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET', '/api/health', api_prefix=False)).get('ok'):
                        break
                except OSError:
                    pass
                await asyncio.sleep(.1)
    try:
        selected = json.loads(query('SELECT profile_json FROM native_sessions WHERE id=?', (sid,))[0][0])
        snapshot = json.loads(query('SELECT profile_json FROM native_requests WHERE id=?', (rid,))[0][0])
        stage = api.root / 'files' / selected['staging_prefix']
        assert stage.resolve().parent == (api.root / 'files' / '.staging').resolve()
        manifest = stage.with_name(stage.name + '.manifest.json')
        marker = stage.with_name(stage.name + '.terminal-cleanup.json')
        archive = Path(snapshot['root']) / 'recovery.zip'
        original_archive = archive.read_bytes()
        assert (await call('GET', endpoint))['status']=='not_started'
        await admin(409)
        await call('POST', endpoint, body={'recovery_id': rid}, expect=422)
        await call('POST', endpoint, body={**body,'confirm':False}, expect=422)
        await call('POST', endpoint, body={**body,'recovery_id':str(uuid.uuid4())}, expect=409)
        stranger = await call('POST', '/auth/register', body={'email':uuid.uuid4().hex+'@example.com',
            'password':'isolated-test-123','name':'Other cleanup user'})
        await call('POST', endpoint, body=body, extra_headers={'Authorization':'Bearer '+stranger['token']}, expect=404)
        record('explicit_confirmation_snapshot_binding_and_owner_required', 'protocol_fault')
        baseline = json.loads(manifest.read_bytes())
        first = stage / next(iter(baseline['files']))
        original = first.read_bytes()
        try:
            first.write_bytes(original + b'changed')
            await call('POST', endpoint, body=body, expect=409)
            assert first.read_bytes()==original+b'changed' and stage.exists()
        finally:
            first.write_bytes(original)
        unknown = stage / 'unregistered-cleanup-proof.txt'
        unknown.write_text('keep this unknown file', encoding='utf8')
        try:
            await call('POST', endpoint, body=body, expect=409)
            assert unknown.read_text()=='keep this unknown file'
        finally:
            unknown.unlink()
        try:
            archive.write_bytes(original_archive[:-1])
            await call('POST', endpoint, body=body, expect=409)
        finally:
            archive.write_bytes(original_archive)
        assert not query("SELECT id FROM durable_events WHERE type='terminal.staging_cleanup_started'")
        record('changed_unknown_contents_and_corrupt_snapshot_refused_before_intent', 'protocol_fault')
        total = len(baseline['files'])
        assert total > 1
        (api.root / 'arm-cleanup-crash').write_text('exit after first actual unlink')
        try:
            await call('POST', endpoint, body=body)
            raise AssertionError('Expected actual API exit during cleanup')
        except (OSError, ConnectionError):
            pass
        assert await asyncio.to_thread(api.proc.wait, 15)==77
        assert (api.root / 'cleanup-crash.json').is_file()
        remaining = [p for p in stage.rglob('*') if p.is_file()]
        assert len(remaining)==total-1 and manifest.exists() and marker.exists()
        assert len(query("SELECT id FROM durable_events WHERE type='terminal.staging_cleanup_started'"))==1
        assert not query("SELECT id FROM durable_events WHERE type='terminal.staging_cleanup_completed'")
        await start()
        assert (await call('GET', endpoint))['status']=='pending'
        await admin(409)
        record('actual_api_exit_after_unlink_leaves_committed_intent_and_blocks_admin')
        before = {str(p.relative_to(stage)):hashlib.sha256(p.read_bytes()).hexdigest() for p in remaining}
        unknown.write_text('retain after crash', encoding='utf8')
        try:
            await call('POST', endpoint, body=body, expect=409)
            assert unknown.read_text()=='retain after crash'
            assert all(hashlib.sha256((stage/p).read_bytes()).hexdigest()==sha for p,sha in before.items())
        finally:
            unknown.unlink()
        record('unknown_survivor_after_crash_refuses_retry_without_further_deletion', 'protocol_fault')
        finished = await call('POST', endpoint, body=body)
        assert finished['status']=='completed' and finished['original_staging_removed']
        assert not finished['image_removed'] and not finished['source_synchronized']
        assert not any(p.exists() for p in (stage,manifest,marker))
        assert archive.read_bytes()==original_archive
        assert (Path(snapshot['image_root']) / 'volume.ext4').exists()
        await call('GET', api.prefix+'/raw?path=Ref/retirement.txt', expect=404)
        await admin(200)
        record('restart_retry_removes_only_original_cache_preserves_snapshot_image_and_remote_project')
        assert await call('POST', endpoint, body=body)==finished
        api.proc.kill()
        await asyncio.to_thread(api.proc.wait, 10)
        await start()
        assert await call('GET', endpoint)==finished
        assert await call('POST', endpoint, body=body)==finished
        assert len(query("SELECT id FROM durable_events WHERE type='terminal.staging_cleanup_started'"))==1
        assert len(query("SELECT id FROM durable_events WHERE type='terminal.staging_cleanup_completed'"))==1
        await admin(200)
        record('completed_receipt_is_idempotent_after_second_actual_restart')
        stage.mkdir()
        unknown.write_text('new contents must survive', encoding='utf8')
        try:
            await call('GET', endpoint, expect=409)
            await call('POST', endpoint, body=body, expect=409)
            await admin(409)
            assert unknown.read_text()=='new contents must survive'
        finally:
            unknown.unlink()
            stage.rmdir()
        record('reappeared_directory_revokes_completion_without_deleting_new_data', 'protocol_fault')
        if continuation:
            report['llm_calls'] = await continuation(api, sid, rid, proof)
            record('real_model_reads_restored_snapshot_after_staging_cleanup', 'real_llm')
        report['passed'] = True
        return report['llm_calls']
    finally:
        save()


if __name__=='__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    from s3testsupport import ObjectStore
    runtime = Path(__file__).resolve().parents[2] / 'third_party/seaweed-4.52-runtime/weed'
    with ObjectStore(executable=runtime) as store:
        asyncio.run(verify(store, fixture_prefix=CRASH_FIXTURE, after_retirement=after_retirement))
