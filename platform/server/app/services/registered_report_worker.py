"""Compile an owned sealed report attempt; publication belongs to its DB owner."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app.services import report_artifacts as artifacts, report_builder
from app.services.attempt_workspace import digest_file
from app.services.postprocess_artifacts import context


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=('compose','markdown'),required=True)
    parser.add_argument('--pandoc',required=True)
    parser.add_argument('--execution-id',required=True)
    parser.add_argument('--generated-at')
    args=parser.parse_args()
    if str(uuid.UUID(args.execution_id))!=args.execution_id:
        raise ValueError('Invalid report invocation')
    root=Path.cwd().resolve()
    identity=context(root)
    case_id,job_id=(identity['source'][key] for key in ('case_id','job_id'))
    if args.generated_at is not None:
        artifacts.generation_time('--generated-at='+args.generated_at)
    markdown,document,_,engineering=artifacts.document(root,args.mode,args.pandoc,args.generated_at)
    with artifacts.compilation(root) as folder:
        outputs=artifacts.compile_document(root,document,args.pandoc,folder)
    source=artifacts.source_path(job_id)
    outputs[source]=markdown
    for path,data in outputs.items():
        target=root/path
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
    common={'case_id':case_id,'job_id':job_id,'attempt_id':identity['attempt_id'],'execution_id':args.execution_id,
        'mode':args.mode,'source_path':source,'input_publications':identity.get('publications',[])}
    if args.generated_at is not None:
        common['generated_at']=args.generated_at
    manifest={**common,'schema':'vibeflow.report-publication.v1','inputs':identity['files'],
        'document_sha256':hashlib.sha256(artifacts.encoded(document)).hexdigest(),
        'artifacts':{path:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for path,data in outputs.items()}}
    if engineering is not None:
        manifest['engineering_report']=engineering
    from app.services.pandoc_execution import run
    version_result=run('pandoc_version',args.pandoc,root);version_result.check_returncode()
    version=version_result.stdout.decode('utf-8','replace').splitlines()[0]
    toolchain={**common,'schema':'vibeflow.report-toolchain.v1','python':sys.version.split()[0],
        'pandoc':str(Path(args.pandoc).resolve()),'pandoc_version':version,
        'pandoc_sha256':digest_file(Path(args.pandoc))['sha256'],
        'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'compiler_sha256':hashlib.sha256(Path(artifacts.__file__).read_bytes()).hexdigest(),
        'builder_sha256':hashlib.sha256(Path(report_builder.__file__).read_bytes()).hexdigest()}
    for path,value in ((artifacts.MANIFEST,manifest),(artifacts.TOOLCHAIN,toolchain)):
        with (root/path).open('xb') as stream:
            stream.write(artifacts.encoded(value))
    print('REGISTERED_REPORT_OK '+args.execution_id,flush=True)


if __name__=='__main__':
    from app.services.native_execution_client import require_supervision
    with require_supervision():main()
