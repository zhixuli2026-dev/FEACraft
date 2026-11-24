"""Pack and restore the serial MOOSE checkpoint as one publication member."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
from zipfile import ZIP_STORED, ZipFile


SCHEMA = 'vibeflow.moose-checkpoint.v1'
LIMIT = 128 * 1024 * 1024
HEADER = 'checkpoint.json'
NATIVE = Path(__file__).resolve()


def members(step):
    mesh = step+'-mesh.cpa.gz/'
    state = step+'-restart-0.rd/'
    return (mesh+'1/header.gz',mesh+'1/split-1-0.gz',mesh+'meta_data_mesh.rd/header',
            mesh+'meta_data_mesh.rd/data',state+'header',state+'data')


def validate(archive, model, executable, destination=None):
    if archive.stat().st_size > LIMIT:
        raise ValueError('MOOSE checkpoint exceeds 128 MiB')
    with ZipFile(archive) as bundle:
        entries = bundle.infolist()
        metadata = bundle.getinfo(HEADER)
        if metadata.file_size>512:
            raise ValueError('MOOSE checkpoint metadata exceeds its limit')
        header = json.loads(bundle.read(metadata))
        step = header.get('step')
        if (not isinstance(step,str) or not re.fullmatch(r'\d{4,8}',step)
                or header != {'schema':SCHEMA,'step':step,'model_sha256':model,'executable_sha256':executable}):
            raise ValueError('MOOSE checkpoint differs from the selected mesh, variables or executable')
        expected = members(step)
        if (len(entries)!=len(expected)+1 or {x.filename for x in entries}!={HEADER,*expected}
                or any(x.compress_type!=ZIP_STORED for x in entries) or sum(x.file_size for x in entries)>LIMIT
                or any(bundle.getinfo(name).file_size<1 for name in expected if not name.endswith('meta_data_mesh.rd/data'))):
            raise ValueError('Incomplete or unsupported serial MOOSE checkpoint')
        if destination is not None:
            for name in expected:
                output = destination/name
                output.parent.mkdir(parents=True,exist_ok=True)
                with bundle.open(name) as incoming, output.open('xb') as outgoing:
                    shutil.copyfileobj(incoming,outgoing,length=1024*1024)
    return step


def pack(checkpoints, archive, model, executable):
    steps = [entry.name.split('-',1)[0] for entry in checkpoints.iterdir()
             if re.fullmatch(r'\d{4,8}-restart-0\.rd',entry.name)]
    if not steps:
        raise ValueError('MOOSE did not produce a serial checkpoint')
    step = max(steps,key=int)
    paths = members(step)
    if sum((checkpoints/name).stat().st_size for name in paths)>LIMIT-16384:
        raise ValueError('MOOSE checkpoint exceeds 128 MiB')
    with ZipFile(archive,'x',compression=ZIP_STORED) as bundle:
        bundle.writestr(HEADER,json.dumps({'schema':SCHEMA,'step':step,'model_sha256':model,
                                          'executable_sha256':executable},sort_keys=True))
        for name in paths:
            bundle.write(checkpoints/name,name)
    validate(archive,model,executable)


def main():
    parser = argparse.ArgumentParser()
    for key in ('root','input','base','executable','executable-sha256','model-sha256'):
        parser.add_argument('--'+key,required=True)
    parser.add_argument('--restart')
    parser.add_argument('--checkpoint',action='store_true')
    args = parser.parse_args()
    root = Path(args.root)
    # This process is launched in its own mount namespace by the owned gate.
    # Unmounting the namespace reclaims scratch even when the job is killed.
    subprocess.run(['/usr/bin/mount','-t','tmpfs','-o','size=512m,mode=0700,nosuid,nodev','tmpfs','/tmp'],check=True)
    scratch = Path('/tmp')
    shutil.copyfile(root/args.input,scratch/'model.i')
    argv = [args.executable,'-i','model.i','Outputs/csv=true','Outputs/exodus=true','Outputs/file_base=result']
    if args.restart is not None:
        source = scratch/'original'
        step = validate(root/args.restart,args.model_sha256,args.executable_sha256,source)
        argv.append('Problem/restart_file_base=original/'+step)
    status = subprocess.run(argv,cwd=scratch).returncode
    if status != 0:
        return status
    if args.checkpoint:
        pack(scratch/'result_cp',scratch/'checkpoint.zip',args.model_sha256,args.executable_sha256)
        with (root/(args.base[:-4]+'_checkpoint.zip')).open('xb') as output, (scratch/'checkpoint.zip').open('rb') as data:
            shutil.copyfileobj(data,output,length=1024*1024)
    for suffix in ('.csv','.e'):
        with (root/(args.base+suffix)).open('xb') as output, (scratch/('result'+suffix)).open('rb') as data:
            shutil.copyfileobj(data,output,length=1024*1024)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
