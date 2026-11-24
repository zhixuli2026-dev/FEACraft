"""Invoke the owned Code_Aster child and verify its declared results."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import attempt_workspace as workspace, code_aster_artifacts as artifacts
from app.services.native_execution_client import require_supervision, run_native


def main():
    parser = argparse.ArgumentParser()
    for key in ('input','executable','distro','executable-sha256','native-sha256','execution-id','timeout','max-log-bytes'):
        parser.add_argument('--'+key,required=True)
    parser.add_argument('--state-sha256')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    config = json.loads(workspace.checked_path(root,artifacts.CONFIG).read_text(encoding='utf8'))
    plan = artifacts.selection(root,args.input)
    source = artifacts.dependencies(root,plan)
    expected = {'source':source,'execution_id':args.execution_id,'distro':args.distro}
    engine = config.get('engine_sha256')
    restart = config.get('restart')
    if plan.get('base') and isinstance(engine,str) and re.fullmatch('[0-9a-f]{64}',engine):
        expected['engine_sha256'] = engine
    if restart is not None:
        expected['restart'] = restart
    if (config!=expected or bool(args.state_sha256)!=bool(plan.get('base'))
            or bool(plan.get('base'))!=('engine_sha256' in expected)
            or ('input' in plan.get('base',{}))!=(restart is not None)):
        raise ValueError('Code_Aster inputs changed before native launch')
    if artifacts.targets(root,plan):
        raise ValueError('Code_Aster attempt already contains declared results')
    log = root/artifacts.LOG
    log.parent.mkdir(parents=True,exist_ok=True)
    purpose = 'code_aster_solve_linux' if args.distro=='native-linux' else 'code_aster_solve'
    parameters = {'input':args.input,'distro':args.distro,'execution_id':args.execution_id}
    with log.open('xb') as stream:
        result = run_native(purpose,parameters,timeout=int(args.timeout),sink=stream.write)
        stream.flush()
        os.fsync(stream.fileno())
    if result.returncode:
        raise ValueError(f'Code_Aster failed with native exit {result.returncode}')
    files = artifacts.targets(root,plan)
    artifacts.validate_outputs(root,source,files,engine)
    receipt = artifacts.receipt(root,source,args.execution_id,args.executable_sha256,
        workspace.digest_file(Path(__file__))['sha256'],files,restart)
    with (root/artifacts.RECEIPT).open('x',encoding='utf8') as stream:
        json.dump(receipt,stream,sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


if __name__=='__main__':
    with require_supervision():
        main()
