"""Stdlib-only owned Linux/WSL OpenFOAM MPI pipeline, with private staging."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import uuid


def digest(path):
    value=hashlib.sha256();size=0
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):size+=len(data);value.update(data)
    return {'size':size,'sha256':value.hexdigest()}


def safe(root,name):
    relative=PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:raise ValueError('Invalid MPI relative path')
    path=root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('MPI path escaped private root')
    return path


def inventory(root):
    result={};size=0;boundary=root.resolve()
    def visit(directory,ancestors):
        nonlocal size
        resolved=directory.resolve(strict=True)
        if not resolved.is_relative_to(boundary) or resolved in ancestors:
            raise ValueError('MPI staging link escapes or cycles')
        for path in sorted(directory.iterdir()):
            if directory==root and path.name=='.vibeflow':continue
            target=path.resolve(strict=True)
            if not target.is_relative_to(boundary):raise ValueError('MPI staging link escapes private root')
            if path.is_dir():visit(path,ancestors|{resolved})
            elif path.is_file():
                record=digest(path);size+=record['size']
                result[path.relative_to(root).as_posix()]={**record,'mtime':path.stat().st_mtime_ns}
                if len(result)>10000 or size>1024**3:raise ValueError('MPI staging output budget exceeded')
            else:raise ValueError('MPI staging contains a non-regular file')
    # OpenFOAM links processor time/uniform to the case's own uniform data on
    # restart. Traverse only bounded internal links; copyback materializes bytes.
    visit(root,set())
    return result


def path_mapping(run_dir,identifier):
    """Keep relative dependency depth while making OpenFOAM's case path portable."""
    original=[];mapped=[];pairs=[]
    for index,part in enumerate(PurePosixPath(run_dir).parts):
        original.append(part)
        mapped.append(part if re.fullmatch(r'[A-Za-z0-9_.+-]+',part) else 'vf-mpi-'+identifier+'-'+str(index))
        pairs.append(('/'.join(original),'/'.join(mapped)))
    def translate(name,reverse=False):
        for left,right in reversed(pairs):
            if reverse:left,right=right,left
            if name==left or name.startswith(left+'/'):return right+name[len(left):]
        return name
    return translate


def invoke(argv,cwd,log,budget,*,environment=None,trace=None,label=None):
    print('MPI_STEP '+Path(argv[0]).name,flush=True)
    with log.open('wb') as stream:
        actual=trace.command(argv,label) if trace else argv
        child=subprocess.Popen(actual,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            env={**os.environ,**environment} if environment is not None else None)
        monitoring=trace.monitor(child) if trace else __import__('contextlib').nullcontext()
        try:
            with monitoring:
                while data:=child.stdout.read1(65536):
                    budget[0]-=len(data)
                    if budget[0]<0:raise ValueError('MPI pipeline log budget exceeded')
                    stream.write(data);stream.flush();sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
                code=child.wait()
                if code:raise ValueError('MPI tool failed: '+Path(argv[0]).name+' exit '+str(code))
        finally:
            if child.poll() is None:child.kill()
            child.wait();child.stdout.close()
    receipt={'argv':argv,'returncode':code,'log':digest(log)}
    if environment is not None:receipt['environment']=environment
    return receipt


def verify_distribution(distro):
    if distro=='native-linux':
        if not sys.platform.startswith('linux'):raise ValueError('Native Linux OpenFOAM is unavailable')
    elif os.environ.get('WSL_DISTRO_NAME')!=distro:raise ValueError('OpenFOAM distribution changed')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--sha256',required=True)
    args=parser.parse_args();config_path=Path(args.config).resolve()
    if digest(config_path)['sha256']!=args.sha256:raise ValueError('MPI settings changed before native execution')
    config=json.loads(config_path.read_text(encoding='utf8'));identifier=config['execution_id']
    query_helper=None
    if config.get('case_queries'):
        helper=Path(__file__).with_name('case_file_queries.py')
        if (config['case_queries'].get('schema')!='vibeflow.case-file-queries.v1'
                or digest(helper)['sha256']!=config['case_queries']['helper_sha256']):raise ValueError('MPI case query helper changed')
        spec=importlib.util.spec_from_file_location('case_file_queries',helper)
        query_helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(query_helper)
    if str(uuid.UUID(identifier))!=identifier:raise ValueError('Invalid MPI invocation')
    root=config_path.parents[2]
    if config_path.relative_to(root).as_posix()!='.vibeflow/prepared/mpi.json':raise ValueError('MPI settings outside private metadata')
    verify_distribution(config['distro'])
    stage=Path('/tmp')/('vibeflow-mpi-'+identifier)
    stage.mkdir(mode=0o700,exist_ok=False)
    proof=root/'.vibeflow/mpi';proof.mkdir(parents=True,exist_ok=True)
    ownership={'schema':'vibeflow.mpi-stage.v1','execution_id':identifier,'path':str(stage),'uid':os.getuid(),
        'device':stage.stat().st_dev,'inode':stage.stat().st_ino,'removed':False}
    (proof/'stage.json').write_text(json.dumps(ownership),encoding='utf8')
    def interrupted(*_):raise InterruptedError('MPI native operation interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        translate=path_mapping(config['run_dir'],identifier)
        entries=set(config['source']['directories'])|set(config['source']['files'])
        if len({translate(p) for p in entries})!=len(entries):raise ValueError('MPI staging path mapping collides')
        for directory in config['source']['directories']:safe(stage,translate(directory)).mkdir(parents=True,exist_ok=True)
        for name,expected in config['source']['files'].items():
            source=safe(root,name);target=safe(stage,translate(name));target.parent.mkdir(parents=True,exist_ok=True)
            if digest(source)!={k:expected[k] for k in ('size','sha256')}:raise ValueError('MPI input changed: '+name)
            shutil.copyfile(source,target);target.chmod(0o755 if expected['executable'] else 0o644)
        run=safe(stage,translate(config['run_dir']))
        decompose=run/'system/decomposeParDict'
        if config['decompose_text'] is not None:decompose.write_text(config['decompose_text'],encoding='utf8')
        if 'solution_text' in config:
            with (run/'system/fvSolution').open('x',encoding='utf-8') as dictionary:
                dictionary.write(config['solution_text'])
        solver=config['solver']
        source_directory=config.get('source_library_dir')
        names=('foamDictionary','decomposePar','mpirun','reconstructPar')
        if not source_directory:names=(*names,solver)
        tools={name:shutil.which(name) for name in names}
        appbin=os.environ.get('FOAM_APPBIN')
        if not appbin or not os.environ.get('WM_PROJECT_VERSION') or not all(tools.values()):raise ValueError('MPI/OpenFOAM toolchain unavailable')
        if any(not Path(path).resolve().is_relative_to(Path(appbin).resolve()) for name,path in tools.items() if name!='mpirun'):
            raise ValueError('MPI solver/tool is outside the configured OpenFOAM installation')
        source_build=None
        rank_arguments=['-parallel','-case',str(run)]
        if source_directory:
            loader=Path(__file__).with_name('openfoam_source_runner.py')
            if digest(loader)['sha256']!=config['source_loader_sha256']:
                raise ValueError('MPI OpenFOAM source loader changed')
            spec=importlib.util.spec_from_file_location('openfoam_source_runner',loader)
            runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
            tools[solver],source_build=runner.build_record(source_directory,solver)
            rank_command=runner.command(tools[solver],solver,rank_arguments)
        else:
            rank_command=[tools[solver],*rank_arguments]
        fingerprints={name:{'path':path,**digest(Path(path))} for name,path in tools.items()}
        trace=query_helper.Trace(stage) if query_helper else None
        if trace:fingerprints['strace']=trace.tool
        selection=[tools['foamDictionary'],str(decompose),'-entry','numberOfSubdomains','-value']
        child=subprocess.Popen(trace.command(selection,'dictionary') if trace else selection,cwd=run,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            with trace.monitor(child) if trace else __import__('contextlib').nullcontext():
                selected,error=child.communicate(timeout=30)
            if child.returncode or len(selected)>4096:raise ValueError('MPI dictionary selection failed')
            selected=selected.decode('utf8').strip()
        finally:
            if child.poll() is None:child.kill()
            child.wait();child.stdout.close();child.stderr.close()
        if selected!=str(config['subdomains']):raise ValueError('decomposeParDict numberOfSubdomains differs from request')
        # Regenerate only private processor trees. Existing serial times remain
        # available for native startFrom/restart controls and reconstruction.
        runtime=stage/'.vibeflow'
        session=runtime/'sessions';shared=runtime/'shared'
        session.mkdir(parents=True);shared.mkdir()
        before=inventory(stage);budget=[config['max_log_bytes']]
        steps={}
        flags = ['-force'] if config.get('force', True) else []
        steps['decompose']=invoke([tools['decomposePar'],*flags,'-case',str(run)],run,run/'mpi-decompose.log',budget,trace=trace,label='decompose')
        ranks=sorted(p.name for p in run.iterdir() if p.is_dir() and re.fullmatch(r'processor\d+',p.name))
        if set(ranks)!={'processor'+str(i) for i in range(config['subdomains'])}:raise ValueError('MPI decomposition has an unexpected rank set')
        # Absolute mpirun otherwise enables implicit --prefix, changing the
        # ranks' library lookup order away from the selected OpenFOAM release.
        # Different PID namespaces can reuse mpirun PIDs/job identifiers. Keep
        # OpenMPI's session and shared-memory files inside this invocation too.
        mpi_options=['--noprefix','--mca','orte_tmpdir_base',str(session),
            '--mca','btl_vader_backing_directory',str(shared)]
        # PMIx's shared-memory GDS can fault in this namespaced runtime.
        # Select its process-local hash backend for this invocation only;
        # transport, authentication and rank isolation remain unchanged.
        if config.get('pmix_gds')!='hash':raise ValueError('MPI PMIx backend is not pinned')
        mpi_environment={'PMIX_MCA_gds':config['pmix_gds']}
        # Docker retains its configured UID inside the existing owned scope.
        # This explicit per-invocation opt-in never changes WSL's nobody path.
        if config['distro']=='native-linux' and os.getuid()==0:
            mpi_environment.update(OMPI_ALLOW_RUN_AS_ROOT='1',OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
        steps['solve']=invoke([tools['mpirun'],*mpi_options,'-np',str(config['subdomains']),*rank_command],run,run/'mpi-solve.log',budget,
            environment=mpi_environment,trace=trace,label='solve')
        steps['reconstruct']=invoke([tools['reconstructPar'],'-case',str(run)],run,run/'mpi-reconstruct.log',budget,trace=trace,label='reconstruct')
        # These three logs are opened by this parent, outside the traced native
        # children. On an explicit replacement they can be present in the full
        # original seal and observed by native directory reads. Bind their exact
        # completed digests; keep all original dependencies and other checks.
        derived={config['run_dir']+'/mpi-'+name+'.log':steps[name]['log']
            for name in ('decompose','solve','reconstruct')}
        queried=query_helper.collect(stage,run,config['source'],translate,
            forced_paths=(config['run_dir']+'/system/decomposeParDict',),
            derived_inputs={p:r for p,r in derived.items() if p in config['source']['files']}) if query_helper else None
        after=inventory(stage)
        changed={p:r for p,r in after.items() if before.get(p)!=r}
        # A newly generated dictionary is a prepared output even though it was
        # present before the tool pipeline's change inventory was captured.
        if config['decompose_text'] is not None:
            name=translate(config['run_dir'])+'/system/decomposeParDict';changed[name]=after[name]
        if 'solution_text' in config:
            name=translate(config['run_dir'])+'/system/fvSolution';changed[name]=after[name]
        for name in changed:
            target=safe(root,translate(name,reverse=True));target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(safe(stage,name),target)
        deleted=sorted(set(before)-set(after))
        for name in deleted:safe(root,translate(name,reverse=True)).unlink()
        receipt={'schema':'vibeflow.mpi-native.v1','execution_id':identifier,'distro':config['distro'],
            'openfoam':os.environ['WM_PROJECT_VERSION'],'uid':os.getuid(),'run_dir':config['run_dir'],
            'solver':config['solver'],'subdomains':config['subdomains'],'processors':ranks,'tools':fingerprints,
            'steps':steps,'changed':{translate(p,reverse=True):{k:r[k] for k in ('size','sha256')} for p,r in changed.items()},
            'deleted':sorted(translate(p,reverse=True) for p in deleted),
            'helper_sha256':digest(Path(__file__))['sha256']}
        if queried:receipt['case_queries']=queried
        if source_build:receipt['source_build']=source_build
        with (proof/'native.json').open('x',encoding='utf8') as stream:json.dump(receipt,stream,sort_keys=True)
    finally:
        # The deterministic path and inode receipt remain available if SIGKILL
        # prevents this cleanup. Never infer cleanup merely from API death.
        if stage.exists() and stage.parent==Path('/tmp') and stage.name=='vibeflow-mpi-'+identifier:
            shutil.rmtree(stage)
        ownership['removed']=not stage.exists()
        (proof/'stage.json').write_text(json.dumps(ownership),encoding='utf8')


if __name__=='__main__':main()
