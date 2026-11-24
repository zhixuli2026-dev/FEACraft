"""Prepare a generated OpenFOAM case to continue from its latest checkpoint."""

from decimal import Decimal
import json
from pathlib import Path
import os
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TIME_NAME = re.compile(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
ZERO = Decimal(0), '0'


def run(*args):
    inherited = ()
    try:
        if os.path.samestat(os.fstat(9), (ROOT/'.flotherm2foam-generated').stat()):
            inherited = (9,)
    except OSError:
        pass
    subprocess.run(args,cwd=ROOT,check=True,pass_fds=inherited)


def latest(directory):
    values = {}
    for path in directory.iterdir():
        if not TIME_NAME.fullmatch(path.name) or not path.is_dir():
            continue
        if path.is_symlink():
            raise ValueError(f'Checkpoint path may not be a symlink: {path.relative_to(ROOT)}')
        time = Decimal(path.name)
        if time in values and values[time] != path.name:
            raise ValueError(f'Ambiguous checkpoint times in {directory.relative_to(ROOT)}')
        values[time] = path.name
    return max(values.items(),default=ZERO)


def processor_dirs():
    paths = sorted((p for p in ROOT.iterdir() if re.fullmatch(r'processor[0-9]+',p.name)),
                   key=lambda p:int(p.name.removeprefix('processor')))
    if any(path.is_symlink() or not path.is_dir() for path in paths):
        raise ValueError('Processor paths must be regular case directories')
    return paths


def fields(manifest, time):
    if not manifest.get('fluid_region'):
        return {'':('T',)}
    fluid = ('p','p_rgh','U','T')
    if time[0]>0:
        fluid += ('rho','phi')
    if manifest.get('physics',{}).get('turbulence')=='realizableKE':
        fluid += ('k','epsilon','nut','alphat')
    regions = {name:fluid for name in (manifest['fluid_region'],*manifest.get('sealed_cavities',()))}
    regions.update({name:('p','T') for name in manifest['solid_regions']})
    if time[0]>0:
        devices = [row for row in manifest.get('flow_devices',()) if row['model']=='fixedVolumetricFan']
        regions[manifest['fluid_region']] += tuple(f'uniform/{row["source"]}Properties' for row in devices)
    return regions


def require_fields(directory, time, manifest):
    for region, names in fields(manifest,time).items():
        for name in names:
            field = directory/time[1]/region/name
            variants = (field,field.with_name(field.name+'.gz'))
            if not any(path.is_file() and not path.is_symlink() and path.stat().st_size for path in variants):
                raise ValueError(f'Incomplete latest checkpoint: {field.relative_to(ROOT)}')


def require_mesh(directory, manifest, *, processor=False):
    for region in fields(manifest,ZERO):
        mesh = directory/'constant'/region/'polyMesh'
        names = ('boundary','points','cellProcAddressing') if processor else ('boundary','points')
        for name in names:
            if not any((mesh/variant).is_file() for variant in (name,name+'.gz')):
                raise ValueError(f'Checkpoint mesh is incomplete: {(mesh/name).relative_to(ROOT)}')
    if directory==ROOT and manifest.get('fluid_region'):
        run(sys.executable,'system/channel_mesh.py','--require-ready')
    elif directory==ROOT and (ROOT/'system/flotherm_conduction_gate.py').is_file():
        run(sys.executable,'system/flotherm_conduction_gate.py','--require-ready')


def prepared(manifest):
    if manifest.get('fluid_region'):
        gate = ROOT/'mesh_gate.json'
        return gate.is_file() and json.loads(gate.read_text(encoding='utf8')).get('ok')
    if (ROOT/'system/flotherm_conduction_gate.py').is_file():
        return json.loads((ROOT/'mesh_gate.json').read_text(encoding='utf8')).get('ok')
    return (ROOT/'constant/polyMesh/boundary').is_file()


def prepare(manifest, mode):
    ranks = processor_dirs()
    root_time = latest(ROOT)
    rank_times = [latest(path) for path in ranks]
    if root_time[0]==0 and not ranks and not prepared(manifest):
        run('bash','Allrun.pre')
    require_mesh(ROOT,manifest)
    require_fields(ROOT,root_time,manifest)

    region_args = ('-allRegions',) if manifest.get('fluid_region') else ()
    rank_time = ZERO
    if ranks:
        if len(set(rank_times))!=1:
            raise ValueError('Processor directories have different latest checkpoint times')
        rank_time = rank_times[0]
        if [path.name for path in ranks] != [f'processor{index}' for index in range(len(ranks))]:
            raise ValueError('Processor checkpoint directories must have consecutive indices')
        for path in ranks:
            require_mesh(path,manifest,processor=True)
            require_fields(path,rank_time,manifest)

    if mode=='serial':
        if rank_time[0]>root_time[0]:
            run('reconstructPar',*region_args,'-time',rank_time[1])
            root_time = rank_time
            require_fields(ROOT,root_time,manifest)
        print(f'Starting serial solver from {root_time[1]}',flush=True)
        return

    count = manifest['parallel_subdomains']
    if ranks and len(ranks)!=count:
        raise ValueError(f'Existing processor layout uses {len(ranks)} ranks but configuration requests {count}')
    if not ranks or root_time[0]>rank_time[0]:
        fields_only = ('-fields',) if ranks else ()
        run('decomposePar',*region_args,*fields_only,'-copyUniform','-time',root_time[1])
        rank_time = root_time
        ranks = processor_dirs()
        if len(ranks)!=count:
            raise ValueError(f'Expected {count} processor directories after decomposition')
        for path in ranks:
            require_mesh(path,manifest,processor=True)
            require_fields(path,rank_time,manifest)
    print(f'Starting parallel solver from {rank_time[1]} on {count} ranks',flush=True)


def main():
    try:
        mode, = sys.argv[1:]
        manifest = json.loads((ROOT/'case-manifest.json').read_text(encoding='utf8'))
        if not json.loads((ROOT/'preflight.json').read_text(encoding='utf8'))['ok']:
            raise ValueError('FloTHERM preflight has unresolved blockers')
        if mode=='before-mesh':
            if latest(ROOT)[0]>0 or processor_dirs():
                raise ValueError('Checkpoint exists. Use Allrun.solve or Allrun.parallel to continue without remeshing.')
        elif mode in ('serial','parallel'):
            prepare(manifest,mode)
        else:
            raise ValueError('Expected serial, parallel or before-mesh')
    except (OSError,ValueError,subprocess.CalledProcessError) as error:
        sys.exit(f'FloTHERM restart: {error}')


if __name__=='__main__':
    main()
