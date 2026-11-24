"""Run SU2 in its sealed workspace through the owned native child channel."""
import argparse
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import su2_artifacts as artifacts,attempt_workspace as workspace
from app.services.native_execution_client import require_supervision,run_native
from app.services.postprocess_artifacts import context


def main():
    parser=argparse.ArgumentParser()
    for key in ('input','executable','executable-sha256','execution-id','timeout','max-log-bytes'):parser.add_argument('--'+key,required=True)
    args=parser.parse_args();root=Path.cwd().resolve();identity=context(root)
    config=json.loads(workspace.checked_path(root,artifacts.CONFIG).read_text(encoding='utf8'));source=config['source']
    if source!=artifacts.dependencies(root,args.input) or config['execution_id']!=args.execution_id:
        raise ValueError('SU2 private input configuration changed before launch')
    if hashlib.sha256(Path(args.executable).read_bytes()).hexdigest()!=args.executable_sha256:
        raise ValueError('SU2 executable changed before launch')
    previous=artifacts.targets(root,source)
    if set(previous)-set(source['mutable_inputs']):raise ValueError('SU2 attempt contains unselected previous outputs')
    stamps={p:workspace.checked_path(root,p).stat().st_mtime_ns for p in previous}
    for base in source['output_bases']:(root/PurePosixPath(base).parent).mkdir(parents=True,exist_ok=True)
    log=root/artifacts.LOG;log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('xb') as stream:
        result=run_native('su2_solve',{'input':args.input,'execution_id':args.execution_id},timeout=int(args.timeout),sink=stream.write)
        stream.flush();os.fsync(stream.fileno())
    if result.returncode:raise ValueError('SU2 failed with native exit '+str(result.returncode))
    files={p:r for p,r in artifacts.targets(root,source).items() if p not in previous or r!=previous[p]
        or workspace.checked_path(root,p).stat().st_mtime_ns!=stamps[p]}
    observed=artifacts.validate_outputs(root,source,files)
    receipt={'schema':'vibeflow.su2-result.v1','case_id':identity['source']['case_id'],
        'job_id':identity['source']['job_id'],'attempt_id':identity['attempt_id'],'execution_id':args.execution_id,
        'input':args.input,'returncode':result.returncode,'inputs':source['files'],'outputs':files,
        'executable_sha256':hashlib.sha256(Path(args.executable).read_bytes()).hexdigest(),
        'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'log':workspace.digest_file(log)}
    if observed is not None:
        receipt['numerical']={**source['numerical'],'observed':observed}
    with (root/artifacts.RECEIPT).open('x',encoding='utf8') as stream:
        json.dump(receipt,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())


if __name__=='__main__':
    with require_supervision():main()
