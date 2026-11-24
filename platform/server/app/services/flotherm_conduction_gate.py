"""Prepare and verify the native mesh of a generated conduction case."""

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT/'mesh_gate.json'
MESH = ROOT/'constant/polyMesh'
FILES = ('boundary', 'faces', 'neighbour', 'owner', 'points')


def read(name):
    return json.loads((ROOT/name).read_text(encoding='utf-8'))


def record(data):
    if GATE.is_symlink():
        raise ValueError('Expected a regular mesh_gate.json')
    with tempfile.NamedTemporaryFile(mode='w', dir=ROOT, encoding='utf-8',
                                     prefix='.flotherm-mesh-', delete=False) as stream:
        staged = Path(stream.name)
        try:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.close()
            staged.replace(GATE)
        finally:
            staged.unlink(missing_ok=True)


def exists(name):
    return any(path.is_file() and not path.is_symlink() and path.stat().st_size
               for path in (MESH/name, MESH/(name+'.gz')))


def run(name, *arguments):
    log = ROOT/f'log.{name}'
    if log.is_symlink():
        raise ValueError(f'Expected a regular {log.name}')
    with log.open('wb') as stream:
        result = subprocess.run([name, *arguments], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError(f'{name} failed with status {result.returncode}. See {log.name}.')
    print(f'{name} finished. See {log.name}.', flush=True)
    return log.read_text(encoding='utf-8', errors='replace')


def prepare():
    record({'ok': False, 'reason': 'Preparing conduction mesh'})
    try:
        run('blockMesh')
        zones = run('topoSet')
        report = run('checkMesh')
        if ('Mesh OK.' not in report or re.search(r'Failed \d+ mesh checks|FOAM FATAL',report)
                or not all(map(exists, FILES))):
            raise ValueError('Conduction mesh did not pass checkMesh. See log.checkMesh.')
        expected = {row['zone'] for row in read('case-manifest.json')['heat_sources']}
        observed = {zone: int(count) for zone,count in re.findall(
            r'cellZoneSet (\w+) now size (\d+)', zones)}
        if any(observed.get(zone,0) <= 0 for zone in expected):
            raise ValueError('A conduction heat source has an empty or missing cellZone. See log.topoSet.')
        record({'ok': True, 'log': 'log.checkMesh', 'heat_source_cells': {
            zone: observed[zone] for zone in sorted(expected)}})
    except (OSError, ValueError) as error:
        record({'ok': False, 'reason': str(error)})
        raise


def main():
    if not read('preflight.json')['ok']:
        raise ValueError('FloTHERM preflight has unresolved blockers. See preflight.json.')
    if sys.argv[1:]==['--require-ready']:
        receipt = read('mesh_gate.json')
        required = ('cellZones',) if receipt.get('heat_source_cells') else ()
        if not receipt.get('ok') or not all(map(exists, (*FILES,*required))):
            raise ValueError('Run Allrun.pre and pass checkMesh before solving.')
    elif not sys.argv[1:]:
        prepare()
    else:
        raise ValueError('Expected --require-ready or no argument')


if __name__=='__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f'FloTHERM mesh: {error}')
