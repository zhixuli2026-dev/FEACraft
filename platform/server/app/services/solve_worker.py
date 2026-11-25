"""Execute a serial OpenFOAM solver in a sealed fresh attempt."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import mesh_worker, openfoam_source_runner
from app.services.case_evidence import safe_run_dir
from app.services.solve_artifacts import EVIDENCE, TOOLCHAIN, LOG, INVOCATION, initial_snapshot, output_times, result


def write_receipt(path,document):
    with path.open('x',encoding='utf-8') as stream:
        json.dump(document,stream,sort_keys=True,ensure_ascii=False,allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def native_toolchain(args):
    source_build = None
    if args.source_library_dir:
        tool,source_build=openfoam_source_runner.build_record(args.source_library_dir,args.solver)
    else:
        tool=shutil.which(args.solver);appbin=os.environ.get('FOAM_APPBIN')
        if not tool or not appbin or not Path(tool).resolve().is_relative_to(Path(appbin).resolve()):
            raise ValueError('Solver must come from the configured OpenFOAM application directory')
    toolchain={'schema':'vibeflow.solve-toolchain.v1','execution_id':args.execution_id,
        'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'runner_sha256':hashlib.sha256(Path(mesh_worker.__file__).read_bytes()).hexdigest(),
        'solver':args.solver,'executable':tool,
        'executable_sha256':source_build['library_sha256'] if source_build else hashlib.sha256(Path(tool).read_bytes()).hexdigest(),
        'openfoam':os.environ.get('WM_PROJECT_VERSION'),'distro':args.distro,'python':sys.version.split()[0]}
    if source_build:
        toolchain['source_build']=source_build
    if not toolchain['openfoam']:raise ValueError('OpenFOAM environment is missing')
    return tool,toolchain


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',required=True)
    parser.add_argument('--execution-id',required=True)
    parser.add_argument('--distro',required=True)
    parser.add_argument('--max-log-bytes',type=int,required=True)
    parser.add_argument('--solver',required=True)
    parser.add_argument('--source-library-dir')
    parser.add_argument('--resume-checkpoint')
    parser.add_argument('--prepared-source')
    parser.add_argument('--retire-prepared-results',action='store_true')
    args=parser.parse_args()
    assert str(uuid.UUID(args.execution_id))==args.execution_id
    root=Path.cwd().resolve()
    if root.name!='run' or not (root.parent/'manifest.json').is_file():
        raise ValueError('Solve worker requires a sealed attempt')
    from app.services.openfoam_runtime import verify_worker
    verify_worker(args.distro)
    run_dir=safe_run_dir(args.run_dir)
    run=root/run_dir
    prepared=None;trace=None;query_helper=None
    if args.prepared_source:
        if args.resume_checkpoint or args.prepared_source!='.vibeflow/prepared/serial.json':
            raise ValueError('Prepared serial source differs from its fixed contract')
        path=root/args.prepared_source;prepared=json.loads(path.read_text(encoding='utf8'))
        if (prepared['schema']!='vibeflow.prepared-serial.v1' or prepared['run_dir']!=run_dir
                or prepared['execution_id']!=args.execution_id or prepared['distro']!=args.distro
                or prepared['configuration']['solver']!=args.solver or prepared['max_log_bytes']!=args.max_log_bytes
                or prepared.get('source_library_dir')!=args.source_library_dir):
            raise ValueError('Prepared serial settings differ from invocation')
        from app.services import case_file_queries as query_helper
        helper_sha=hashlib.sha256(Path(query_helper.__file__).read_bytes()).hexdigest()
        if prepared['case_queries']!={'schema':query_helper.SCHEMA,'helper_sha256':helper_sha}:
            raise ValueError('Prepared serial query helper changed')
        if args.retire_prepared_results:
            from app.services.solve_artifacts import managed
            directories=set()
            for name,record in prepared['retired_files'].items():
                target=root/name
                if (not managed(name,run_dir) or target.is_symlink() or not target.resolve().is_relative_to(run)
                        or query_helper.digest(target)!={k:record[k] for k in ['size','sha256']}):
                    raise ValueError('Prepared serial retirement differs from sealed output')
                target.unlink();directories.update(target.parents)
            for directory in sorted(directories,key=lambda p:len(p.parts),reverse=True):
                if directory!=run and directory.is_relative_to(run) and directory.is_dir() and not any(directory.iterdir()):directory.rmdir()
            print('SERIAL_RETIRED '+str(len(prepared['retired_files'])),flush=True)
            return
        from app.services.mpi_native import inventory
        observed=inventory(root)
        if {p:{k:r[k] for k in ['size','sha256']} for p,r in observed.items()}!={p:{k:r[k] for k in ['size','sha256']} for p,r in prepared['source']['files'].items()}:
            raise ValueError('Prepared serial complete sealed source changed')
        trace=query_helper.Trace(root)
        if prepared['retired_files']:
            child=subprocess.Popen(trace.command([sys.executable,'-I','-S',str(Path(__file__).resolve()),*sys.argv[1:],
                '--retire-prepared-results'],'retire'),cwd=root)
            try:
                with trace.monitor(child):
                    if child.wait()!=0:raise ValueError('Prepared serial private result retirement failed')
            finally:
                if child.poll() is None:child.kill()
                child.wait()
    elif args.retire_prepared_results:raise ValueError('Serial retirement requires its bound preparation')
    if any((run/name).exists() for name in (EVIDENCE,TOOLCHAIN,LOG,INVOCATION)):
        raise ValueError('Fresh solver workspace contains old results')
    resume=None
    if args.resume_checkpoint:
        from app.services.solve_resume_artifacts import verify_frozen
        resume=verify_frozen(root,run_dir,args.resume_checkpoint)
    elif output_times(root,run_dir):raise ValueError('Fresh solver workspace contains old results')
    snapshot=initial_snapshot(root,run_dir,args.execution_id,resume_time=resume['time'] if resume else None)
    if args.solver!=snapshot['configuration']['solver']:
        raise ValueError('Solver differs from the prepared application')
    tool,toolchain=native_toolchain(args)
    if trace:toolchain['case_queries']={'helper_sha256':prepared['case_queries']['helper_sha256'],'strace':trace.tool}
    if resume:
        from app.services.solve_resume_artifacts import require_toolchain
        require_toolchain(toolchain,resume['toolchain'])
    # A prepared invocation is not a success receipt. It lets a later explicit
    # continuation authenticate partial native fields after failure/cancellation.
    write_receipt(run/INVOCATION,{'schema':'vibeflow.solve-invocation.v1','state':'prepared',
        'snapshot':snapshot,'toolchain':toolchain,'resume':resume})
    (run/(run.name+'.foam')).touch(exist_ok=False)
    arguments = ['-case', str(run)]
    if 'convection_diffusivity' in snapshot['configuration']:
        arguments += ['-lib', 'libthermoTools.so']
    if args.source_library_dir:
        tool,*arguments = openfoam_source_runner.command(tool,args.solver,arguments)
    mesh_worker.execute(tool,run,run/LOG,[args.max_log_bytes],trace=trace,arguments=arguments)
    if trace:
        queried=query_helper.collect(root,root,prepared['source'],lambda p,reverse=False:p)
        proof=root/'.vibeflow/serial';proof.mkdir()
        write_receipt(proof/'queries.json',{'schema':'vibeflow.serial-queries.v1','execution_id':args.execution_id,
            'prepared_sha256':query_helper.digest(root/args.prepared_source)['sha256'],'case_queries':queried})
    evidence=result(root,snapshot)
    if resume:
        from app.services.solve_resume_artifacts import verify_checkpoint_files
        verify_checkpoint_files(root,resume);evidence['resume']=resume
    for name,document in ((EVIDENCE,evidence),(TOOLCHAIN,toolchain)):
        write_receipt(run/name,document)
    print('SOLVE_WORKER_OK '+args.execution_id,flush=True)


if __name__=='__main__':
    main()
