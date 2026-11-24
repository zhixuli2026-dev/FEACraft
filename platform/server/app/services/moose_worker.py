"""Run MOOSE in the attempt's owned native child and seal its receipt."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import attempt_workspace as workspace, moose_artifacts as artifacts
from app.services.native_execution_client import require_supervision, run_native


def main():
    parser = argparse.ArgumentParser()
    for key in ('input','executable','distro','executable-sha256','execution-id','timeout','max-log-bytes'):
        parser.add_argument('--'+key,required=True)
    parser.add_argument('--native-sha256')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    config = json.loads(workspace.checked_path(root,artifacts.CONFIG).read_text(encoding='utf8'))
    restart = config.get('restart')
    source = artifacts.dependencies(root,args.input,restart is not None)
    expected = {'source':source,'execution_id':args.execution_id,'distro':args.distro}
    if restart is not None:
        expected['restart'] = restart
    if config != expected or bool(args.native_sha256)!=('model_sha256' in source):
        raise ValueError('MOOSE inputs changed before native launch')
    checkpoint = source.get('checkpoint',False)
    steps = source.get('mesh_adaptivity',{}).get('cycles',0)
    if artifacts.targets(root,args.input,checkpoint=checkpoint,adaptive_steps=steps):
        raise ValueError('MOOSE attempt contains results from another run')
    log = root/artifacts.LOG
    log.parent.mkdir(parents=True,exist_ok=True)
    purpose = 'moose_solve_linux' if args.distro=='native-linux' else 'moose_solve'
    params = {'input':args.input,'distro':args.distro,'execution_id':args.execution_id}
    with log.open('xb') as stream:
        result = run_native(purpose,params,timeout=int(args.timeout),sink=stream.write)
        stream.flush()
        os.fsync(stream.fileno())
    if result.returncode:
        raise ValueError(f'MOOSE failed with native exit {result.returncode}')
    files = artifacts.targets(root,args.input,checkpoint=checkpoint,adaptive_steps=steps)
    artifacts.validate_outputs(root,source,files,args.executable_sha256)
    receipt = artifacts.receipt(root,source,args.execution_id,args.executable_sha256,
        workspace.digest_file(Path(__file__))['sha256'],files,restart)
    with (root/artifacts.RECEIPT).open('x',encoding='utf8') as stream:
        json.dump(receipt,stream,sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == '__main__':
    with require_supervision():
        main()
