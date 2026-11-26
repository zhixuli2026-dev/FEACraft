"""PPTX 后台作业入口；spec 和全部资源均在当前算例中。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services.ppt_builder import build_pptx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--execution-id")
    args = parser.parse_args()
    root = Path(os.environ["VIBEFLOW_CASE_ROOT"]).resolve()
    spec_path = (root / args.spec).resolve()
    if root != spec_path and root not in spec_path.parents:
        raise ValueError("spec 越出算例根目录")
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    result = build_pptx(root, document)
    if args.execution_id:
        import uuid
        identifier=str(uuid.UUID(args.execution_id))
        manifest=json.loads((root.parent/'manifest.json').read_text(encoding='utf8'))
        if root.name!='run' or manifest['attempt_id']!=root.parent.name:
            raise ValueError('Registered PPT worker requires its own attempt directory')
        output=(root/result['output']).read_bytes()
        value={'schema':'vibeflow.ppt-build.v1','execution_id':identifier,'attempt_id':manifest['attempt_id'],
            'job_id':manifest['source']['job_id'],'case_id':manifest['source']['case_id'],
            'request_sha256':hashlib.sha256(spec_path.read_bytes()).hexdigest(),**result,
            'output_record':{'size':len(output),'sha256':hashlib.sha256(output).hexdigest()},
            'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'library_sha256':hashlib.sha256(Path(__file__).with_name('ppt_builder.py').read_bytes()).hexdigest()}
        receipt=root/'.vibeflow/ppt-receipts'/f'{identifier}.json'
        receipt.parent.mkdir(parents=True,exist_ok=True)
        with receipt.open('x',encoding='utf8') as stream:
            json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
