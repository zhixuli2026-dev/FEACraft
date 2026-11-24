"""Generate an immutable draft and exact prior-file backup from sealed inputs."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services.compose_artifacts import OUTPUTS,produce
from app.services.report_artifacts import generation_time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',choices=tuple(OUTPUTS),required=True)
    parser.add_argument('--execution-id',required=True)
    parser.add_argument('--case-name',required=True)
    parser.add_argument('--generated-at')
    args=parser.parse_args()
    root=Path.cwd().resolve()
    if args.generated_at is not None:
        generation_time('--generated-at='+args.generated_at)
    files,_=produce(root,args.execution_id,args.output,args.case_name,args.generated_at)
    for path,data in files.items():
        target=root/path
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
    print('REGISTERED_COMPOSE_OK '+args.execution_id,flush=True)


if __name__=='__main__':
    main()
