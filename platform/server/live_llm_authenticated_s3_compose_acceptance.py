"""Run both Linux API replicas and an actual worker against authenticated SeaweedFS."""

import argparse
import asyncio
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlsplit

import boto3
from botocore.config import Config as S3Config
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.fernet import Fernet
import httpx

from live_llm_acceptance import FIXTURE, source
from prod_default_storage_acceptance import INFRA, call, docker, ready, register, wait, wait_job


def prepare(root, args):
    project = 'vf-j15-auth-' + secrets.token_hex(6)
    identity_file, vault_file, empty_env = [root / name for name in ('identity.json', 'vault.key', 'empty.env')]
    empty_env.touch()
    vault_file.write_bytes(Fernet.generate_key())
    bucket, region = project, 'us-west-2'
    admin, application = [(secrets.token_hex(12), secrets.token_hex(24)) for _ in range(2)]
    identities = [
        {'name': 'bootstrap', 'actions': ['Admin', 'Read', 'List', 'Write', 'Tagging'], 'credentials': [
            {'accessKey': admin[0], 'secretKey': admin[1]}]},
        {'name': 'application', 'actions': [f'{action}:{bucket}' for action in ('Read', 'List', 'Write', 'Tagging')],
         'credentials': [{'accessKey': application[0], 'secretKey': application[1]}]},
    ]
    identity_file.write_text(json.dumps({'identities': identities}), encoding='utf8')
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith(('VIBEFLOW_', 'COMPOSE_'))}
    env.update(PG_PASSWORD=secrets.token_hex(20), JWT_SECRET=secrets.token_hex(32),
               REDIS_PASSWORD=secrets.token_hex(24), PROVIDER_VAULT_FILE=str(vault_file),
               VIBEFLOW_STORAGE_BACKEND='s3', VIBEFLOW_ALLOW_PARTIAL_S3='true',
               VIBEFLOW_SEAWEEDFS_IMAGE=args.s3_image, VIBEFLOW_SEAWEEDFS_IDENTITY_FILE=str(identity_file),
               VIBEFLOW_S3_ENDPOINT='http://object-store:8333', VIBEFLOW_S3_BUCKET=bucket,
               VIBEFLOW_S3_REGION=region, VIBEFLOW_S3_ACCESS_KEY=application[0], VIBEFLOW_S3_SECRET_KEY=application[1],
               VIBEFLOW_REPLICA_API_IMAGE=args.api_image)
    prefixes = ['compose', '--env-file', str(empty_env), '-p', project, '--profile', 's3', '--profile', 'distributed']
    recipe = prefixes.copy()
    for file in ('docker-compose.prod.yml', 'docker-compose.replicas.yml', 'docker-compose.s3-auth.yml'):
        recipe.extend(('-f', str(INFRA / file)))
    missing_secret = {name: value for name, value in env.items() if name != 'VIBEFLOW_S3_SECRET_KEY'}
    absent = docker(*recipe, 'config', '--quiet', env=missing_secret, check=False)
    assert absent.returncode and 'VIBEFLOW_S3_SECRET_KEY' in absent.stderr
    rendered = json.loads(docker(*recipe, 'config', '--format', 'json', env=env).stdout)
    services = rendered['services']
    assert set(services) == {'api', 'api-peer', 'arq-worker', 'postgres', 'redis', 'seaweedfs', 'web'}
    for service in ('api', 'api-peer', 'arq-worker'):
        current = services[service]['environment']
        assert all(current[key] == env[key] for key in (
            'VIBEFLOW_S3_ENDPOINT', 'VIBEFLOW_S3_BUCKET', 'VIBEFLOW_S3_REGION',
            'VIBEFLOW_S3_ACCESS_KEY', 'VIBEFLOW_S3_SECRET_KEY'))
        services[service].pop('build', None)
    services['web'].pop('build', None)
    services['web']['image'] = args.web_image
    # All addresses published by the verification stack are loopback-only.
    ports = {'web': 80, 'seaweedfs': 8333, 'api': 8000, 'api-peer': 8000}
    for role, service in services.items():
        service.pop('extends', None)
        service['container_name'] = project + '-' + role
        if role in ports:
            service['ports'] = [f'127.0.0.1::{ports[role]}']
    services['seaweedfs']['networks'] = {'default': {'aliases': ['object-store']}}
    # Peer recreation starts without a case cache while sharing SQL and the same bucket.
    peer = services['api-peer']
    peer['volumes'] = [mount for mount in peer['volumes'] if mount['target'] != '/data/files']
    peer['tmpfs'] = ['/data/files:rw,size=256m']
    observer = root / 'observer'
    observer.mkdir()
    (observer / 'live_fixture.py').write_text('live_request_limit=4\n' + FIXTURE, encoding='utf8')
    peer['volumes'].append({'type': 'bind', 'source': str(observer), 'target': '/acceptance'})
    peer['command'] = ['python', '-m', 'uvicorn', 'live_fixture:app', '--app-dir', '/acceptance',
                       '--host', '0.0.0.0', '--port', '8000', '--no-access-log']
    assert all(volume['name'].startswith(project + '_') and not volume.get('external')
               for volume in rendered['volumes'].values())
    manifest = root / 'private-compose.json'
    manifest.write_text(json.dumps(rendered), encoding='utf8')
    files = (identity_file, vault_file, empty_env, manifest)
    return project, env, [*prefixes, '-f', str(manifest)], ports, admin, application, observer, files


def signed_client(endpoint, identity):
    return boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=identity[0], aws_secret_access_key=identity[1],
                        region_name='us-west-2', config=S3Config(
                            signature_version='s3v4', s3={'addressing_style': 'path'}, proxies={},
                            connect_timeout=2, read_timeout=8, retries={'total_max_attempts': 1}))


def denied(operation):
    try:
        operation()
    except ClientError as error:
        assert error.response['ResponseMetadata']['HTTPStatusCode'] == 403
        return
    raise AssertionError('A request outside the S3 identity scope was accepted')


def verify(root, args, configuration):
    endpoint = urlsplit(configuration['base_url'])
    assert endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
    before = set(docker('ps', '-q', '--no-trunc').stdout.splitlines())
    project, env, command, ports, admin, identity, observer, private_files = prepare(root, args)
    bucket = env['VIBEFLOW_S3_BUCKET']
    forbidden_bucket = bucket + '-control'
    result = {'passed': False, 'checks': [], 'bucket': bucket, 'simulated_model_calls': 0}

    def compose(*arguments, check=True, **kwargs):
        completed = docker(*command, *arguments, env=env, check=False, **kwargs)
        if check and completed.returncode:
            error = completed.stderr
            for secret in (*admin, *identity, configuration['key'], env['PG_PASSWORD'],
                           env['JWT_SECRET'], env['REDIS_PASSWORD']):
                error = error.replace(secret, '[REDACTED]')
            raise RuntimeError(f'Compose {arguments[0]} failed with code {completed.returncode}\n{error[-2000:]}')
        return completed

    def address(role):
        binding = compose('port', role, str(ports[role])).stdout.strip()
        assert binding.startswith('127.0.0.1:')
        return 'http://' + binding

    def check(name):
        result['checks'].append(name)
        print('S3_COMPOSE_PASS', name, flush=True)

    try:
        check('replica and worker use the selected endpoint bucket region and identity')
        compose('up', '-d', '--no-build', 'postgres', 'redis', 'seaweedfs')
        wait(lambda: docker('inspect', project+'-postgres', '--format', '{{.State.Health.Status}}')
             .stdout.strip() == 'healthy', 'Disposable PostgreSQL did not become healthy')
        s3_url = address('seaweedfs')
        with closing(signed_client(s3_url, admin)) as bootstrap, closing(signed_client(s3_url, identity)) as store:
            def s3_ready():
                try:
                    bootstrap.list_buckets()
                    return True
                except (BotoCoreError, ClientError):
                    return False

            wait(s3_ready, 'Authenticated SeaweedFS did not become ready')
            for name in (bucket, forbidden_bucket):
                bootstrap.create_bucket(Bucket=name)
            denied(lambda: store.list_objects_v2(Bucket=forbidden_bucket))
            assert store.head_bucket(Bucket=bucket)['ResponseMetadata']['HTTPStatusCode'] == 200
            check('scoped identity can access the existing bucket and is denied another bucket')
            compose('run', '--rm', '--no-deps', 'api', 'python', '-m', 'alembic', 'upgrade', 'head')
            compose('up', '-d', '--no-build', 'api', 'api-peer', 'arq-worker', 'web', timeout=180)
            with ExitStack() as clients:
                a, peer, web = [
                    clients.enter_context(httpx.Client(base_url=address(role), timeout=85, trust_env=False))
                    for role in ('api', 'api-peer', 'web')]
                for client in (a, peer, web):
                    wait(lambda: ready(client), 'A production application route did not become healthy')
                register(web)
                for client in (a, peer):
                    client.headers['Authorization'] = web.headers['Authorization']
                workspace = call(a, 'GET', '/workspaces').json()[0]['id']
                case = call(a, 'POST', f'/workspaces/{workspace}/cases', {'name': 'Authenticated S3 case'}).json()
                case_route = f'/cases/{case["id"]}'
                prefix = call(a, 'GET', case_route).json()['storage_prefix']
                path = 'Ref/storage-proof.txt'
                nonce = 'COMPOSE_S3_' + secrets.token_hex(16)
                call(a, 'PUT', case_route+'/file', {'path': path, 'content': nonce})
                raw_request = {'params': {'path': path}}
                assert call(peer, 'GET', case_route+'/raw', **raw_request).content == nonce.encode()
                assert call(web, 'GET', case_route+'/raw', expected=206,
                            headers={'Range': 'bytes=0-9'}, **raw_request).content == nonce.encode()[:10]
                keys = store.list_objects_v2(Bucket=bucket, Prefix=prefix+'/')['Contents']
                head = next(item['Key'] for item in keys if item['Key'].endswith('/.vibeflow/live-store/head.json'))
                with closing(store.get_object(Bucket=bucket, Key=head)['Body']) as stream:
                    member = json.load(stream)['files'][path]
                with closing(store.get_object(Bucket=bucket, Key=member['key'])['Body']) as stream:
                    content = stream.read()
                assert content == nonce.encode()
                anonymous = httpx.get(f'{s3_url}/{bucket}/{member["key"]}', trust_env=False)
                assert anonymous.status_code == 403
                result['file_sha256'] = hashlib.sha256(content).hexdigest()
                check('real immutable object agrees with peer read and signed API range')

                publications = []
                for size in (1, 2):
                    script = ('import cadquery as cq\n'
                              f'def build_geometry(ctx):\n return {{"box": cq.Workplane("XY").box({size}, 1, 1)}}\n')
                    saved = call(a, 'PUT', case_route+'/file',
                                 {'path': 'A-geo/geometry.py', 'content': script}).json()
                    job = saved.get('auto_preview_job') or call(a, 'POST', case_route+'/geometry:preview').json()['job_id']
                    finished = wait_job(a, job, 'succeeded')
                    worker = docker('inspect', project+'-arq-worker', '--format', '{{.Config.Hostname}}').stdout.strip()
                    assert finished['worker'].startswith(worker + ':')
                    scene = call(a, 'GET', case_route+'/geometry/scene').json()
                    publications.append(scene['publication']['id'])
                assert len(set(publications)) == 2
                check('actual CadQuery jobs publish two versions through the Redis worker')

                pressure = call(peer, 'GET', case_route+'/geometry/scene', expected=409).json()
                assert pressure['detail']['code'] == 'node_storage_full'
                check('cold peer reports its local cache capacity error')
                before_peer = docker('inspect', project+'-api-peer', '--format', '{{.Id}}').stdout.strip()
                manifest = private_files[-1]
                spec = json.loads(manifest.read_text('utf8'))
                spec['services']['api-peer']['tmpfs'] = ['/data/files:rw,size=512m']
                manifest.write_text(json.dumps(spec), encoding='utf8')
                compose('up', '-d', '--no-build', '--no-deps', '--force-recreate', 'api-peer')
                assert before_peer != docker('inspect', project+'-api-peer', '--format', '{{.Id}}').stdout.strip()
                peer.base_url = address('api-peer')
                wait(lambda: ready(peer), 'Recreated replica did not become healthy')
                assert call(peer, 'GET', case_route+'/raw', **raw_request).content == nonce.encode()
                for publication in publications:
                    scene = call(peer, 'GET', case_route+'/geometry/scene',
                                 params={'publication_id': publication}).json()
                    assert scene['publication']['id'] == publication
                result['publications'] = publications
                check('cold replica reads current files and both publication generations')

                model = configuration['model']
                profile = call(peer, 'POST', '/provider-profiles', {
                    'provider': 'deepseek', 'name': 'Authenticated S3 acceptance',
                    'default_model': model, 'base_url': configuration['base_url'], 'api_key': configuration['key'],
                }).json()['id']
                conversation = call(peer, 'POST', '/conversations', {
                    'case_id': case['id'], 'title': 'Real storage read', 'provider_profile_id': profile,
                    'model': model, 'permission_mode': 'plan',
                }).json()['id']
                with peer.stream('POST', f'/api/v1/conversations/{conversation}/messages', json={
                    'content': f'Use read_text_file to read {path}. Reply with the exact marker from the tool result.',
                    'provider_profile_id': profile, 'model': model,
                }) as response:
                    response.raise_for_status()
                    events = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith('data:')]
                assert nonce in ''.join(event['text'] for event in events if event['type'] == 'text.delta')
                assert {'tool.result', 'run.completed'} <= {event['type'] for event in events}
                upstream = [json.loads(line) for line in (observer/'upstream.jsonl').read_text('utf8').splitlines()]
                calls = [event for event in upstream if event['kind'] == 'http']
                assert len(calls) >= 2 and all(row['status'] == 200 and row['host'] == 'api.deepseek.com' for row in calls)
                result['real_model_http_200'] = len(calls)
                result['model'] = model
                check('real DeepSeek reads the case through the recreated S3 replica')
            result['passed'] = True
    finally:
        removed = compose('down', '--volumes', check=False, timeout=150)
        result['removed'] = removed.returncode == 0
        if result['removed']:
            for file in private_files:
                file.unlink(missing_ok=True)
        result['existing_services_preserved'] = before == set(docker('ps', '-q', '--no-trunc').stdout.splitlines())
        (root/'result.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf8')
    assert result['removed'] and result['existing_services_preserved']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-image', required=True)
    parser.add_argument('--web-image', required=True)
    parser.add_argument('--s3-image', required=True)
    parser.add_argument('--source-profile-id', required=True, type=int)
    options = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='j15-auth-compose-', dir=INFRA.parents[1]/'tmp'))
    print('S3_COMPOSE_EVIDENCE', root, flush=True)
    verify(root, options, asyncio.run(source(options.source_profile_id)))
