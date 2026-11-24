"""Owned UDF compilation/loading/integration in a private Linux copy."""
import argparse
from contextlib import nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from mpi_native import digest,safe,inventory,path_mapping,verify_distribution


def invoke(argv,cwd,log,budget,*,trace_directory=None,trace=None,label=None):
    print('UDF_STEP '+Path(argv[0]).name,flush=True)
    with log.open('wb') as stream:
        actual=trace.command(argv,label) if trace else argv
        child=subprocess.Popen(actual,cwd=cwd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        finished=threading.Event();over_limit=threading.Event();monitor=None
        if trace_directory is not None:
            def observe():
                while not finished.wait(.05):
                    files=list(trace_directory.glob('compiler-query.*'))
                    try:exceeded=len(files)>1024 or sum(p.stat().st_size for p in files)>67108864
                    except OSError:exceeded=True
                    if exceeded:
                        over_limit.set()
                        try:child.kill()
                        except ProcessLookupError:pass
                        return
            monitor=threading.Thread(target=observe,name='udf-trace-budget');monitor.start()
        try:
            with trace.monitor(child) if trace else nullcontext():
                while data:=child.stdout.read1(65536):
                    budget[0]-=len(data)
                    if budget[0]<0:raise ValueError('UDF pipeline log budget exceeded')
                    stream.write(data);stream.flush();sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
                code=child.wait()
                if over_limit.is_set():raise ValueError('UDF compiler trace exceeded its admitted bound')
                if code:raise ValueError('UDF tool failed: '+Path(argv[0]).name+' exit '+str(code))
        finally:
            if child.poll() is None:child.kill()
            child.wait();child.stdout.close()
            finished.set()
            if monitor is not None:monitor.join()
    return {'argv':argv,'returncode':code,'log':digest(log)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--sha256',required=True)
    args=parser.parse_args();path=Path(args.config).resolve()
    if digest(path)['sha256']!=args.sha256:raise ValueError('UDF configuration changed')
    config=json.loads(path.read_text(encoding='utf8'));root=path.parents[2]
    dependencies=None;compiler_inputs=None;query_helper=None;queried=None
    if config.get('case_queries'):
        helper=Path(__file__).with_name('case_file_queries.py')
        if (config['mode']!='integrate' or config['case_queries'].get('schema')!='vibeflow.case-file-queries.v1'
                or digest(helper)['sha256']!=config['case_queries']['helper_sha256']):
            raise ValueError('UDF runtime query helper differs from prepared helper')
        spec=importlib.util.spec_from_file_location('case_file_queries',helper)
        query_helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(query_helper)
    if config.get('dependency_tracking'):
        helper=Path(__file__).with_name('udf_dependencies.py')
        if (config['dependency_tracking']['schema']!='vibeflow.udf-compiler-inputs.v1' or
                digest(helper)['sha256']!=config['dependency_tracking']['helper_sha256']):
            raise ValueError('UDF dependency collector differs from prepared helper')
        spec=importlib.util.spec_from_file_location('udf_dependencies',helper)
        dependencies=importlib.util.module_from_spec(spec);spec.loader.exec_module(dependencies)
    if path.relative_to(root).as_posix()!='.vibeflow/prepared/udf.json':raise ValueError('UDF settings outside private root')
    if digest(Path(__file__).with_name('mpi_native.py'))['sha256']!=config['common_native_sha256']:
        raise ValueError('UDF native file helper changed')
    identifier=config['execution_id']
    verify_distribution(config['distro'])
    if str(uuid.UUID(identifier))!=identifier or (config['distro']!='native-linux' and os.getuid()==0):
        raise ValueError('Invalid UDF invocation identity')
    stage=Path('/tmp')/('vibeflow-udf-'+identifier);stage.mkdir(mode=0o700,exist_ok=False)
    proof=root/'.vibeflow/udf';proof.mkdir(parents=True,exist_ok=True)
    ownership={'schema':'vibeflow.udf-stage.v1','execution_id':identifier,'path':str(stage),
        'uid':os.getuid(),'device':stage.stat().st_dev,'inode':stage.stat().st_ino,'removed':False}
    (proof/'stage.json').write_text(json.dumps(ownership),encoding='utf8')
    def interrupted(*_):raise InterruptedError('UDF native operation interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        translate=path_mapping(config['run_dir'] or config['udf_dir'],identifier)
        entries=set(config['source']['directories'])|set(config['source']['files'])
        if len({translate(p) for p in entries})!=len(entries):raise ValueError('UDF native path collision')
        for directory in config['source']['directories']:safe(stage,translate(directory)).mkdir(parents=True,exist_ok=True)
        for name,expected in config['source']['files'].items():
            source=safe(root,name);target=safe(stage,translate(name));target.parent.mkdir(parents=True,exist_ok=True)
            if digest(source)!={k:expected[k] for k in ('size','sha256')}:raise ValueError('UDF sealed input changed')
            shutil.copyfile(source,target);target.chmod(0o755 if expected['executable'] else 0o644)
        before=inventory(stage);budget=[config['max_log_bytes']]
        kind=config.get('udf_kind','functionObject')
        components={'functionObject':('libmyFunctionObject.so','icoFoam'),
                    'boundaryCondition':('libvibeflowVelocityBoundary.so','icoFoam'),
                    'sourceTerm':('libvibeflowUniformAcceleration.so','pimpleFoam'),
                    'turbulenceModel':('libvibeflowKEpsilon.so','pimpleFoam'),
                    'solver':('vibeflowDiffusionFoam','vibeflowDiffusionFoam')}
        if kind not in components:raise ValueError('Unknown UDF component type')
        artifact_name,solver=components[kind]
        executable=kind=='solver'
        udf=safe(stage,translate(config['udf_dir']));artifact=udf/artifact_name
        project=Path(os.environ['WM_PROJECT_DIR']).resolve();appbin=Path(os.environ['FOAM_APPBIN']).resolve()
        names=('wmake','g++') if config['mode']=='build' else (solver,)
        tools={name:(str(artifact) if executable and name==solver else shutil.which(name)) for name in names}
        if not all(tools.values()) or not os.environ.get('WM_PROJECT_VERSION'):raise ValueError('UDF toolchain unavailable')
        if config['mode']=='build' and not Path(tools['wmake']).resolve().is_relative_to(project/'wmake'):
            raise ValueError('wmake differs from selected OpenFOAM release')
        if config['mode']=='integrate' and not executable and not Path(tools[solver]).resolve().is_relative_to(appbin):
            raise ValueError('UDF integration solver differs from selected release')
        fingerprints={name:{'path':value,**digest(Path(value))} for name,value in tools.items()}
        steps={}
        runtime=stage/'.vibeflow';runtime.mkdir()
        trace=query_helper.Trace(stage) if query_helper else None
        if trace:fingerprints['strace']=trace.tool
        if config['mode']=='build':
            # Copying bytes gives fresh timestamps. Remove only this copy's
            # compiler cache so stale native output cannot masquerade as a rebuild.
            for target in (udf/'Make'/os.environ['WM_OPTIONS'],udf/'lnInclude'):
                if target.exists():
                    if not target.resolve().is_relative_to(udf.resolve()):raise ValueError('UDF build cache escapes source')
                    if target.is_dir():shutil.rmtree(target)
                    else:target.unlink()
            artifact.unlink(missing_ok=True)
            os.environ['FOAM_USER_LIBBIN']=str(runtime/'userlib');(runtime/'userlib').mkdir()
            if dependencies is not None:
                os.environ['LC_ALL']='C'
                os.environ['FOAM_EXTRA_CXXFLAGS']=os.environ.get('FOAM_EXTRA_CXXFLAGS','')+' -MD'
                os.environ['FOAM_EXTRA_LDFLAGS']=os.environ.get('FOAM_EXTRA_LDFLAGS','')+' -Wl,-t,--dependency-file='+str(runtime/'link-inputs.d')
                # wmake intentionally clears MAKEFLAGS for its first Make
                # preprocessors. A fixed private launcher still executes the
                # real GNU make, preserving its exact argv and recipe results.
                binary=Path('/usr/bin/make')
                fingerprints['make']={'path':str(binary),**digest(binary)}
                bindir=runtime/'bin';bindir.mkdir()
                launcher=bindir/'make'
                # Make/options is an included makefile. GNU make also needs
                # the m flag to trace recipes while remaking included files.
                launcher.write_text('#!/bin/sh\nexec /usr/bin/make --debug=v,m --trace "$@"\n',encoding='utf8')
                launcher.chmod(0o755)
                os.environ['PATH']=str(bindir)+':'+os.environ['PATH']
            build=[tools['wmake']] if executable else [tools['wmake'],'libso']
            trace_directory=None
            if config.get('dependency_tracking',{}).get('file_queries'):
                tracer=shutil.which('strace')
                if tracer is None:raise ValueError('UDF file input tracking requires strace')
                fingerprints['strace']={'path':tracer,**digest(Path(tracer))}
                build=[tracer,'--kill-on-exit','-ff','-ttt','-yy','-xx','-s','4096','-e','trace=%file,%process,getdents64',
                    '-o',str(runtime/'compiler-query'),*build]
                trace_directory=runtime
            steps['build']=invoke(build,udf,udf/'udf-build.log',budget,trace_directory=trace_directory)
            if dependencies is not None:
                cpp=shutil.which('cpp')
                if cpp is None:raise ValueError('UDF Make dependency preprocessor is unavailable')
                fingerprints['cpp']={'path':cpp,**digest(Path(cpp))}
                compiler_inputs=dependencies.collect(stage,udf,{**config,'wm_options':os.environ['WM_OPTIONS'],
                    'cpp_path':cpp},translate,udf/'udf-build.log')
        if not artifact.is_file():raise ValueError('UDF produced no local ELF artifact')
        with artifact.open('rb') as stream:
            if stream.read(4)!=b'\x7fELF':raise ValueError('UDF produced no local ELF artifact')
        if executable:
            artifact.chmod(0o700)
            load=[str(artifact),'-help']
            fingerprints['loader']={'path':str(artifact),**digest(artifact)}
        else:
            libbin=Path(os.environ['FOAM_LIBBIN'])
            preloads=[libbin/name for name in ('libOpenFOAM.so','libfiniteVolume.so','libmeshTools.so','libfileFormats.so','libsurfMesh.so')]
            preloads.append(libbin/'sys-openmpi/libPstream.so')
            if not all(p.is_file() for p in preloads):raise ValueError('UDF loading libraries unavailable')
            # The official Python container installs its interpreter in /usr/local.
            # Reuse the actual native interpreter; retain the established WSL path.
            loader=sys.executable if config['distro']=='native-linux' else '/usr/bin/python3'
            fingerprints['loader']={'path':loader,**digest(Path(loader))}
            fingerprints['preloads']=[{'path':str(p),**digest(p)} for p in preloads]
            load=['/usr/bin/env','LD_PRELOAD='+':'.join(map(str,preloads)),loader,'-I','-S','-c',
                'import ctypes,sys;ctypes.CDLL(sys.argv[1]);print("UDF_LIBRARY_LOADED",flush=True)',str(artifact)]
        load_log=safe(stage,translate(config.get('load_log',config['udf_dir']+'/udf-load.log')))
        steps['load']=invoke(load,udf,load_log,budget,trace=trace,label='load')
        if config['mode']=='integrate':
            run=safe(stage,translate(config['run_dir']))
            (run/'system/controlDict').write_text(config['control_text'],encoding='utf8')
            os.environ['LD_LIBRARY_PATH']=str(udf)+':'+os.environ.get('LD_LIBRARY_PATH','')
            steps['solve']=invoke([tools[solver],'-case',str(run)],run,run/'udf-solve.log',budget,trace=trace,label='solve')
            transcript=(run/'udf-solve.log').read_text(errors='replace')
            if any(text in transcript for text in ('Unknown function type','Unknown patchField type','Could not load','cannot load library')):
                raise ValueError('UDF component was not loaded by the solver')
            if query_helper:
                control=config['control_text'].encode('utf8')
                # The owned executor opens transcript files before spawning the
                # traced child. Bind those actual writes to its returned hashes;
                # a recomputation can legitimately replace an initial old log.
                derived={config['run_dir']+'/system/controlDict':{'size':len(control),'sha256':hashlib.sha256(control).hexdigest()},
                    config['load_log']:steps['load']['log'],config['run_dir']+'/udf-solve.log':steps['solve']['log']}
                queried=query_helper.collect(stage,udf,config['source'],translate,
                    initial_cwds={'load':udf,'solve':run},
                    derived_inputs={p:r for p,r in derived.items() if p in config['source']['files']})
        after=inventory(stage);changed={p:r for p,r in after.items() if before.get(p)!=r}
        for name in changed:
            target=safe(root,translate(name,reverse=True));target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(safe(stage,name),target)
        deleted=sorted(set(before)-set(after))
        for name in deleted:safe(root,translate(name,reverse=True)).unlink()
        receipt={'schema':'vibeflow.udf-native.v1','execution_id':identifier,'mode':config['mode'],'name':config['name'],
            'run_dir':config['run_dir'],'distro':config['distro'],'uid':os.getuid(),'openfoam':os.environ['WM_PROJECT_VERSION'],
            'tools':fingerprints,'steps':steps,('executable' if executable else 'library'):digest(artifact),
            'changed':{translate(p,reverse=True):{k:r[k] for k in ('size','sha256')} for p,r in changed.items()},
            'deleted':sorted(translate(p,reverse=True) for p in deleted),
            'helper_sha256':digest(Path(__file__))['sha256'],'common_native_sha256':config['common_native_sha256']}
        if compiler_inputs is not None:receipt['compiler_dependencies']=compiler_inputs
        if queried is not None:receipt['case_queries']=queried
        if 'udf_kind' in config:receipt['udf_kind']=kind
        with (proof/'native.json').open('x',encoding='utf8') as stream:json.dump(receipt,stream,sort_keys=True)
    finally:
        if stage.exists() and stage.parent==Path('/tmp') and stage.name=='vibeflow-udf-'+identifier:shutil.rmtree(stage)
        ownership['removed']=not stage.exists();(proof/'stage.json').write_text(json.dumps(ownership),encoding='utf8')


if __name__=='__main__':main()
