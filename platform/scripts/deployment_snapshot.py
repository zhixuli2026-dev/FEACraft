"""Cold Docker Compose snapshot and isolated restore for the platform stack.

Input is private JSON from `docker compose config --format json`. Backup includes
secrets and user data: keep the entire output private, outside release archives.
PostgreSQL uses pg_dump; other named volumes are copied after writers stop.
No model calls. No volume deletion, overwrite, schema downgrade or source reset.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf8')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def command(argv, *, output=None, input_file=None):
    result = subprocess.run(argv, stdout=output or subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=input_file, timeout=300)
    if result.returncode:
        # Configuration and command arguments can contain deployment secrets.
        raise RuntimeError(f'{argv[0]} {argv[1]} failed with exit {result.returncode}')
    return result.stdout.decode('utf8') if output is None else ''


def inspect(name):
    return json.loads(command(['docker', 'inspect', name]))[0]


def sql(container, statement):
    env = dict(item.split('=', 1) for item in container['Config']['Env'])
    return command(['docker', 'exec', container['Id'], 'psql', '-U', env['POSTGRES_USER'],
                    '-d', env['POSTGRES_DB'], '-tAc', statement]).strip()


def check_topology(config):
    services = config['services']
    required = {'api', 'postgres', 'web'}
    supported = required | {'arq-worker', 'redis', 'seaweedfs'}
    if not required <= set(services) <= supported:
        raise ValueError('Expected the platform API, PostgreSQL, Web and their optional platform services')
    for name in ('api', 'arq-worker'):
        env = services.get(name, {}).get('environment', {})
        if env.get('VIBEFLOW_STORAGE_BACKEND', 'fs') == 's3':
            endpoint = urlsplit(env.get('VIBEFLOW_S3_ENDPOINT', ''))
            if endpoint.hostname != 'seaweedfs' or 'seaweedfs' not in services:
                raise ValueError('External object storage needs a coordinated storage backup')


def snapshot(compose_path, destination):
    config = json.loads(compose_path.read_text('utf8'))
    check_topology(config)
    services = config['services']
    containers = {name: inspect(service['container_name']) for name, service in services.items()}
    project_ids = command(['docker', 'ps', '--no-trunc', '--quiet', '--filter',
                           'label=com.docker.compose.project=' + config['name']]).splitlines()
    if set(project_ids) != {c['Id'] for c in containers.values()}:
        raise ValueError('Compose configuration must include every running service of the source project')
    assert all(c['State']['Running'] for c in containers.values()), 'All source services must be running'
    assert sql(containers['postgres'], "SELECT count(*) FROM jobs WHERE status IN ('queued','running')") == '0', 'Active jobs'
    assert sql(containers['postgres'], "SELECT count(*) FROM native_cli_sessions WHERE phase NOT IN ('closed','failed')") == '0', 'Active/recovering CLI sessions'
    # Bind mounts other than Docker secrets need an explicit backup strategy.
    for container in containers.values():
        assert all(m['Type'] == 'volume' or m['Destination'].startswith('/run/secrets/')
                   for m in container['Mounts']), 'Non-secret bind mount requires separate backup'
    mounted_volumes = {m['Name'] for c in containers.values() for m in c['Mounts'] if m['Type'] == 'volume'}
    active_volumes = {k: v for k, v in config.get('volumes', {}).items()
                      if v.get('name', config['name'] + '_' + k) in mounted_volumes}
    if mounted_volumes != {v.get('name', config['name'] + '_' + k) for k, v in active_volumes.items()}:
        raise ValueError('The active volumes differ from the supplied Compose configuration')
    config['volumes'] = active_volumes
    destination.mkdir(parents=True, exist_ok=False)
    private = destination / 'private'
    private.mkdir(mode=0o700)
    write(private / 'source-compose.json', config)
    secrets = {}
    for name, definition in config.get('secrets', {}).items():
        path = Path(definition['file'])
        if not path.is_absolute():
            path = compose_path.parent / path
        target = private / ('secret-' + name)
        target.write_bytes(path.read_bytes())
        secrets[name] = target.name
    result = {'schema': 'vibeflow.deployment-snapshot.v1', 'complete': False,
              'source_project': config['name'], 'images': {n: c['Image'] for n, c in containers.items()},
              'database_revision': sql(containers['postgres'], 'SELECT version_num FROM alembic_version'),
              'volumes': {}, 'secrets': secrets, 'source_restarted': False}
    write(destination / 'manifest.json', result)
    stopped = [n for n in ('arq-worker', 'api', 'web', 'redis', 'seaweedfs') if n in containers]
    try:
        command(['docker', 'stop', '--time', '120', *(containers[n]['Id'] for n in stopped)])
        assert all(not inspect(containers[n]['Id'])['State']['Running'] for n in stopped)
        # Recheck after admission is stopped; do not back up an unfinished job.
        assert sql(containers['postgres'], "SELECT count(*) FROM jobs WHERE status IN ('queued','running')") == '0'
        pg_env = dict(e.split('=', 1) for e in containers['postgres']['Config']['Env'])
        with (private / 'database.dump').open('xb') as stream:
            command(['docker', 'exec', containers['postgres']['Id'], 'pg_dump', '-U', pg_env['POSTGRES_USER'],
                     '-d', pg_env['POSTGRES_DB'], '--format=custom', '--no-owner', '--no-acl'], output=stream)
        for key, definition in config.get('volumes', {}).items():
            physical = definition.get('name', config['name'] + '_' + key)
            if any(m.get('Name') == physical for m in containers['postgres']['Mounts']):
                continue  # PostgreSQL is restored logically, never from a live data directory.
            mount = {'key': key, 'source_name': physical, 'archive': key + '.tar'}
            assert any(m.get('Name') == physical for c in containers.values() for m in c['Mounts'])
            with (private / mount['archive']).open('xb') as stream:
                command(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'tar',
                         '-v', physical + ':/snapshot:ro', containers['api']['Image'],
                         '-C', '/snapshot', '-cpf', '-', '.'], output=stream)
            result['volumes'][key] = mount
        result['files'] = {p.name: {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in private.iterdir() if p.is_file()}
        result['complete'] = True
    finally:
        # Restore every original service even if snapshotting failed midway.
        for name in ('seaweedfs', 'redis', 'api', 'arq-worker', 'web'):
            if name in containers:
                command(['docker', 'start', containers[name]['Id']])
        result['source_restarted'] = all(inspect(containers[n]['Id'])['State']['Running'] for n in stopped)
        write(destination / 'manifest.json', result)
    assert result['complete'] and result['source_restarted']
    print(json.dumps({'snapshot': str(destination), 'complete': True, 'source_restarted': True,
                      'volumes': len(result['volumes']), 'bytes': sum(f['bytes'] for f in result['files'].values())}), flush=True)


def restore(snapshot_dir, project, port):
    assert re.fullmatch(r'vf-restore-[a-z0-9-]{1,48}', project), 'Use a new vf-restore- project name'
    assert 1024 <= port <= 65535
    manifest = json.loads((snapshot_dir / 'manifest.json').read_text('utf8'))
    assert manifest['complete'] and manifest['source_restarted']
    private = snapshot_dir / 'private'
    for name, record in manifest['files'].items():
        assert Path(name).name == name and digest(private / name) == record['sha256'], 'Snapshot hash mismatch'
    config = copy.deepcopy(json.loads((private / 'source-compose.json').read_text('utf8')))
    check_topology(config)
    assert project != config['name']
    config['name'] = project
    for name, service in config['services'].items():
        service['container_name'] = project + '-' + name
        service['image'] = manifest['images'][name]
        service.pop('build', None)
        service.pop('profiles', None)
        if name == 'web':
            service['ports'] = [{'target': 80, 'published': port, 'host_ip': '127.0.0.1', 'protocol': 'tcp'}]
        else:
            assert not service.get('ports'), 'Unexpected published service port'
    for name, definition in config['volumes'].items():
        definition['name'] = project + '_' + name
        assert not definition.get('external')
    for name, definition in config.get('networks', {}).items():
        assert not definition.get('external')
        definition['name'] = project + '_' + name
    for name, definition in config.get('secrets', {}).items():
        definition['file'] = str((private / manifest['secrets'][name]).resolve())
    # Refuse collisions before Compose can reuse any previous state.
    existing_containers = command(['docker', 'ps', '-a', '--format', '{{.Names}}']).splitlines()
    existing_volumes = command(['docker', 'volume', 'ls', '--format', '{{.Name}}']).splitlines()
    assert not any(s['container_name'] in existing_containers for s in config['services'].values())
    assert not any(v['name'] in existing_volumes for v in config['volumes'].values())
    target = private / ('restore-' + project + '.json')
    assert not target.exists()
    write(target, config)
    compose = ['docker', 'compose', '-p', project, '-f', str(target)]
    command([*compose, 'config', '--quiet'])
    # Create target containers and volumes without starting consumers.
    command([*compose, 'create'])
    for name, record in manifest['volumes'].items():
        volume = config['volumes'][name]['name']
        with (private / record['archive']).open('rb') as stream:
            command(['docker', 'run', '--rm', '-i', '--network', 'none', '--entrypoint', 'tar',
                     '-v', volume + ':/snapshot', manifest['images']['api'],
                     '-C', '/snapshot', '-xpf', '-'], input_file=stream)
    command([*compose, 'start', 'postgres'])
    pg = inspect(project + '-postgres')
    until = time.monotonic() + 90
    while inspect(project + '-postgres')['State'].get('Health', {}).get('Status') != 'healthy':
        assert time.monotonic() < until, 'Restored PostgreSQL startup timeout'
        time.sleep(1)
    env = dict(e.split('=', 1) for e in pg['Config']['Env'])
    with (private / 'database.dump').open('rb') as stream:
        command(['docker', 'exec', '-i', pg['Id'], 'pg_restore', '-U', env['POSTGRES_USER'],
                 '-d', env['POSTGRES_DB'], '--no-owner', '--no-acl', '--exit-on-error'], input_file=stream)
    assert sql(pg, 'SELECT version_num FROM alembic_version') == manifest['database_revision']
    storage_services = [name for name in ('redis', 'seaweedfs') if name in config['services']]
    if storage_services:
        command([*compose, 'start', *storage_services])
    # Start API only after its storage is healthy. Worker/web follow API health.
    for service in ('redis', 'seaweedfs'):
        if service not in config['services']:
            continue
        until = time.monotonic() + 90
        while inspect(project + '-' + service)['State'].get('Health', {}).get('Status') not in ('healthy', None):
            assert time.monotonic() < until, service + ' startup timeout'
            time.sleep(1)
    command([*compose, 'start', 'api'])
    until = time.monotonic() + 120
    while inspect(project + '-api')['State'].get('Health', {}).get('Status') != 'healthy':
        assert time.monotonic() < until, 'Restored API startup timeout'
        time.sleep(1)
    command([*compose, 'start', *(name for name in ('arq-worker', 'web') if name in config['services'])])
    result = {'restored': True, 'project': project, 'port': port, 'database_revision': manifest['database_revision'],
              'volumes_restored': len(manifest['volumes']), 'images': manifest['images']}
    write(snapshot_dir / ('restore-' + project + '-result.json'), result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    backup = sub.add_parser('snapshot')
    backup.add_argument('--compose', type=Path, required=True)
    backup.add_argument('--destination', type=Path, required=True)
    recover = sub.add_parser('restore')
    recover.add_argument('--snapshot', type=Path, required=True)
    recover.add_argument('--project', required=True)
    recover.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    if args.action == 'snapshot':
        snapshot(args.compose.resolve(), args.destination.resolve())
    else:
        restore(args.snapshot.resolve(), args.project, args.port)
