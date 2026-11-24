"""Compile a sealed LaTeX attempt; only the durable owner may publish it."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import latex_artifacts as artifacts
from app.services.postprocess_artifacts import context


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--main',required=True)
    parser.add_argument('--recipe',choices=artifacts.RECIPES,required=True)
    parser.add_argument('--distro',required=True)
    parser.add_argument('--execution-id',required=True)
    parser.add_argument('--epoch',type=int,required=True)
    parser.add_argument('--max-log-bytes',type=int,required=True)
    parser.add_argument('--resources-json',required=True)
    parser.add_argument('--input-aliases-json', default='{}')
    args=parser.parse_args()
    root=Path.cwd().resolve()
    identity=context(root)
    parameters=vars(args)
    parameters['main']=artifacts.main_path(args.main)
    parameters['resources']=artifacts.normalize_resources(json.loads(args.resources_json))
    parameters['input_aliases']=artifacts.normalize_input_aliases(json.loads(args.input_aliases_json))
    artifacts.history(args.execution_id)
    runtime=artifacts.probe(args.distro,args.recipe)
    files,_,_=artifacts.generate(root,parameters,identity,runtime)
    for path,data in files.items():
        target=root/path
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
    print('REGISTERED_LATEX_OK '+args.execution_id,flush=True)


if __name__=='__main__':
    try:
        from app.services.native_execution_client import require_supervision
        with require_supervision():main()
    except TimeoutError as error:
        print(str(error),flush=True)
        raise SystemExit(124)
