"""Create/verify/extract an internal review package, including runtime static assets.

No application imports, model calls, dependency installation or service mutation.
This is not a user-data backup or a declaration of public distribution clearance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import zipfile

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = 'release-manifest.json'
LEGACY_SCHEMA = 'vibeflow.internal-release-review.v1'
SCHEMA = 'vibeflow.internal-release-review.v2'
TREES = ('platform/server/app', 'platform/server/migrations', 'platform/server/skills',
         'platform/server/examples', 'platform/server/data/completion',
         'platform/web/src', 'platform/web/public')
LEGACY_FILES = ('AGENTS.md', 'platform/.dockerignore', 'platform/.gitignore',
                'platform/server/requirements.txt', 'platform/server/alembic.ini',
                'platform/server/.env.example', 'platform/server/schema_diff.py',
                'platform/server/flotherm2foam.py', 'platform/web/package.json',
                'platform/web/package-lock.json', 'platform/web/index.html',
                'platform/web/tsconfig.json', 'platform/web/tsconfig.app.json',
                'platform/web/tsconfig.node.json', 'platform/web/vite.config.ts',
                'platform/web/.oxlintrc.json', 'platform/infra/.env.example',
                'platform/infra/docker-compose.prod.yml', 'platform/infra/docker-compose.yml',
                'platform/infra/Dockerfile.api', 'platform/infra/Dockerfile.web',
                'platform/infra/Dockerfile.sandbox', 'platform/infra/nginx-web.conf',
                'platform/infra/compose_smoke.py', 'platform/scripts/dev-up.ps1',
                'platform/tools/release_package.py')
FILES = (*LEGACY_FILES,
         'platform/server/requirements-linux-amd64-cp311.lock',
         'platform/server/requirements-windows-amd64-cp311.lock',
         'platform/infra/docker-compose.provider-kms.yml',
         'platform/infra/docker-compose.oidc.yml',
         'platform/infra/docker-compose.hpc.yml',
         'platform/infra/Dockerfile.cae', 'platform/infra/docker-compose.cae.yml',
         'platform/infra/Dockerfile.tex', 'platform/infra/docker-compose.tex.yml',
         'platform/infra/Dockerfile.calculix', 'platform/infra/docker-compose.calculix.yml',
         'platform/infra/Dockerfile.su2', 'platform/infra/docker-compose.su2.yml',
         'platform/infra/Dockerfile.paraview', 'platform/infra/docker-compose.paraview.yml',
         'platform/tools/fetch_paraview.py',
         'platform/infra/Dockerfile.node-cli', 'platform/infra/docker-compose.node-cli.yml',
         'platform/infra/cli-toolchain/package.json', 'platform/infra/cli-toolchain/package-lock.json',
         'platform/tools/fetch_node.py',
         'platform/infra/docker-compose.replicas.yml',
         'platform/infra/nginx-upstream-default/api.conf',
         'platform/infra/nginx-upstream-replicas/api.conf',
         'platform/tools/replica_upstreams.py',
         'platform/infra/docker-compose.client-updates.yml',
         'platform/infra/client-updates/maps/web.conf',
         'platform/infra/client-updates/locations/assets.conf',
         'platform/scripts/init-provider-vault.py',
         'platform/scripts/configure-windows-codex-sandbox.py',
         'platform/scripts/deployment_snapshot.py',
         'platform/scripts/maintain-docker-space.ps1',
         'platform/scripts/maintain-s3-versions.py',
         'platform/scripts/maintain-s3-publication-cache.py')
SKIP = {'__pycache__', 'node_modules', '.git', '.venv', 'dist', '.pytest_cache'}
TEXT = {'.py', '.sh', '.ps1', '.md', '.txt', '.json', '.yaml', '.yml', '.ini',
        '.toml', '.ts', '.tsx', '.js', '.mjs', '.html', '.css', '.conf', '.example'}
SECRETS = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
                     rb'\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|'
                     rb'AKIA[A-Z0-9]{16})\b')


OPTIONAL_FILES = ('platform/infra/docker-compose.paraview-wsl-gpu.yml',)


def allowed(name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
        return False
    if any(part in SKIP for part in p.parts):
        return False
    if p.name.startswith('.') and name not in FILES:
        return False
    if p.suffix.lower() in {'.pyc', '.pyo', '.log', '.db', '.sqlite', '.pem', '.key'}:
        return False
    return (name in FILES or name in OPTIONAL_FILES or any(name.startswith(t + '/') for t in TREES)
            or (str(p.parent) == 'platform/docs' and p.suffix in {'.md', '.json'}))


def sources(root):
    names = set(FILES)
    names.update(name for name in OPTIONAL_FILES if (root / name).is_file())
    for tree in TREES:
        for directory, dirs, files in os.walk(root / tree, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith('.')
                             and not (Path(directory) / d).is_symlink()
                             and not os.path.ismount(Path(directory) / d))
            for name in files:
                p = Path(directory) / name
                relative = p.relative_to(root).as_posix()
                if allowed(relative):
                    names.add(relative)
    names.update(p.relative_to(root).as_posix() for p in (root/'platform/docs').iterdir()
                 if p.is_file() and p.suffix in {'.md', '.json'})
    for name in sorted(names):
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Missing or linked source: ' + name)
        yield name, path


def check_content(name, data):
    if PurePosixPath(name).suffix in TEXT and SECRETS.search(data):
        # Never include the matched credential in diagnostics.
        raise ValueError('Possible credential/private key in package source: ' + name)


def put(archive, name, data):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def create(destination, root=ROOT):
    root = root.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    inventory = []
    with zipfile.ZipFile(destination, 'x') as archive:
        for name, path in sources(root):
            data = path.read_bytes()
            check_content(name, data)
            put(archive, name, data)
            inventory.append({'path': name, 'bytes': len(data),
                              'sha256': hashlib.sha256(data).hexdigest()})
        manifest = {'schema': SCHEMA,
                    'purpose': 'Internal review; public distribution and deployment acceptance incomplete',
                    'instructions': 'platform/docs/release-review.md',
                    'external_dependencies': 'platform/docs/third-party.md',
                    'excluded': 'Private config, keys, user data, dependencies, caches, historical test runners, extracted vendor binaries',
                    'files': inventory}
        put(archive, MANIFEST, json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                         indent=2).encode('utf-8'))
    with destination.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    destination.with_suffix(destination.suffix+'.sha256').write_text(digest+'\n', encoding='ascii')
    return {'files': len(inventory), 'bytes': destination.stat().st_size, 'sha256': digest}


def verify(path):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != path.with_suffix(path.suffix+'.sha256').read_text().strip():
        raise ValueError('Archive hash mismatch')
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(MANIFEST))
        schema = manifest.get('schema')
        if schema not in {LEGACY_SCHEMA, SCHEMA}:
            raise ValueError('Unsupported manifest')
        names = [i['path'] for i in manifest['files']]
        if len(names) != len(set(names)) or len(archive.namelist()) != len(names)+1:
            raise ValueError('Duplicate archive members')
        if set(archive.namelist()) != set(names) | {MANIFEST}:
            raise ValueError('Unlisted archive members')
        # Old packages are checked against their stable core and their full
        # inventoried contents. Current packages must carry the deployment inputs.
        files = LEGACY_FILES if schema == LEGACY_SCHEMA else FILES
        required = (*files, 'platform/server/app/main.py', 'platform/server/migrations/env.py')
        if not set(required).issubset(names):
            raise ValueError('Required deployment sources missing')
        for item in manifest['files']:
            name = item['path']
            if not allowed(name):
                raise ValueError('Unexpected package path: ' + name)
            data = archive.read(name)
            if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError('Source hash mismatch: ' + name)
            check_content(name, data)
    return manifest


def extract(path, destination):
    manifest = verify(path)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(path) as archive:
        for name in [i['path'] for i in manifest['files']] + [MANIFEST]:
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.read(name))
    return {'files': len(manifest['files']), 'destination': str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('create', 'verify', 'extract'))
    parser.add_argument('archive', type=Path)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if args.operation == 'create':
        result = create(args.archive)
    elif args.operation == 'verify':
        result = {'verified_files': len(verify(args.archive)['files'])}
    else:
        if args.destination is None:
            parser.error('extract requires a new --destination')
        result = extract(args.archive, args.destination)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
