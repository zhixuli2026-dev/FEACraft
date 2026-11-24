"""Run the existing numerical extractor against an owned immutable input version."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import postprocess_executor as executor
from app.services.postprocess_artifacts import TOOLCHAIN, config_path, context, derived_config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',required=True)
    parser.add_argument('--execution-id',required=True)
    parser.add_argument('--case-inputs',action='store_true')
    args=parser.parse_args()
    if str(uuid.UUID(args.execution_id))!=args.execution_id:
        raise ValueError('Invalid extraction invocation')
    root=Path.cwd().resolve()
    manifest=context(root)
    case_id,job_id=(manifest['source'][key] for key in ('case_id','job_id'))
    config=derived_config(root,args.run_dir,args.execution_id,case_inputs=args.case_inputs)
    path=config_path(job_id)
    target=root/path
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('xb') as stream:
        stream.write(executor._json_bytes(config))
    result=executor.execute(root,case_id,job_id,path,args.run_dir)
    import numpy
    import vtk
    import matplotlib
    toolchain={'schema':'vibeflow.postprocess-toolchain.v1','execution_id':args.execution_id,
        'case_id':case_id,'job_id':job_id,'attempt_id':manifest['attempt_id'],
        'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'executor_sha256':hashlib.sha256(Path(executor.__file__).read_bytes()).hexdigest(),
        'python':sys.version.split()[0],'vtk':vtk.vtkVersion.GetVTKVersion(),
        'numpy':numpy.__version__,'matplotlib':matplotlib.__version__}
    with (root/TOOLCHAIN).open('xb') as stream:
        stream.write(executor._json_bytes(toolchain))
    print('REGISTERED_POSTPROCESS_OK '+args.execution_id+' '+json.dumps(result['readiness']),flush=True)


if __name__=='__main__':
    main()
