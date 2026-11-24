"""Set the healthy internal API origins and gracefully reload the named Nginx."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import NamedTemporaryFile


CONTAINER_FILE = '/etc/nginx/vibeflow-upstreams/api.conf'
HOSTNAME = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')


def nginx(container, *command):
    result = subprocess.run(['docker', 'exec', container, *command], capture_output=True, timeout=12)
    if result.returncode:
        raise RuntimeError(f'Web container command failed: {command[0]}')
    return result.stdout


def replace(path, contents):
    mode = stat.S_IMODE(path.stat().st_mode)
    stream = NamedTemporaryFile(dir=path.parent, prefix='.upstream-', delete=False)
    pending = Path(stream.name)
    try:
        with stream:
            stream.write(contents)
        pending.chmod(mode)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def stage(container, directory, hosts):
    if not hosts or len(hosts) != len(set(hosts)) or any(not HOSTNAME.fullmatch(host) for host in hosts):
        raise ValueError('Supply one or more distinct internal API hostnames')
    target = directory.resolve(strict=True) / 'api.conf'
    if target.is_symlink() or not target.is_file():
        raise ValueError('The upstream directory must contain an ordinary api.conf file')
    desired = ''.join(f'server {host}:8000 resolve;\n' for host in hosts).encode('ascii')
    # Hold one local rollout writer while changing the file and signalling Nginx.
    with (target.parent / '.upstream-update.lock').open('a+b') as lock:
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        previous = target.read_bytes()
        if nginx(container, 'cat', CONTAINER_FILE) != previous:
            raise RuntimeError('The selected web container does not read this upstream file')
        for host in hosts:
            answer = nginx(container, 'wget', '-T', '4', '-qO-', f'http://{host}:8000/api/health')
            health = json.loads(answer)
            if not isinstance(health, dict) or health.get('ok') is not True:
                raise RuntimeError(f'API did not report ready: {host}')
        replace(target, desired)
        signalled = False
        try:
            if nginx(container, 'cat', CONTAINER_FILE) != desired:
                raise RuntimeError('The new upstream file is not visible in the web container')
            nginx(container, 'nginx', '-t')
            signalled = True
            nginx(container, 'nginx', '-s', 'reload')
        except BaseException:
            replace(target, previous)
            if signalled:
                nginx(container, 'nginx', '-t')
                nginx(container, 'nginx', '-s', 'reload')
            raise
    return {'upstreams': list(hosts), 'reload_requested': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web-container', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('hosts', nargs='+')
    args = parser.parse_args()
    try:
        print(json.dumps(stage(args.web_container, args.directory, args.hosts)))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
