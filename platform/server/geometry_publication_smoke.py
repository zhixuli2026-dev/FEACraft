"""Real dual-API CadQuery publication races on disposable SQLite/PostgreSQL."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

CONTROLLED = '''from pathlib import Path
from fault_fixture import FileBarrier
from app.services import artifact_publication as pub
from sqlalchemy.ext.asyncio import AsyncSession
root = Path(__file__).parent
finalize = pub.finalize
async def final(db, job, *args):
    result = await finalize(db, job, *args)
    if (root / ('hold-'+str(job.case_id))).exists():
        await FileBarrier(root, 'publish-'+str(job.case_id), timeout=40).wait_async()
    if (root / ('commitfail-'+str(job.case_id))).exists():
        db.info['test_publication_commit_fail'] = True
    return result
pub.finalize = final
commit = AsyncSession.commit
async def committing(db):
    if db.info.pop('test_publication_commit_fail', False):
        raise OSError('injected publication commit failure')
    await commit(db)
AsyncSession.commit = committing
aliases = pub._write_aliases
def write_aliases(prefix, row):
    if (root / ('aliasfail-'+str(row.case_id))).exists():
        raise OSError('injected alias repair failure')
    return aliases(prefix, row)
pub._write_aliases = write_aliases
from app.services import job_execution
attempt_event = job_execution.attempt_event
async def event(db, job, attempt, kind, payload):
    if kind == 'artifact.alias_repair_required' and (root / ('eventfail-'+str(job.case_id))).exists():
        raise OSError('injected alias notice failure')
    return await attempt_event(db, job, attempt, kind, payload)
job_execution.attempt_event = event
from app.main import app
'''


def script(width=1, barrier=None):
    pause = ''
    if barrier:
        pause = (f'    from pathlib import Path\n    import time\n    p=Path({str(barrier)!r})\n'
            '    p.with_suffix(".ready").touch()\n    deadline=time.monotonic()+35\n'
            '    while not p.with_suffix(".release").exists():\n'
            '        assert time.monotonic()<deadline, "test barrier timeout"\n        time.sleep(0.02)\n')
    return ('import cadquery as cq\ndef build_geometry(ctx):\n'+pause+
        f'    return {{"box":cq.Workplane("XY").box({width},2,3)}}\n'
        'def export_geometry(ctx, objects):\n'
        '    (ctx.a_geo_dir / "export-hook.txt").write_text("called")\n'
        '    cq.exporters.export(objects["box"], str(ctx.a_geo_dir / "inline-export.step"))\n')


def wait_file(path, timeout=35):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(str(path))


def verify(database_url=None):
    api = IsolatedServer('geometry-publication', app='controlled_geometry_publication:app', database_url=database_url)
    (api.root / 'controlled_geometry_publication.py').write_text(CONTROLLED, encoding='utf-8')
    peer = IsolatedServer('geometry-publication-peer')
    checks = []
    with api:
        api.create_case('published geometry')
        workspace_id = api.call('GET','/workspaces')[0]['id']
        peer.token = api.token
        peer.log = (peer.root / 'api.log').open('w', encoding='utf-8')
        peer.proc = subprocess.Popen([sys.executable, '-m','uvicorn','controlled_geometry_publication:app',
            '--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(peer.port),'--no-access-log'],
            cwd=peer.root, env=api.env, stdout=peer.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        def root_for(case_id):
            return next((api.root / 'files/workspaces').glob(f'*/cases/{case_id}'))
        def new_case(name, source):
            case = api.call('POST', f'/workspaces/{workspace_id}/cases', {'name':name})
            (root_for(case['id']) / 'A-geo/geometry.py').write_text(source, encoding='utf-8')
            return case['id']
        def launch(case_id, mode='preview', client=api, key=None):
            return client.call('POST', f'/cases/{case_id}/geometry:{mode}', {},
                extra_headers={'Idempotency-Key':key} if key else None)
        def scene(case_id, identifier=None, client=api, expect=200):
            return client.call('GET', f'/cases/{case_id}/geometry/scene'+
                ('?publication_id='+identifier if identifier else ''), expect=expect)
        def history(case_id):
            return peer.call('GET',f'/cases/{case_id}/geometry/publications')
        def width(scene):
            box = scene['objects'][0]['bbox']
            return round(box['max'][0]-box['min'][0], 5)
        try:
            for _ in range(200):
                if peer.proc.poll() is not None:
                    raise AssertionError((peer.root / 'api.log').read_text(encoding='utf-8'))
                try:
                    if peer.call('GET','/api/health',api_prefix=False).get('ok'):
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            else:
                raise TimeoutError('Peer API did not start')

            case_id = api.case_id
            response = api.put('A-geo/geometry.py', script())
            completed = api.wait_job(response['auto_preview_job'])
            assert completed['attempt']['workspace']['run_path'].startswith('.vibeflow/attempts/')
            original = scene(case_id, client=peer)
            assert width(original) == 1 and original['publication']['current'] and not original['publication']['stale']
            old_id = original['publication']['id']
            assert len(history(case_id)) == 1 and history(case_id)[0]['status']=='committed'
            assert (root_for(case_id) / 'A-geo/scene.json').is_file()
            checks.append('default_auto_preview_executes_in_frozen_attempt_and_peer_reads_committed_bundle')

            with ThreadPoolExecutor(max_workers=6) as pool:
                requests = [pool.submit(launch,case_id,'preview',api if i%2 else peer,'geometry-once') for i in range(5)]
                replies = [future.result(timeout=30) for future in requests]
            assert len({reply['job_id'] for reply in replies}) == 1
            api.wait_job(replies[0]['job_id'])
            assert len(history(case_id)) == 2
            api.call('POST', f'/cases/{case_id}/geometry:export', {}, expect=409,
                extra_headers={'Idempotency-Key':'geometry-once'})
            api.call('POST', f'/cases/{case_id}/geometry:preview', {}, expect=400,
                extra_headers={'Idempotency-Key':'bad key'})
            checks.append('five_cross_API_idempotent_previews_allocate_one_attempt_and_generation')

            exported = launch(case_id, 'export')
            api.wait_job(exported['job_id'])
            current = scene(case_id)
            assert width(current)==1 and not current['publication']['stale']
            root = root_for(case_id)
            assert (root / 'A-geo/export-hook.txt').read_text()=='called'
            assert 'ISO-10303-21' in (root / 'A-geo/inline-export.step').read_text()[:200]
            assert (root / 'A-geo/exports/box.stl').stat().st_size > 100
            toolchain = json.loads((root / 'A-geo/toolchain.json').read_text())
            assert toolchain['cadquery'] and toolchain['ocp'] and len(toolchain['worker_sha256'])==64
            assert width(scene(case_id, old_id))==1
            assert not scene(case_id, old_id)['publication']['current']
            checks.append('export_publishes_scene_validation_STEP_STL_custom_hook_and_toolchain_with_readable_history')
            saved = api.call('GET',api.prefix+'/caseDict')
            api.call('PUT',api.prefix+'/caseDict',{'content':saved['content']+'\n// bookkeeping only\n'})
            assert not scene(case_id)['publication']['stale']
            api.advance('B')
            assert not scene(case_id)['publication']['stale']
            api.advance('A')
            checks.append('engineering_gate_uses_published_validation_and_ignores_bookkeeping_only_changes')

            immutable_path = history(case_id)[0]['path']+'/files/A-geo/scene.json'
            reject = api.call('PUT',api.prefix+'/file',{'path':immutable_path,'content':'bad'},expect=409)
            assert reject['detail']['code']=='input_sealed'
            api.call('DELETE',api.prefix+'/file?path='+immutable_path,expect=409)
            checks.append('ordinary_file_writes_and_deletes_cannot_change_publication_versions')

            draft = dict(current, objects=[])
            api.put('A-geo/scene.json',json.dumps(draft))
            assert scene(case_id)['publication']['status']=='draft'
            api.advance('B',expect=409)
            assert width(scene(case_id,current['publication']['id']))==1
            restored = launch(case_id)
            api.wait_job(restored['job_id'])
            assert width(scene(case_id))==1
            checks.append('manual_alias_edit_invalidates_active_proof_but_keeps_pinned_history_and_can_regenerate')

            async def clear_scene():
                import websockets
                from urllib.parse import quote
                uri = peer.base.replace('http', 'ws', 1)+f'/ws/cadscene?case_id={case_id}&token={quote(peer.token)}'
                async with websockets.connect(uri) as socket:
                    assert json.loads(await socket.recv())['type']=='ready'
                    await socket.send(json.dumps({'type':'scene.clear'}))
                    assert json.loads(await socket.recv())['type']=='scene.clear'
            asyncio.run(clear_scene())
            assert scene(case_id)['publication']['status']=='draft'
            api.advance('B',expect=409)
            assert width(scene(case_id,restored['publication_id']))==1
            restored = launch(case_id)
            api.wait_job(restored['job_id'])
            checks.append('CAD_websocket_edit_invalidates_published_proof_across_API_processes')

            slow = api.root / 'old-native'
            race_id = new_case('old-versus-new',script(1,slow))
            old = launch(race_id)
            try:
                wait_file(slow.with_suffix('.ready'))
                newer = peer.call('PUT',f'/cases/{race_id}/file',{'path':'A-geo/geometry.py','content':script(4)})
                # Final publication waits for earlier case writers to drain.
                # Let the newer native execution finish, then release the old
                # writer before asking for the newer committed result.
                deadline=time.monotonic()+30
                while peer.call('GET',f'/jobs/{newer["auto_preview_job"]}')['attempt']['phase']!='finalizing':
                    assert time.monotonic()<deadline,'Newer geometry did not reach finalizing'
                    time.sleep(.05)
                slow.with_suffix('.release').touch()
                peer.wait_job(newer['auto_preview_job'])
                winner = scene(race_id,client=peer)
                assert width(winner)==4
            finally:
                slow.with_suffix('.release').touch()
            api.wait_job(old['job_id'])
            assert scene(race_id)['publication']['id']==winner['publication']['id']
            assert width(scene(race_id,old['publication_id']))==1
            assert next(row for row in history(race_id) if row['id']==old['publication_id'])['status']=='superseded'
            assert width(json.loads((root_for(race_id)/'A-geo/scene.json').read_text()))==4
            checks.append('late_old_native_completion_is_historical_superseded_and_never_replaces_new_head_or_alias')

            slow = api.root / 'stale-native'
            stale_id = new_case('input-change-without-resubmit',script(2,slow))
            stale = launch(stale_id)
            try:
                wait_file(slow.with_suffix('.ready'))
                peer.call('PUT',f'/cases/{stale_id}/file',{'path':'A-geo/new-input.txt','content':'changed input'})
            finally:
                slow.with_suffix('.release').touch()
            failed = api.wait_job(stale['job_id'],expected='failed')
            assert failed['attempt']['reason_code']=='input_stale'
            assert not any(row['current'] for row in history(stale_id))
            checks.append('changed_geometry_dependencies_reject_latest_publication_without_active_pointer')

            held_id = new_case('atomic-publish-reader',script(3))
            (api.root / f'hold-{held_id}').touch()
            held = launch(held_id)
            try:
                wait_file(api.root / f'publish-{held_id}.ready')
                during = api.call('GET',f'/jobs/{held["job_id"]}')
                assert during['status']=='running' and during['phase']=='finalizing'
                with ThreadPoolExecutor(max_workers=1) as pool:
                    reader = pool.submit(scene,held_id,None,peer)
                    time.sleep(0.2)
                    assert not reader.done()
                    (api.root / f'publish-{held_id}.release').touch()
                    result = reader.result(timeout=30)
                    assert width(result)==3 and result['publication']['current']
            finally:
                (api.root / f'publish-{held_id}.release').touch()
            api.wait_job(held['job_id'])
            checks.append('reader_waits_for_single_transaction_publication_head_job_success_and_events')

            alias_id = new_case('alias-repair-failure',script(5))
            (api.root / f'aliasfail-{alias_id}').touch()
            alias = launch(alias_id)
            api.wait_job(alias['job_id'])
            assert width(scene(alias_id,client=peer))==5
            assert not (root_for(alias_id)/'A-geo/scene.json').exists()
            saved = peer.call('GET',f'/cases/{alias_id}/caseDict')
            peer.call('POST',f'/cases/{alias_id}/caseDict:advance',
                {'state':'B','confirmed':True,'expected_revision':saved['dict_revision']})
            checks.append('compatibility_alias_failure_preserves_canonical_peer_read_and_engineering_gate')

            notice_id = new_case('alias-notice-failure',script(5))
            (api.root / f'aliasfail-{notice_id}').touch()
            (api.root / f'eventfail-{notice_id}').touch()
            notice = launch(notice_id)
            api.wait_job(notice['job_id'])
            assert width(scene(notice_id,client=peer))==5
            checks.append('failed_alias_notice_cannot_reverse_committed_success_or_fail_completion_wait')

            for artifact in ('validation.json','scene.json'):
                malformed_id = new_case('malformed-export-'+artifact,
                    script(7)+f'    (ctx.a_geo_dir / {artifact!r}).write_text("[]")\n')
                valid = launch(malformed_id)
                api.wait_job(valid['job_id'])
                malformed = launch(malformed_id,'export')
                failed = api.wait_job(malformed['job_id'],expected='failed')
                assert failed['attempt']['reason_code']=='publication_failed'
                assert scene(malformed_id,client=peer)['publication']['id']==valid['publication_id']
                assert width(scene(malformed_id,client=peer))==7
            checks.append('malformed_export_hook_JSON_finishes_failed_and_preserves_previous_publication')

            failed_id = new_case('commit-failure',script(6))
            baseline = launch(failed_id)
            api.wait_job(baseline['job_id'])
            before = scene(failed_id)
            (api.root / f'commitfail-{failed_id}').touch()
            broken = launch(failed_id)
            deadline = time.monotonic()+35
            while time.monotonic()<deadline:
                state = api.call('GET',f'/jobs/{broken["job_id"]}')
                if state.get('completion_error'):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('Commit failure was not observable')
            assert state['status']!='succeeded'
            assert scene(failed_id,client=peer)['publication']['id']==before['publication']['id']
            assert next(row for row in history(failed_id) if row['id']==broken['publication_id'])['status']=='prepared'
            assert (root_for(failed_id)/'.vibeflow/publications'/broken['publication_id']).is_dir()
            checks.append('DB_commit_failure_preserves_old_head_and_alias_with_uncommitted_bundle_retained_for_recovery')

            damaged = history(case_id)[0]
            (root_for(case_id)/damaged['path']/'manifest.json').write_text('{}')
            scene(case_id,expect=409)
            api.advance('B',expect=409)
            regenerated = launch(case_id)
            api.wait_job(regenerated['job_id'])
            assert width(scene(case_id))==1
            checks.append('corrupt_publication_is_not_trusted_and_new_valid_generation_can_repair_service')

            other = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
                'password':'publication-other-123','name':'Other'})['token']
            for path in (f'/cases/{case_id}/geometry/scene?publication_id={old_id}',f'/cases/{case_id}/geometry/publications'):
                api.call('GET',path,expect=404,extra_headers={'Authorization':'Bearer '+other})
            scene(race_id,old_id,expect=404)
            checks.append('publication_history_and_pinned_reads_enforce_case_and_user_ownership')
            result = {'passed':True,'count':len(checks),'checks':checks,'api_processes':2,
                'shared_database':True,'evidence':str(api.root)}
            (api.root/'publication-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        finally:
            for marker in api.root.glob('*.ready'):
                marker.with_suffix('.release').touch()
            if peer.proc.poll() is None:
                peer.proc.terminate()
                try:
                    peer.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    peer.proc.kill()
                    peer.proc.wait(timeout=5)
            peer.log.close()
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix='geometry-publication-evidence-',dir=SERVER.parents[1]/'tmp'))
    results = {'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        pg_url = make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert pg_url.drivername=='postgresql+asyncpg' and pg_url.host in {'127.0.0.1','localhost','::1'}
        name = 'tmp_vf_publication_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_publication_[0-9a-f]{32}',name)
        admin = await asyncpg.connect(host=pg_url.host,port=pg_url.port or 5432,user=pg_url.username,
            password=pg_url.password,database='postgres',timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created = True
            url = pg_url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,url,report,'upgrade','head')
            results['postgresql'] = await asyncio.to_thread(verify,url)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('GEOMETRY_PUBLICATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
