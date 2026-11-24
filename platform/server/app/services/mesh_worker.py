"""Generate or import an OpenFOAM mesh and check it in a sealed attempt."""
import argparse
from contextlib import nullcontext
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services import mesh_refinement, openfoam_cht
from app.services.casedict import parse_casedict
from app.services.case_mesh_schema import directional_refinement
from app.services.case_evidence import file_record, run_inventory, safe_run_dir
from app.services.mesh_artifacts import (ARTIFACTS, BOUNDARIES, IMPORTED_GMSH, IMPORTED_POLY_MESH, MESH_FILES, NATIVE_INPUT, NATIVE_LOGS, NATIVE_TOOLS,
    POLY_MESH_INPUT,
    QUALITY, TOOLCHAIN, artifact_index, boundaries, gmsh_format, imported_boundaries, input_snapshot,
    mesh_source, quality, quality_targets, regions_log)
from app.services.mesh_jacobian import VTK_DIRECTORY, VTK_EVIDENCE
from app.services.mesh_quality import evaluate_mesh_quality


def execute(tool, run, log, budget, *, trace=None,arguments=None):
    command = [tool, *(arguments if arguments is not None else ['-case', str(run)])]
    if Path(tool).name == 'checkMesh':
        command += ['-constant', '-allGeometry', '-allTopology']
    with log.open('xb') as output:
        actual=trace.command(command,'solve') if trace else command
        proc = subprocess.Popen(actual, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            with trace.monitor(proc) if trace else nullcontext():
                while chunk := proc.stdout.read1(65536):
                    budget[0] -= len(chunk)
                    if budget[0] < 0:
                        raise ValueError('OpenFOAM tool log budget exceeded')
                    output.write(chunk)
                    output.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                if proc.wait() != 0:
                    raise ValueError(Path(tool).name+' exited unsuccessfully')
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()


def normalize_mesh(mesh,run):
    """Normalize only freshly generated core mesh outputs, never input files."""
    if mesh.is_symlink() or not mesh.resolve().is_relative_to(run.resolve()):
        raise ValueError('Generated mesh escaped its attempt run directory')
    decoded,total=[],0
    for name in MESH_FILES:
        compressed=mesh/(name+'.gz')
        if not compressed.exists():
            continue
        if compressed.is_symlink() or not compressed.is_file() or compressed.stat().st_nlink!=1:
            raise ValueError('Generated compressed mesh is not an ordinary file')
        # Exclusive creation rejects ambiguous plain+gzip outputs. The caller
        # already proved this attempt did not contain an old polyMesh directory.
        with gzip.open(compressed,'rb') as source, (mesh/name).open('xb') as target:
            while chunk:=source.read(1024*1024):
                total+=len(chunk)
                if total>1024**3:
                    raise ValueError('Expanded mesh exceeds the 1 GiB input limit')
                target.write(chunk)
        compressed.unlink()
        decoded.append(name)
    return decoded


def export_volume_mesh(tool, run, budget):
    directory=run/VTK_DIRECTORY
    execute(tool,run,run/'foamToVTK.log',budget,arguments=[
        '-case',str(run),'-ascii','-constant','-time','none','-name',VTK_DIRECTORY,
        '-no-fields','-no-boundary','-no-finite-area','-no-lagrangian','-no-libs','-noFunctionObjects'])
    volumes=list(directory.rglob('internal.vtu'))
    if len(volumes)!=1 or not volumes[0].resolve().is_relative_to(directory.resolve()):
        raise ValueError('foamToVTK did not export one native volume mesh')
    volumes[0].rename(run/VTK_EVIDENCE)
    shutil.rmtree(directory)


def refine_mesh(run, mesh, tools, budget, parameters):
    execute(tools['checkMesh'],run,run/mesh_refinement.BASE,budget)
    if 'Mesh OK.' not in (run/mesh_refinement.BASE).read_text(encoding='utf-8',errors='replace'):
        raise ValueError('方向加密需要通过完整检查的基准网格')
    with tempfile.TemporaryDirectory(prefix='mesh-refine-',dir=run) as work:
        staging=Path(work)
        (staging/'system').mkdir()
        (staging/'constant').mkdir()
        for name,body in mesh_refinement.dictionaries(parameters).items():
            (staging/'system'/name).write_text(body,encoding='ascii')
        candidate=staging/'constant/polyMesh'
        mesh.rename(candidate)
        try:
            selector=run/mesh_refinement.SELECT
            execute(tools['topoSet'],staging,selector,budget)
            mesh_refinement.selected_count(selector.read_text(encoding='utf-8',errors='replace'))
            execute(tools['refineMesh'],staging,run/mesh_refinement.REFINE,budget,
                    arguments=['-overwrite','-case',str(staging)])
        finally:
            candidate.rename(mesh)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',required=True)
    parser.add_argument('--execution-id',required=True)
    parser.add_argument('--distro',required=True)
    parser.add_argument('--max-log-bytes',type=int,required=True)
    args=parser.parse_args()
    assert str(uuid.UUID(args.execution_id))==args.execution_id
    from app.services.openfoam_runtime import verify_worker
    verify_worker(args.distro)
    root=Path.cwd().resolve()
    if root.name!='run' or not (root.parent/'manifest.json').is_file():
        raise ValueError('Mesh worker requires a sealed attempt workspace')
    run_dir=safe_run_dir(args.run_dir)
    run=root/run_dir
    mesh=run/'constant/polyMesh'
    source=mesh_source(root)
    case=parse_casedict((root/'0-caseDict/caseDict').read_text(encoding='utf-8'))
    declaration=case['mesh'].get('refinement')
    refinement=directional_refinement(declaration) if declaration is not None else None
    if refinement is not None and source!='blockMesh':
        raise ValueError('方向加密需要 blockMesh 原生网格')
    regions=openfoam_cht.inspect_inputs(root,run_dir) if source==openfoam_cht.MESH_SOURCE else {}
    # Old meshes/logs are deliberately not selected as inputs. Never delete a
    # live mesh or reuse a copied output if the source selection regresses.
    known_logs={name for names in NATIVE_LOGS.values() for name in names}
    if (not regions and mesh.exists()) or any((run/name).exists() for name in (
            'checkMesh.log','foamToVTK.log',VTK_DIRECTORY,VTK_EVIDENCE,*known_logs,*mesh_refinement.LOGS)):
        raise ValueError('Mesh workspace contains old generated outputs')
    before=input_snapshot(root,run_dir)
    names=NATIVE_TOOLS[source]
    with_jacobian=not regions and 'minJacobian' in quality_targets(root)
    if with_jacobian:
        names+=('foamToVTK',)
    if refinement is not None:
        names+=('topoSet','refineMesh')
    tools={name:shutil.which(name) for name in names}
    if not all(tools.values()) or not os.environ.get('WM_PROJECT_VERSION'):
        raise ValueError('OpenFOAM tools/environment unavailable')
    fingerprints={name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in tools.items()}
    budget=[args.max_log_bytes]
    if source in ('blockMesh','snappyHexMesh'):
        execute(tools['blockMesh'],run,run/'blockMesh.log',budget)
    if source=='snappyHexMesh':
        execute(tools[source],run,run/'snappyHexMesh.log',budget,arguments=['-overwrite','-case',str(run)])
    elif source=='cfMesh':
        execute(tools['cartesianMesh'],run,run/'cartesianMesh.log',budget)
    elif source==IMPORTED_POLY_MESH:
        shutil.copytree(run/POLY_MESH_INPUT,mesh)
    elif source in ('gmsh',IMPORTED_GMSH):
        if source==IMPORTED_GMSH:
            original=run/NATIVE_INPUT[source]
            gmsh_format(original)
            with original.open('rb') as input_stream, (run/'gmsh.msh').open('xb') as retained:
                shutil.copyfileobj(input_stream,retained)
        else:
            execute(tools['gmsh'],run,run/'gmsh.log',budget,arguments=[str(run/'system/mesh.geo'),'-3','-format','msh2','-o',str(run/'gmsh.msh')])
        execute(tools['gmshToFoam'],run,run/'gmshToFoam.log',budget,arguments=[str(run/'gmsh.msh'),'-case',str(run)])
    normalized=[]
    if regions:
        execute(tools['checkMesh'],run,root/regions_log(run_dir),budget,
                arguments=['-case',str(run),'-allRegions'])
    else:
        normalized=normalize_mesh(mesh,run)
        if refinement is not None:
            refine_mesh(run,mesh,tools,budget,refinement)
        execute(tools['checkMesh'],run,run/'checkMesh.log',budget)
        from app.services import openfoam_overset
        if openfoam_overset.declared(case.get('interfaces')):
            openfoam_overset.retain_mesh_regions(run,run_dir,before['files'])
    if with_jacobian and evaluate_mesh_quality((run/'checkMesh.log').read_text(encoding='utf-8',errors='replace'),
            {},registered=True)['mesh_ok']:
        export_volume_mesh(tools['foamToVTK'],run,budget)
    if input_snapshot(root,run_dir)!=before:
        raise ValueError('Mesh tools modified their input files')
    if not regions and any(not (mesh/name).is_file() for name in MESH_FILES):
        raise ValueError('OpenFOAM mesh files are missing')
    report=quality(root,run_dir)
    report['inputs']=[file_record(root,path) for path in run_inventory(root,run_dir,results=False)]
    report['execution_id']=args.execution_id
    patches=imported_boundaries(root,run_dir,regions) if regions else boundaries(mesh/'boundary')
    provenance={'schema':'vibeflow.mesh-toolchain.v1','execution_id':args.execution_id,
        'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'openfoam':os.environ['WM_PROJECT_VERSION'],'distro':args.distro,
        'executables':tools,'executable_sha256':fingerprints,'python':sys.version.split()[0],
        'normalized_gzip_mesh_files':normalized,'mesh_source':source}
    for path,document in ((QUALITY,report),(BOUNDARIES,{'schema':'vibeflow.mesh-boundaries.v1',
            'run_dir':run_dir,'execution_id':args.execution_id,'patches':patches}),(TOOLCHAIN,provenance)):
        target=root/path
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('x',encoding='utf-8') as output:
            json.dump(document,output,ensure_ascii=False,sort_keys=True)
    if report['gate']['status']=='blocked':
        raise ValueError(report['gate']['reason'])
    index = artifact_index(root,run_dir,source,args.execution_id,regions,report['log_path'])
    with (root/ARTIFACTS).open('x',encoding='utf-8') as output:
        json.dump(index,output,ensure_ascii=False,sort_keys=True)
    print('MESH_WORKER_OK '+args.execution_id,flush=True)


if __name__=='__main__':
    main()
