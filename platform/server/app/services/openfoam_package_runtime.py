"""Run or clean an exported OpenFOAM case using only its package and native tools."""

import fcntl
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


PACKAGE = 'vibeflow-case.json'
RECEIPT = '.vibeflow-run.json'
SCHEMA = 'vibeflow.openfoam-case-package.v1'
SOURCES = {'blockMesh', 'gmsh', 'importedGmsh', 'importedOpenFOAM', 'importedPolyMesh', 'snappyHexMesh', 'cfMesh'}
REFINEMENT = 'system/vibeflow-refinement'
REFERENCE = 'system/vibeflow_reference_temperature'
PRESSURE_REFERENCE = 'system/vibeflow_reference_pressure'


def ordinary(path, root):
    path.relative_to(root)
    if any(member.is_symlink() for member in (path, *path.parents) if member.is_relative_to(root)):
        raise ValueError(f'Symbolic link in package: {path.relative_to(root)}')
    return path


def file_record(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return {'size':path.stat().st_size, 'sha256':digest.hexdigest()}


def inventory(root):
    files = {}
    for path in root.rglob('*'):
        ordinary(path,root)
        if path.is_file() and path.name != RECEIPT:
            files[path.relative_to(root).as_posix()] = file_record(path)
        elif not path.is_dir() and path.name != RECEIPT:
            raise ValueError(f'Unsupported case file: {path.relative_to(root)}')
    return dict(sorted(files.items()))


def package(root):
    data = json.loads(ordinary(root/PACKAGE,root).read_text(encoding='utf-8'))
    if (not isinstance(data,dict) or data.get('schema')!=SCHEMA or data.get('mesh_source') not in SOURCES
            or ('directional_refinement' in data and (data['directional_refinement'] is not True or data['mesh_source']!='blockMesh'))
            or ('reference_temperature_file' in data and data['reference_temperature_file'] not in
                ('constant/transportProperties','constant/transportProperties.gz'))
            or ('reference_pressure_file' in data and data['reference_pressure_file']!='system/fvSolution')
            or ('rotation_zone' in data and (not isinstance(data['rotation_zone'],str)
                or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*',data['rotation_zone'])))
            or not re.fullmatch('[A-Za-z][A-Za-z0-9_]*Foam',data.get('solver',''))):
        raise ValueError('Invalid OpenFOAM case package')
    return data


def copy_inputs(source, target, *, allow_empty=False):
    folder = ordinary(source,source.parent)
    if not folder.is_dir():
        raise ValueError(f'Missing canonical directory: {source.name}')
    records = inventory(folder)
    if not records and not allow_empty:
        raise ValueError(f'Empty canonical directory: {source.name}')
    target.mkdir(parents=True,exist_ok=True)
    for relative in records:
        item = target/relative
        item.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(folder/relative,item)
    if inventory(target) != records or inventory(folder) != records:
        raise ValueError(f'Canonical inputs changed while copying {source.name}')


def native(run, tool, *arguments, log=None):
    path = shutil.which(tool)
    if path is None:
        raise ValueError(f'{tool} is unavailable. Load the native OpenFOAM environment first.')
    with (log if log is not None else run/f'{tool}.log').open('xb') as output:
        subprocess.run([path,*arguments,'-case',str(run)],cwd=run.parent,stdout=output,
                       stderr=subprocess.STDOUT,env={**os.environ,'PWD':str(run.parent)},check=True)


def check_mesh(run, *arguments, log=None, dimension=None):
    report=log if log is not None else run/'checkMesh.log'
    native(run,'checkMesh','-constant','-allGeometry','-allTopology',*arguments,log=report)
    text=report.read_text(encoding='utf-8',errors='replace')
    if 'Mesh OK.' not in text or re.search(r'Failed\s+[1-9]\d* mesh checks',text):
        raise ValueError('The native mesh did not pass the full checkMesh checks')
    if dimension is not None:
        measured=re.findall(r'^[ \t]*Mesh has ([123]) geometric \(non-empty/wedge\) directions',text,re.M)
        if not measured or any(int(item)!=dimension for item in measured):
            raise ValueError('The native geometry does not match the declared mesh dimension')


def refine_mesh(run):
    check_mesh(run,log=run/'checkMesh-base.log')
    with tempfile.TemporaryDirectory(prefix='mesh-refine-',dir=run) as folder:
        work=Path(folder)
        copy_inputs(run/REFINEMENT,work/'system')
        (work/'constant').mkdir()
        mesh=run/'constant/polyMesh'
        candidate=work/'constant/polyMesh'
        mesh.rename(candidate)
        try:
            selected=run/'topoSet-refine.log'
            native(work,'topoSet',log=selected)
            text=selected.read_text(encoding='utf-8',errors='replace')
            if len(re.findall(r'^\s*cellSet vibeRefinementCells now size [1-9]\d*\s*$',text,re.M))!=1:
                raise ValueError('The directional refinement region does not select any cells')
            native(work,'refineMesh','-overwrite',log=run/'refineMesh.log')
        finally:
            candidate.rename(mesh)


def prepare(run, source, *, refinement=False, dimension=None):
    if source == 'gmsh':
        gmsh = shutil.which('gmsh')
        if gmsh is None:
            raise ValueError('The native gmsh command is unavailable')
        with (run/'gmsh.log').open('xb') as output:
            subprocess.run([gmsh,'system/mesh.geo','-3','-format','msh2','-o','gmsh.msh'],cwd=run,
                           stdout=output,stderr=subprocess.STDOUT,check=True)
        native(run,'gmshToFoam',str(run/'gmsh.msh'))
    elif source == 'importedGmsh':
        native(run,'gmshToFoam',str(run/'system/mesh.msh'))
    elif source == 'importedPolyMesh':
        target = run/'constant/polyMesh'
        target.mkdir()
        copy_inputs(run/'system/polyMesh',target)
    elif source == 'blockMesh':
        native(run,'blockMesh')
        if refinement:
            refine_mesh(run)
    elif source == 'snappyHexMesh':
        native(run,'blockMesh')
        native(run,'snappyHexMesh','-overwrite')
    elif source == 'cfMesh':
        native(run,'cartesianMesh')
    extra = ('-allRegions',) if source=='importedOpenFOAM' else ()
    check_mesh(run,*extra,dimension=dimension)


def verify_linear_convergence(log, declarations):
    from vibeflow_openfoam_residuals import has_nonfinite_telemetry, residual_evidence, residual_samples
    text=log.read_text(encoding='utf-8',errors='replace')
    if has_nonfinite_telemetry(text):
        raise ValueError('Native solver reported non-finite residuals or times')
    detail=defaultdict(list)
    for field,point in residual_samples(text):
        detail[field].append(point)
    evidence=residual_evidence(declarations,detail)
    failed=[field for field,record in evidence.items() if record['status']!='converged']
    if failed:
        raise ValueError('Native linear solve did not meet declared tolerances: '+', '.join(failed))


def publish(run, final):
    for delay in (0, .05, .1, .25, .5, 1.):
        if delay:
            time.sleep(delay)
        try:
            run.rename(final)
            return
        except PermissionError as error:
            cause=error
    raise ValueError(f'Could not move completed work. The run is preserved at {run}') from cause


def run_case(root, runs, data, solve):
    identifier=str(uuid.uuid4())
    final=runs/identifier
    device=runs.stat().st_dev
    # The OpenFOAM runtime rejects some characters in the resolved case path.
    # Work on the same mount so even a package with spaces can publish by rename.
    parent=next((path for path in (runs,*runs.parents)
                 if re.fullmatch(r'[-A-Za-z0-9_./]+',str(path)) and path.stat().st_dev==device
                 and os.access(path,os.W_OK)),None)
    if parent is None:
        raise ValueError('No writable OpenFOAM-compatible workspace on the package filesystem')
    print(final,flush=True)
    stage=Path(tempfile.mkdtemp(prefix='.vibeflow-openfoam-',dir=parent))
    try:
        run=stage/identifier
        run.mkdir()
        status='failed'
        try:
            copy_inputs(root/'system',run/'system')
            copy_inputs(root/'constant',run/'constant',allow_empty=True)
            reference=data.get('reference_temperature_file')
            if reference:
                (run/reference).write_bytes((run/REFERENCE).read_bytes())
            pressure=data.get('reference_pressure_file')
            if pressure:
                (run/pressure).write_bytes((run/PRESSURE_REFERENCE).read_bytes())
            initial = root/'0' if (root/'0').exists() else root/'0.orig'
            copy_inputs(initial,run/'0')
            control=(run/'system/controlDict').read_text(encoding='utf-8')
            application=re.findall(r'^\s*application\s+([A-Za-z][A-Za-z0-9_]*Foam)\s*;',control,re.M)
            if application!=[data['solver']]:
                raise ValueError('The native application differs from the packaged solver')
            prepare(run,data['mesh_source'],refinement=data.get('directional_refinement') is True,
                    dimension=data.get('mesh_dimension'))
            zone = data.get('rotation_zone')
            if zone:
                checked = (run/'checkMesh.log').read_text(encoding='utf-8',errors='replace')
                if not re.search(r'^[ \t]*'+re.escape(zone)+r'[ \t]+[1-9]\d*[ \t]+\d+[ \t]+\S+[ \t]+\(',checked,re.M):
                    raise ValueError('The checked mesh has no cells in its declared MRF zone: '+zone)
            if solve:
                native(run,data['solver'])
                if zone:
                    log = (run/(data['solver']+'.log')).read_text(encoding='utf-8',errors='replace')
                    zones = re.findall(r'^[ \t]*creating MRF zone:[ \t]+(\S+)[ \t]*$',log,re.M | re.I)
                    if zones != [zone]:
                        raise ValueError('The native solver did not load its declared MRF zone: '+zone)
                if data.get('linear_convergence'):
                    verify_linear_convergence(run/(data['solver']+'.log'),data['linear_convergence'])
            status = 'solved' if solve else 'prepared'
        finally:
            try:
                snapshot={'schema':SCHEMA,'id':identifier,'status':status,'files':inventory(run)}
                with (run/RECEIPT).open('x',encoding='utf-8') as output:
                    json.dump(snapshot,output,sort_keys=True)
            finally:
                publish(run,final)
    finally:
        if not any(stage.iterdir()):
            stage.rmdir()


def relative_file(value):
    candidate=PurePosixPath(value)
    if not candidate.parts or candidate.is_absolute() or '..' in candidate.parts or '\\' in value:
        raise ValueError('Invalid owned run file path')
    return candidate


def clean(runs):
    preserved=[]
    for run in sorted(runs.iterdir()):
        if not re.fullmatch('[0-9a-f-]{36}',run.name) or run.is_symlink() or not run.is_dir():
            continue
        receipt=ordinary(run/RECEIPT,runs)
        if not receipt.is_file():
            continue
        state=json.loads(receipt.read_text(encoding='utf-8'))
        if state.get('schema')!=SCHEMA or state.get('id')!=run.name or not isinstance(state.get('files'),dict):
            raise ValueError('Invalid owned run receipt')
        for value,record in state['files'].items():
            relative=relative_file(value)
            try:
                path=ordinary(run/relative,run)
            except ValueError:
                continue
            if path==receipt:
                raise ValueError('An owned file cannot be its own receipt')
            if path.is_file() and file_record(path)==record:
                path.unlink()
        for path in sorted(run.rglob('*'),key=lambda item:len(item.parts),reverse=True):
            if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()
        if any(path!=receipt for path in run.iterdir()):
            preserved.append(run.name)
        else:
            receipt.unlink()
            run.rmdir()
    if preserved:
        print('Preserved changed or untracked files in runs: '+' '.join(preserved))


def main():
    if len(sys.argv)!=2 or sys.argv[1] not in {'pre','run','clean'}:
        raise ValueError('Use Allrun.pre, Allrun or Allclean')
    root=Path(__file__).resolve().parents[1]
    data=package(root)
    runs=ordinary(root/'runs',root)
    runs.mkdir(exist_ok=True)
    with ordinary(root/'.vibeflow-run.lock',root).open('a',encoding='utf-8') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if sys.argv[1]=='clean':
            clean(runs)
        else:
            run_case(root,runs,data,sys.argv[1]=='run')


if __name__=='__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
