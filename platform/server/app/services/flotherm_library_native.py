"""Owned wmake compilation with a private OpenFOAM user-library directory."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mpi_native import digest, safe, inventory, path_mapping, verify_distribution


def invoke(argv, cwd, log, budget):
    print('FLOTHERM_STEP '+Path(argv[0]).name, flush=True)
    with log.open('wb') as stream:
        child = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            while data := child.stdout.read1(65536):
                budget[0] -= len(data)
                if budget[0] < 0:
                    raise ValueError('FloTHERM compilation log budget exceeded')
                stream.write(data)
                stream.flush()
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            code = child.wait()
            if code:
                raise ValueError('FloTHERM tool failed: '+Path(argv[0]).name+' exit '+str(code))
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()
    return {'argv': argv, 'returncode': code, 'log': digest(log)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    path = Path(args.config).resolve()
    if digest(path)['sha256'] != args.sha256:
        raise ValueError('FloTHERM compilation configuration changed')
    config = json.loads(path.read_text(encoding='utf8'))
    root = path.parents[2]
    if path.relative_to(root).as_posix() != '.vibeflow/prepared/flotherm-library.json':
        raise ValueError('FloTHERM configuration outside private root')
    if digest(Path(__file__).with_name('mpi_native.py'))['sha256'] != config['common_native_sha256']:
        raise ValueError('FloTHERM native file helper changed')
    identifier = config['execution_id']
    verify_distribution(config['distro'])
    if (str(uuid.UUID(identifier)) != identifier
            or (config['distro'] != 'native-linux' and os.getuid() == 0)):
        raise ValueError('Invalid FloTHERM native identity')
    stage = Path('/tmp')/('vibeflow-flotherm-library-'+identifier)
    stage.mkdir(mode=0o700, exist_ok=False)
    proof = root/'.vibeflow/flotherm-library'
    proof.mkdir(parents=True, exist_ok=True)
    ownership = {'schema': 'vibeflow.flotherm-library-stage.v1', 'execution_id': identifier,
        'path': str(stage), 'uid': os.getuid(), 'device': stage.stat().st_dev,
        'inode': stage.stat().st_ino, 'removed': False}
    (proof/'stage.json').write_text(json.dumps(ownership), encoding='utf8')
    def interrupted(*_):
        raise InterruptedError('FloTHERM compilation interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        translate = path_mapping(config['destination_dir'], identifier)
        entries = set(config['source']['directories'])|set(config['source']['files'])
        if len({translate(p) for p in entries}) != len(entries):
            raise ValueError('FloTHERM native path collision')
        for directory in config['source']['directories']:
            safe(stage, translate(directory)).mkdir(parents=True, exist_ok=True)
        for name, expected in config['source']['files'].items():
            source = safe(root, name)
            target = safe(stage, translate(name))
            target.parent.mkdir(parents=True, exist_ok=True)
            if digest(source) != {k: expected[k] for k in ('size', 'sha256')}:
                raise ValueError('FloTHERM sealed source changed')
            shutil.copyfile(source, target)
            target.chmod(0o755 if expected['executable'] else 0o644)
        before = inventory(stage)
        source = safe(stage, translate(config['source_dir']))
        destination = safe(stage, translate(config['destination_dir']))
        if config['source_dir'] != config['destination_dir']:
            shutil.copytree(source, destination)
        before_compile = inventory(stage)
        project = Path(os.environ['WM_PROJECT_DIR']).resolve()
        options = os.environ['WM_OPTIONS']
        if not options or Path(options).name != options:
            raise ValueError('Invalid OpenFOAM build cache directory')
        tools = {name: shutil.which(name) for name in ('wmake', 'g++')}
        if (not all(tools.values()) or not os.environ.get('WM_PROJECT_VERSION')
                or not Path(tools['wmake']).resolve().is_relative_to(project/'wmake')):
            raise ValueError('FloTHERM compiler differs from selected OpenFOAM release')
        fingerprints = {name: {'path': value, **digest(Path(value))} for name, value in tools.items()}
        # Recreate objects, not user libraries: dependencies may include existing
        # shared libraries, while fresh objects force the selected target to link.
        for target in (destination/'Make'/options, destination/'lnInclude'):
            if target.exists():
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError('FloTHERM cache escapes destination')
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        userlib = stage/'.vibeflow/userlib'
        userlib.mkdir(parents=True)
        os.environ['FOAM_USER_LIBBIN'] = str(userlib)
        budget = [config['max_log_bytes']]
        steps = {'build': invoke([tools['wmake'], '-j', '1', 'libso'], destination,
                                 destination/'flotherm-build.log', budget)}
        compiled = inventory(stage)
        libraries = {p: safe(stage, p) for p, value in compiled.items()
                     if p.endswith('.so') and before_compile.get(p) != value}
        installed = {}
        for name in inventory(userlib):
            if not name.endswith('.so'):
                continue
            target = destination/name
            relative = target.relative_to(stage).as_posix()
            if relative in libraries:
                raise ValueError('Conflicting local and FOAM_USER_LIBBIN outputs')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe(userlib, name), target)
            installed[translate(relative, reverse=True)] = name
            libraries[relative] = target
        if not libraries:
            raise ValueError('FloTHERM compilation produced no fresh shared library')
        for path in libraries.values():
            with path.open('rb') as stream:
                if stream.read(4) != b'\x7fELF':
                    raise ValueError('FloTHERM output library is not ELF')
        libbin = Path(os.environ['FOAM_LIBBIN'])
        preloads = [libbin/name for name in ('libOpenFOAM.so', 'libfiniteVolume.so',
                    'libmeshTools.so', 'libfileFormats.so', 'libsurfMesh.so', 'sys-openmpi/libPstream.so')]
        if not all(p.is_file() for p in preloads):
            raise ValueError('FloTHERM loading libraries unavailable')
        loader = sys.executable if config['distro'] == 'native-linux' else '/usr/bin/python3'
        fingerprints['loader'] = {'path': loader, **digest(Path(loader))}
        fingerprints['preloads'] = [{'path': str(p), **digest(p)} for p in preloads]
        directories = sorted({str(p.parent) for p in libraries.values()}|{str(userlib)})
        os.environ['LD_LIBRARY_PATH'] = ':'.join(directories)+':'+os.environ.get('LD_LIBRARY_PATH', '')
        load = ['/usr/bin/env', 'LD_PRELOAD='+':'.join(map(str, preloads)), loader, '-I', '-S', '-c',
            'import ctypes,sys;[(ctypes.CDLL(p,mode=ctypes.RTLD_GLOBAL),print("FLOTHERM_LIBRARY_LOADED",p,flush=True)) for p in sys.argv[1:]]',
            *map(str, libraries.values())]
        steps['load'] = invoke(load, destination, destination/'flotherm-load.log', budget)
        after = inventory(stage)
        changed = {p: r for p, r in after.items() if before.get(p) != r}
        for name in changed:
            target = safe(root, translate(name, reverse=True))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe(stage, name), target)
        deleted = sorted(set(before)-set(after))
        for name in deleted:
            safe(root, translate(name, reverse=True)).unlink()
        receipt = {'schema': 'vibeflow.flotherm-library-native.v1', 'execution_id': identifier,
            'source_dir': config['source_dir'], 'destination_dir': config['destination_dir'],
            'distro': config['distro'], 'uid': os.getuid(), 'openfoam': os.environ['WM_PROJECT_VERSION'],
            'wm_options': options, 'tools': fingerprints, 'steps': steps,
            'libraries': {translate(p, reverse=True): digest(path) for p, path in libraries.items()},
            'private_installations': installed,
            'changed': {translate(p, reverse=True): {k: r[k] for k in ('size', 'sha256')} for p, r in changed.items()},
            'deleted': sorted(translate(p, reverse=True) for p in deleted),
            'helper_sha256': digest(Path(__file__))['sha256'], 'common_native_sha256': config['common_native_sha256']}
        with (proof/'native.json').open('x', encoding='utf8') as stream:
            json.dump(receipt, stream, sort_keys=True)
    finally:
        if stage.exists() and stage.parent == Path('/tmp') and stage.name == 'vibeflow-flotherm-library-'+identifier:
            shutil.rmtree(stage)
        ownership['removed'] = not stage.exists()
        (proof/'stage.json').write_text(json.dumps(ownership), encoding='utf8')


if __name__ == '__main__':
    main()
