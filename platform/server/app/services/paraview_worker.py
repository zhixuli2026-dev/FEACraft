"""Execute a user script in the sealed case through its owned pvpython/pvbatch."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import paraview_artifacts as artifacts,attempt_workspace as workspace
from app.services.native_execution_client import require_supervision,run_native
from app.services.postprocess_artifacts import context


def main():
    parser=argparse.ArgumentParser()
    for key in ('script','backend','executable','executable-sha256','execution-id','timeout','max-log-bytes'):
        parser.add_argument('--'+key,required=True)
    args=parser.parse_args();root=Path.cwd().resolve();identity=context(root)
    config=json.loads(workspace.checked_path(root,artifacts.CONFIG).read_text(encoding='utf8'));source=config['source']
    if (source!=artifacts.snapshot(root) or config['execution_id']!=args.execution_id
            or config['script']!=args.script or config['backend']!=args.backend):
        raise ValueError('ParaView private input configuration changed before launch')
    if hashlib.sha256(Path(args.executable).read_bytes()).hexdigest()!=args.executable_sha256:
        raise ValueError('ParaView executable changed before launch')
    script = workspace.checked_path(root, args.script)
    if workspace.digest_file(script) != artifacts.expected_script(config):
        raise ValueError('ParaView script differs from the frozen input')
    stamps={p:workspace.checked_path(root,p).stat().st_mtime_ns for p in source['files']}
    log=root/artifacts.LOG;log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('xb') as stream:
        result=run_native('paraview_run',{'script':args.script,'backend':args.backend,'execution_id':args.execution_id},
            timeout=int(args.timeout),sink=stream.write)
        stream.flush();os.fsync(stream.fileno())
    if result.returncode:raise ValueError('ParaView failed with native exit '+str(result.returncode))
    files,deleted=artifacts.changes(root,source,stamps)
    if config.get('catalyst'):
        summary=artifacts.catalyst_result(config,files,deleted)
        folder=config['catalyst']['folder']
        target=workspace.checked_path(root,folder)/'catalyst.json'
        with target.open('x',encoding='utf8') as stream:
            json.dump(summary,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        relative=folder+'/catalyst.json'
        files[relative]=workspace.inventory(root,[relative])[relative]
    receipt={'schema':'vibeflow.paraview-result.v1','case_id':identity['source']['case_id'],
        'job_id':identity['source']['job_id'],'attempt_id':identity['attempt_id'],'execution_id':args.execution_id,
        'script':args.script,'backend':args.backend,'returncode':result.returncode,'inputs':source,'outputs':files,'deleted':deleted,
        'executable_sha256':hashlib.sha256(Path(args.executable).read_bytes()).hexdigest(),
        'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'log':workspace.digest_file(log)}
    with (root/artifacts.RECEIPT).open('x',encoding='utf8') as stream:
        json.dump(receipt,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())


if __name__=='__main__':
    with require_supervision():main()
