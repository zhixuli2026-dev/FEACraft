"""Run two real case commands across an API restart and read the evidence with DeepSeek."""
import asyncio
from contextlib import contextmanager
import json
import os
import subprocess
import sys
from textwrap import dedent
import time
import uuid

from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_job_repeat_acceptance import main, put
from live_llm_outbox_restart_acceptance import restart
from testsupport import IsolatedServer, SERVER


FIXTURE = """
import os
from pathlib import Path
(Path(__file__).parent / 'api-pid').write_text(str(os.getpid()))
"""
FILES = ('Ref/alpha.txt', 'Ref/beta.txt')


@contextmanager
def worker_peer(api, store, *, delay_old_publication=False, environment_overrides=None):
    """Keep the real old command and its owner alive while the serving API restarts."""
    peer = IsolatedServer('publication-worker', database_url=api.database_url)
    peer.token, peer.case_id, peer.prefix = api.token, api.case_id, api.prefix
    env = dict(api.env)
    if store is not None:
        env.update(store.settings())
    env.update(environment_overrides or {})
    app = 'app.main:app'
    if delay_old_publication:
        app = 'late_publisher:app'
        env['PYTHONPATH'] = str(peer.root) + os.pathsep + env['PYTHONPATH']
        (peer.root / 'late_publisher.py').write_text(dedent(f"""
            import asyncio
            import time
            from pathlib import Path
            from app.main import app
            from app.services.publication_source_validation import SourceValidation

            acquire = SourceValidation.acquire
            async def delayed(self, status, prepared):
                if prepared is not None and prepared.publication.generation == 1 and status == 'succeeded':
                    Path({str(api.root / 'old-writes-drained')!r}).touch()
                    limit = time.monotonic() + 240
                    while not Path({str(api.root / 'release-old-command')!r}).exists():
                        if time.monotonic() >= limit:
                            raise TimeoutError('Late publication never released')
                        await asyncio.sleep(.1)
                return await acquire(self, status, prepared)
            SourceValidation.acquire = delayed
        """), encoding='utf8')
    with (peer.root / 'worker.log').open('wb') as log:
        with subprocess.Popen(
            [sys.executable, '-B', '-m', 'uvicorn', app, '--app-dir', str(SERVER),
             '--host', '127.0.0.1', '--port', str(peer.port), '--no-access-log'],
            cwd=peer.root, env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as process:
            def healthy():
                assert process.poll() is None, 'Isolated worker stopped at startup'
                try:
                    return peer.call('GET', '/api/health', api_prefix=False)['ok']
                except OSError:
                    return False

            try:
                wait_for(healthy, 'Isolated worker failed to start')
                yield peer
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()


def submit(api):
    key = uuid.uuid4().hex
    deadline = time.monotonic() + 45
    while True:
        response = api.call('POST', api.prefix + '/jobs',
                            {'type': 'convert', 'command': 'python Ref/generate.py'},
                            expect=(200, 409), extra_headers={'Idempotency-Key': key})
        if 'id' in response:
            return response['id']
        assert response['detail']['code'] in {'storage_quota_busy', 'validation_busy'}, response
        if time.monotonic() >= deadline:
            raise TimeoutError('Publication job admission stayed busy')
        time.sleep(.15)


def reboot(api):
    pid_file = api.root / 'api-pid'
    old_pid = int(pid_file.read_text())
    kill_owned_api(api, old_pid)
    restart(api)
    new_pid = int(pid_file.read_text())
    assert new_pid != old_pid
    return [old_pid, new_pid]


def scenario(api, *, store=None, **_):
    assert api.database_url, 'Use --postgres for this two-process scenario'
    started, release = api.root / 'old-command-started', api.root / 'release-old-command'
    program = dedent(f"""
        from pathlib import Path
        import os

        try:
            with Path({str(started)!r}).open('x') as witness:
                witness.write(str(os.getpid()))
        except FileExistsError:
            generation = 'new'
        else:
            generation = 'old'
        for name in {FILES!r}:
            Path(name).write_bytes((generation + '/' + name).encode())
    """)
    put(api, 'Ref/generate.py', program)
    with worker_peer(api, store, delay_old_publication=True) as worker:
        old_id = submit(worker)
        try:
            wait_for((api.root / 'old-writes-drained').exists, 'Old command never reached final validation', 100)
            pids = reboot(api)
            assert api.call('GET', f'/jobs/{old_id}')['status'] == 'running'
            new_id = submit(api)
            new = api.wait_job(new_id)['attempt']['outputs']['publication']
            assert new['status'] == 'committed'
            assert api.call('GET', f'/jobs/{old_id}')['status'] == 'running'
            release.touch()
            old = api.wait_job(old_id)['attempt']['outputs']['publication']
            assert old['status'] == 'superseded', old
        finally:
            release.touch(exist_ok=True)
        assert old['kind'] == new['kind'] and old['generation'] + 1 == new['generation']
        final_pids = reboot(api)
        for name in FILES:
            assert api.call('GET', api.prefix + '/raw?path=' + name, raw=True) == ('new/' + name).encode()
        # Verify the persisted pointer and both row states independently of API output.
        import asyncpg
        from sqlalchemy.engine import make_url
        async def heads():
            url = make_url(api.database_url)
            db = await asyncpg.connect(host=url.host, port=url.port, user=url.username,
                                       password=url.password, database=url.database)
            try:
                return [dict(row) for row in await db.fetch("""
                    SELECT p.id, p.status, p.generation, h.active_publication_id
                    FROM artifact_publications p JOIN artifact_publication_heads h
                    ON (p.case_id,p.kind)=(h.case_id,h.kind)
                    WHERE p.case_id=$1 AND p.kind=$2 ORDER BY p.generation
                """, api.case_id, new['kind'])]
            finally:
                await db.close()
        rows = asyncio.run(heads())
        assert [(r['id'], r['status']) for r in rows] == [(old['id'], 'superseded'), (new['id'], 'committed')]
        assert all(r['active_publication_id'] == new['id'] for r in rows)
    evidence = {'storage': 's3' if store else 'fs', 'database': 'postgresql',
                'old_job': old_id, 'new_job': new_id, 'command_pid': int(started.read_text()),
                'api_restarts': [pids, final_pids], 'public_files': list(FILES), 'rows': rows}
    (api.root / 'publication-restart.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
    return evidence


if __name__ == '__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE))
