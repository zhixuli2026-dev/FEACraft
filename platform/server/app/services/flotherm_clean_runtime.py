"""Remove generated runtime results from a FloTHERM OpenFOAM case."""

from decimal import Decimal
import gzip
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TIME_NAME = re.compile(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')


def directory(path):
    if path.is_symlink() or not path.is_dir():
        return False
    return all(not parent.is_symlink() for parent in path.parents if parent.is_relative_to(ROOT))


def native_file(path):
    for candidate in (path,path.with_name(path.name+'.gz')):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        opener = gzip.open if candidate.suffix=='.gz' else open
        try:
            with opener(candidate,'rb') as stream:
                if b'FoamFile' in stream.read(2048):
                    return True
        except (EOFError,gzip.BadGzipFile):
            continue
    return False


def read(name, schema):
    path = ROOT/name
    if path.is_symlink():
        raise ValueError(f'Expected generated case file: {name}')
    document = json.loads(path.read_text(encoding='utf8'))
    if document.get('schema')!=schema:
        raise ValueError(f'Expected generated case file: {name}')
    return document


def reset_gate(fluid):
    reason = '等待多区域 checkMesh' if fluid else '等待 blockMesh+topoSet 证据'
    gate = ROOT/'mesh_gate.json'
    if gate.is_symlink():
        raise ValueError('Expected generated mesh_gate.json')
    with tempfile.NamedTemporaryFile(mode='w',dir=ROOT,encoding='utf8',prefix='.mesh-gate-',delete=False) as stream:
        pending = Path(stream.name)
        try:
            json.dump({'ok':False,'reason':reason},stream)
            stream.write('\n')
            stream.close()
            pending.replace(gate)
        finally:
            pending.unlink(missing_ok=True)


def clean():
    read('.flotherm2foam-generated','vibeflow.flotherm2foam.generated.v1')
    manifest = read('case-manifest.json','vibeflow.flotherm2foam.case-manifest.v1')
    fluid = manifest.get('fluid_region')
    regions = [fluid,*manifest.get('sealed_cavities',()),*manifest.get('solid_regions',())] if fluid else ['']
    if any(name and not re.fullmatch(r'[A-Za-z0-9_]+',name) for name in regions):
        raise ValueError('Invalid generated region name')

    transient = [ROOT/'postProcessing',ROOT/'constant/polyMesh']
    if fluid:
        transient.extend(ROOT/'constant'/region/'polyMesh' for region in regions)
    for path in ROOT.iterdir():
        if not directory(path):
            continue
        if re.fullmatch(r'processor[0-9]+',path.name):
            for region in regions:
                mesh = path/'constant'/region/'polyMesh'
                if directory(mesh) and native_file(mesh/'cellProcAddressing'):
                    transient.append(path)
                    break
        if TIME_NAME.fullmatch(path.name) and Decimal(path.name)>0:
            for region in regions:
                fields = path/region
                if directory(fields) and any(native_file(fields/name) for name in ('T','p','U')):
                    transient.append(path)
                    break

    reset_gate(fluid)
    for path in transient:
        if directory(path):
            shutil.rmtree(path)
    for log in ROOT.glob('log.*'):
        if log.is_file() and not log.is_symlink():
            log.unlink()


if __name__=='__main__':
    try:
        clean()
    except (OSError,ValueError) as error:
        sys.exit(f'FloTHERM clean: {error}')
