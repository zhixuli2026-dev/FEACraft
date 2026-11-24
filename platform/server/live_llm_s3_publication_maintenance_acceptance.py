"""Real PostgreSQL, authenticated S3, CadQuery and DeepSeek after cache eviction."""

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from uuid import uuid4

from app.services.process_identity_native import probe
from live_llm_acceptance import source
from live_llm_sandbox_recovery_acceptance import model_read
from live_llm_shared_index_acceptance import SharedIndexServer, configure
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from testsupport import SERVER


ROOT = SERVER.parents[1]
FAIL_AT_COMMIT = '''
from app.services import artifact_publication

original_finalize = artifact_publication.finalize
async def fail_after_copy(db, job, attempt, status, reason, prepared=None, validation=None):
    fail = root / 'fail-next-publication'
    if status == 'succeeded' and prepared is not None and fail.is_file():
        assert prepared.bundle_sha256 and prepared.error is None
        fail.unlink()
        status, reason = 'failed', 'publication_failed'
    return await original_finalize(db, job, attempt, status, reason, prepared, validation)
artifact_publication.finalize = fail_after_copy
'''


class RustFS:
    """Disposable official RustFS server on the Windows loopback interface."""

    def __init__(self, executable):
        self.executable = executable.resolve(strict=True)
        self.root = Path(tempfile.mkdtemp(prefix='j15-rustfs-', dir=ROOT/'tmp'))
        self.access_key, self.secret_key = uuid4().hex.upper(), secrets.token_hex(32)
        self.bucket = 'j15-rustfs-' + uuid4().hex
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            self.address = f'127.0.0.1:{server.getsockname()[1]}'
        self.endpoint = 'http://' + self.address

    def settings(self):
        return {'VIBEFLOW_STORAGE_BACKEND': 's3', 'VIBEFLOW_ALLOW_PARTIAL_S3': 'true',
                'VIBEFLOW_S3_ENDPOINT': self.endpoint, 'VIBEFLOW_S3_BUCKET': self.bucket,
                'VIBEFLOW_S3_ACCESS_KEY': self.access_key, 'VIBEFLOW_S3_SECRET_KEY': self.secret_key}

    def __enter__(self):
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError
        from app.services.s3_live_store import Client

        env = {**os.environ, 'RUSTFS_ACCESS_KEY': self.access_key, 'RUSTFS_SECRET_KEY': self.secret_key,
               'RUSTFS_CONSOLE_ENABLE': 'false', 'RUSTFS_OBS_LOG_DIRECTORY': str(self.root/'logs'),
               'RUSTFS_OBS_LOGGER_LEVEL': 'warn'}
        data = self.root/'data'
        data.mkdir()
        self.raw_client = boto3.client('s3', endpoint_url=self.endpoint, region_name='us-east-1',
            aws_access_key_id=self.access_key, aws_secret_access_key=self.secret_key,
            config=Config(proxies={}, connect_timeout=1, read_timeout=15, retries={'max_attempts':0},
                          s3={'addressing_style':'path'}))
        self.client = Client(self.raw_client)
        try:
            with (self.root/'rustfs.log').open('wb') as log:
                self.proc = subprocess.Popen([str(self.executable), 'server', '--address', self.address,
                    str(data)], cwd=self.root, env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

            def ready():
                if self.proc.poll() is not None:
                    raise RuntimeError(f'RustFS exited. See {self.root / "rustfs.log"}')
                try:
                    self.client.list_buckets()
                except (ConnectTimeoutError, EndpointConnectionError):
                    return False
                except ClientError as error:
                    if error.response['ResponseMetadata']['HTTPStatusCode'] != 503:
                        raise
                    return False
                return True

            wait(ready, 40)
            self.client.create_bucket(Bucket=self.bucket)
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *_):
        self.client.close()
        proc = getattr(self, 'proc', None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=15)


def wait(check, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(.12)
    raise TimeoutError('Expected fixture state was not observed')


def settled(api, job_id, *, status='succeeded'):
    def check():
        value = api.call('GET', f'/jobs/{job_id}')
        if value['ended_at'] and not value['completion_pending']:
            assert value['status'] == status, value['log_backlog'][-2000:]
            assert value['attempt']['process_stop']['verified']
            assert value['attempt']['outputs']['publication']['status'] == ('committed' if status == 'succeeded' else 'failed')
            return value
        return None
    return wait(check, 120)


def scenario(configuration, database_url, rustfs=None):
    primary = SharedIndexServer('live-j15-s3-primary', app='live_fixture:app', database_url=database_url)
    peer = SharedIndexServer('live-j15-s3-peer', app='live_fixture:app', database_url=database_url)
    store = (RustFS(rustfs) if rustfs else
             ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed', volume_max=8, authenticated=True))
    evidence = primary.root/'publication-maintenance.json'
    gate, started = primary.root/'release-cadquery', primary.root/'cadquery-started'
    report = {'passed': False, 'checks': [], 'peer': str(peer.root), 'store': str(store.root),
              'backend': 'RustFS' if rustfs else 'SeaweedFS',
              'real_llm_calls': 0, 'mock_llm_calls': 0}
    print('J15_EVIDENCE ' + str(primary.root), flush=True)

    def record(name, **fields):
        report['checks'].append(name)
        report.update(fields)
        evidence.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('J15_PASS ' + name, flush=True)

    def program(size, *, long=False):
        pause = (f'Path({str(started)!r}).touch()\n'
                 'deadline = time.monotonic() + 45\n'
                 f'while not Path({str(gate)!r}).exists():\n'
                 ' if time.monotonic() > deadline: raise TimeoutError("release fixture")\n'
                 ' time.sleep(.05)\n') if long else ''
        return ('from pathlib import Path\nimport time\nimport cadquery as cq\n' + pause +
                f'def build_geometry(ctx):\n return {{"box": cq.Workplane("XY").box({size}, 1, 1)}}\n')

    def launch(api, program_source):
        saved = api.put('A-geo/geometry.py', program_source)
        if saved['auto_preview_job'] is not None:
            return saved['auto_preview_job']
        return api.call('POST', api.prefix + '/geometry:preview')['job_id']

    def call_cli(*options, expect=0, publication_id=None):
        command = [sys.executable, '-X', 'utf8', '-B', str(ROOT/'platform/scripts/maintain-s3-publication-cache.py'),
                   str(primary.case_id), publication_id or first['id'], *options]
        result = subprocess.run(command, env={**primary.env, **store.settings()}, capture_output=True,
                                encoding='utf-8', timeout=120)
        assert result.returncode == expect, result.stderr[-800:]
        return json.loads(result.stdout) if expect == 0 else result.stderr.strip()

    try:
        with store:
            configure(primary, store.settings(), False)
            with (primary.root/'live_fixture.py').open('a', encoding='utf-8') as module:
                module.write(FAIL_AT_COMMIT)
            with primary:
                primary.create_case('Verified S3 publication eviction')
                scene = primary.prefix + '/geometry/scene'
                first_job = settled(primary, launch(primary, program(1)))
                first = first_job['attempt']['outputs']['publication']
                archive = f'.vibeflow/publications/{first["id"]}'
                prefix = first_job['attempt']['workspace']['storage']['prefix'].rstrip('/')
                cache = primary.root/'files'/prefix/archive
                assert not cache.exists(), 'Finalization or alias settlement materialized a full cache'
                first_scene = primary.call('GET', scene)
                assert first_scene['publication']['id'] == first['id'] and cache.exists()
                manifest_data = (cache/'manifest.json').read_bytes()
                manifest = json.loads(manifest_data)
                cached_scene = (cache/'files/A-geo/scene.json').read_bytes()
                assert hashlib.sha256(manifest_data).hexdigest() == first['manifest_sha256']
                record('real native publication commits and settles without materializing a cache')

                configure(peer, {**store.settings(), 'VIBEFLOW_JWT_SECRET': primary.env['VIBEFLOW_JWT_SECRET']}, False)
                peer.token = primary.token
                peer.case_id, peer.prefix = primary.case_id, primary.prefix
                second_job_id = launch(primary, program(2, long=True))
                try:
                    wait(started.exists)
                    long_started = time.monotonic()
                    with peer:
                        previous = peer.call('GET', scene)
                        assert previous['publication']['id'] == first['id']
                        assert previous['publication']['current'] is True
                        assert previous['publication']['stale'] is True
                        assert primary.call('GET', f'/jobs/{second_job_id}')['status'] == 'running'
                        time.sleep(max(0, 8 - (time.monotonic() - long_started)))
                        gate.touch()
                        second_job = settled(primary, second_job_id)
                        second = second_job['attempt']['outputs']['publication']
                        assert second['id'] != first['id'] and not cache.with_name(second['id']).exists()
                        assert peer.call('GET', scene)['publication']['id'] == second['id']
                        past = peer.call('GET', scene + '?publication_id=' + first['id'])
                        assert past['publication']['id'] == first['id'] and not past['publication']['current']
                        assert past['publication']['status'] == 'committed'
                        record('independent API reads one generation during a long native job and advances after commit',
                               native_hold_seconds=round(time.monotonic()-long_started, 2), first=first, second=second)

                        # Deleting this fixture's logical alias must not delete archived bytes.
                        alias = {'Bucket': store.bucket, 'Key': prefix + '/A-geo/scene.json'}
                        current = store.client.head_object(**alias)
                        store.client.delete_object(**alias, IfMatch=current['ETag'])
                        peer.call('GET', peer.prefix + '/raw?path=A-geo/scene.json', expect=404)
                        assert peer.call('GET', scene)['publication']['id'] == second['id']
                        assert peer.call('GET', scene + '?publication_id=' + first['id']) == past
                        record('logical deletion preserves current and historical immutable reads')

                        with closing(store.raw_client.get_object(Bucket=store.bucket,
                                Key=prefix+'/'+archive+'/manifest.json')['Body']) as stream:
                            assert stream.read() == manifest_data
                        proof = 'RETIREMENT_' + uuid4().hex
                        primary.put('Ref/publication-maintenance.txt', proof+'\n')
                finally:
                    gate.touch(exist_ok=True)
                (primary.root/'fail-next-publication').touch()
                failed_job = settled(primary, launch(primary, program(3)), status='failed')
                failed = failed_job['attempt']['outputs']['publication']
                original_key = prefix+'/'+failed_job['attempt']['workspace']['run_path']+'/A-geo/scene.json'
                duplicate_key = prefix+'/.vibeflow/publications/'+failed['id']+'/files/A-geo/scene.json'
                def archive_bytes(key):
                    with closing(store.raw_client.get_object(Bucket=store.bucket,Key=key)['Body']) as body:
                        return body.read()
                original_scene = archive_bytes(original_key)
                assert original_scene == archive_bytes(duplicate_key)
                def settled_scene():
                    read = primary.call('GET',scene,expect=(200,409))
                    if read.get('detail', {}).get('code') == 'file_sync_busy':
                        return None
                    return read['publication']
                assert wait(settled_scene, 30)['id'] == second['id']
                record('fault at final commit leaves a failed duplicate and the retained native job output', failed=failed)

            assert wait(lambda: probe(primary.birth)['state'] == 'exited')
            assert wait(lambda: probe(peer.birth)['state'] == 'exited')
            assert call_cli()['state'] == 'retained'
            options = ('--retention-days', '0')
            candidate = call_cli(*options)
            approval = ('--confirm-node-offline', '--review-sha256', candidate['review_sha256'])
            assert candidate['candidate_bytes'] == len(manifest_data) + sum(f['size'] for f in manifest['files'].values())

            unknown = cache/'manual-diagnostics.log'
            unknown.write_text('keep this fixture file', encoding='utf-8')
            refusal = call_cli(*options, *approval, expect=1)
            assert 'Unknown publication cache member' in refusal and unknown.is_file()
            unknown.unlink()
            record('review preserves unknown local content')

            key = prefix+'/'+archive+'/files/A-geo/scene.json'
            remote = {'Bucket': store.bucket, 'Key': key}
            store.raw_client.delete_object(**remote)
            try:
                call_cli(*options, *approval, expect=1)
                assert cache.is_dir()
            finally:
                store.raw_client.put_object(**remote, Body=cached_scene)
            record('confirmed eviction refuses an incomplete remote archive')

            candidate = call_cli(*options)
            approval = ('--confirm-node-offline', '--review-sha256', candidate['review_sha256'])
            evicted = call_cli(*options, *approval)
            assert evicted['state'] == 'evicted' and evicted['evicted_bytes'] == candidate['candidate_bytes']
            assert not cache.exists() and call_cli(*options)['state'] == 'absent'
            record('shipped CLI evicts only the reviewed node cache', eviction=evicted)

            jwt = primary.env['VIBEFLOW_JWT_SECRET']
            configure(primary, {**store.settings(), 'VIBEFLOW_JWT_SECRET': jwt}, False)
            with primary:
                assert primary.call('GET', scene + '?publication_id=' + first['id']) == past
                assert (cache/'files/A-geo/scene.json').read_bytes() == cached_scene
                assert primary.call('GET', scene)['publication']['id'] == second['id']
            assert wait(lambda: probe(primary.birth)['state'] == 'exited')
            record('cold restart restores exact historical bytes and still reads the committed head')

            # Simulate a maintenance-process interruption after atomic parking
            # and one actual unlink, with the audited API stopped.
            parked = cache.with_name('.evicting-' + first['id'])
            cache.rename(parked)
            (parked/'files/A-geo/scene.json').unlink()
            pending = call_cli(*options)
            assert pending['interrupted']
            resumed = call_cli(*options, '--confirm-node-offline', '--review-sha256', pending['review_sha256'])
            assert resumed['state'] == 'evicted' and not parked.exists()
            assert resumed['evicted_bytes'] + len(cached_scene) == evicted['evicted_bytes']
            record('interrupted eviction resumes from remaining inventoried files', resumed=resumed)

            def review_failed(*switches, **kwargs):
                return call_cli('--failed-copy', *switches, publication_id=failed['id'], **kwargs)

            assert review_failed()['state'] == 'retained'
            assert call_cli('--failed-copy', *options, expect=1).endswith('never became visible')
            candidate = review_failed(*options)
            failed_approval = ('--confirm-storage-offline', '--review-sha256', candidate['review_sha256'])
            original_args = {'Bucket':store.bucket, 'Key':original_key}
            store.raw_client.delete_object(**original_args)
            try:
                review_failed(*options, *failed_approval, expect=1)
                assert archive_bytes(duplicate_key) == original_scene
            finally:
                store.raw_client.put_object(**original_args, Body=original_scene)
            candidate = review_failed(*options)
            retired = review_failed(*options, '--confirm-storage-offline', '--review-sha256', candidate['review_sha256'])
            assert retired['state'] == 'evicted' and retired['evicted_bytes'] > len(original_scene)
            assert review_failed(*options)['state'] == 'absent'
            assert archive_bytes(original_key) == original_scene
            record('failed duplicate eviction requires intact original and preserves both old committed generations',
                   failed_copy=retired, preserved_sha256=hashlib.sha256(original_scene).hexdigest())

            configure(primary, {**store.settings(), 'VIBEFLOW_JWT_SECRET': jwt}, False)
            with primary:
                assert primary.call('GET', scene + '?publication_id=' + first['id']) == past
                report['real_llm_calls'] = model_read(primary, 'publication-cache', 'restart', proof, configuration,
                    already_restored=True, path='Ref/publication-maintenance.txt')
            assert wait(lambda: probe(primary.birth)['state'] == 'exited')
            record('actual DeepSeek selects and executes a case read after storage restart')
        assert store.proc.poll() is not None
        report.update(passed=True, store_exited=True)
    except BaseException as error:
        line = traceback.extract_tb(error.__traceback__)[-1]
        report['failure'] = {'type': type(error).__name__, 'line': line.lineno, 'function': line.name,
                             'detail': str(error).replace(configuration['key'], '[REDACTED]')[:1000]}
        raise RuntimeError(f'Acceptance failed. Evidence: {evidence}') from None
    finally:
        (primary.root/'.vault_key').unlink(missing_ok=True)
        evidence.write_text(json.dumps(report, indent=2), encoding='utf-8')


async def main(profile, rustfs):
    configuration = await source(profile)
    async with disposable_database(True) as database_url:
        await asyncio.to_thread(scenario, configuration, database_url, rustfs)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--rustfs-executable', type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.source_profile_id, args.rustfs_executable))
