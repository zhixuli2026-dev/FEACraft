"""Run the registered Elmer solver and write its attempt-bound receipt."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import attempt_workspace as workspace, elmer_artifacts as artifacts
from app.services.native_execution_client import require_supervision, run_native


def main():
    parser = argparse.ArgumentParser()
    for key in ('input','executable','executable-sha256','execution-id','timeout','max-log-bytes'):
        parser.add_argument('--'+key,required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    config = json.loads(workspace.checked_path(root,artifacts.CONFIG).read_text(encoding='utf8'))
    source = config['source']
    if config['execution_id']!=args.execution_id or source!=artifacts.dependencies(root,args.input):
        raise ValueError('Elmer inputs changed before native launch')
    if artifacts.targets(root,source):
        raise ValueError('Elmer attempt contains outputs from another run')
    if artifacts.has_generated_input(source):
        native = root/artifacts.NATIVE
        native.parent.mkdir(parents=True,exist_ok=False)
        original = workspace.checked_path(root,args.input).read_bytes()
        with native.open('xb') as stream:
            stream.write(artifacts.render_input(source,original))
        artifacts.native_invocation(root,source)
    for directory in artifacts.output_directories(source):
        (root/directory).mkdir(parents=True,exist_ok=True)
    log = root/artifacts.LOG
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('xb') as stream:
        result = run_native('elmer_solve',{'input':args.input,'execution_id':args.execution_id},
                           timeout=int(args.timeout),sink=stream.write)
        stream.flush()
        os.fsync(stream.fileno())
    if result.returncode:
        raise ValueError('Elmer failed with native exit '+str(result.returncode))
    files = artifacts.targets(root,source)
    artifacts.validate_outputs(root,source,files)
    receipt = artifacts.receipt(root,source,args.execution_id,args.executable_sha256,
                                workspace.digest_file(Path(__file__))['sha256'],files)
    with (root/artifacts.RECEIPT).open('x',encoding='utf8') as stream:
        json.dump(receipt,stream,sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == '__main__':
    with require_supervision():
        main()
