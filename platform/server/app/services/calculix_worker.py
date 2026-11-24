"""Invoke CCX through the attempt's owned child channel, then seal its receipt."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import calculix_artifacts as artifacts,attempt_workspace as workspace
from app.services.native_execution_client import require_supervision,run_native
from app.services.postprocess_artifacts import context


def main():
    parser=argparse.ArgumentParser()
    for key in ('input','executable','executable-sha256','execution-id','timeout','max-log-bytes'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args();root=Path.cwd().resolve();identity=context(root)
    config=json.loads(workspace.checked_path(root,artifacts.CONFIG).read_text(encoding='utf8'))
    source=config['source']
    transfer=source.get('pressure_transfer')
    if transfer is not None:
        from app.services.calculix_pressure_transfer import verify_sealed

        verify_sealed(root,transfer)
    if source!=artifacts.dependencies(root,args.input,pressure_transfer=transfer):raise ValueError('CalculiX inputs changed before launch')
    if hashlib.sha256(Path(args.executable).read_bytes()).hexdigest()!=args.executable_sha256:
        raise ValueError('CalculiX executable changed before launch')
    if artifacts.targets(root,source):
        # An eigenfile is both a prior input and an optional new output.
        extra=set(artifacts.targets(root,source))-set(source['mutable_inputs'])
        if extra:raise ValueError('CalculiX attempt already contains previous outputs')
    digest=artifacts.native_digest(source)
    native=artifacts.native_input(args.input,digest is not None)
    if native!=args.input:
        destination=root/native;destination.parent.mkdir(parents=True,exist_ok=False)
        original=workspace.checked_path(root,args.input)
        if digest is None:
            shutil.copyfile(original,destination)
        else:
            with destination.open('xb') as file:
                file.write(artifacts.render_input(source,original.read_bytes()))
        for suffix in ('.eig','.rin','.vwf'):
            path=args.input[:-4]+suffix
            if path in source['files']:
                shutil.copyfile(workspace.checked_path(root,path),root/(native[:-4]+suffix))
    log=root/artifacts.LOG;log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('xb') as stream:
        result=run_native('calculix_solve',{'input':args.input,'execution_id':args.execution_id},
            timeout=int(args.timeout),sink=stream.write)
        stream.flush();os.fsync(stream.fileno())
    if result.returncode:raise ValueError('CalculiX failed with native exit '+str(result.returncode))
    if native!=args.input:
        for path in sorted((root/native).parent.iterdir()):
            if not path.name.startswith('model.') or path.name in {'model.inp','model.rin'}:continue
            destination=args.input[:-4]+path.name[len('model'):]
            if destination in source['files'] and destination not in source['mutable_inputs']:continue
            target=workspace.checked_path(root,str(Path(destination).parent))/Path(destination).name
            if target.exists():workspace.ordinary(target)
            os.replace(workspace.checked_path(root,path.relative_to(root).as_posix()),target)
    files=artifacts.targets(root,source)
    observed=artifacts.validate_outputs(root,source,files)
    receipt={'schema':'vibeflow.calculix-result.v1','case_id':identity['source']['case_id'],
        'job_id':identity['source']['job_id'],'attempt_id':identity['attempt_id'],'execution_id':args.execution_id,
        'input':args.input,'returncode':result.returncode,'inputs':source['files'],'outputs':files,
        'executable_sha256':hashlib.sha256(Path(args.executable).read_bytes()).hexdigest(),
        'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'log':workspace.digest_file(log)}
    if 'contact' in source:
        receipt['native_contact_sha256']=source['contact']['native_input_sha256']
    if 'mechanical_loads' in source:
        receipt['native_loads_sha256']=workspace.digest_file(root/native)['sha256']
    if 'elasticity' in source:
        receipt['native_elasticity_sha256']=workspace.digest_file(root/native)['sha256']
    if 'element_families' in source:
        receipt['element_families']=source['element_families']
    if observed is not None:
        receipt['numerical']={**source['numerical'],'observed':observed}
    with (root/artifacts.RECEIPT).open('x',encoding='utf8') as stream:
        json.dump(receipt,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())


if __name__=='__main__':
    with require_supervision():main()
