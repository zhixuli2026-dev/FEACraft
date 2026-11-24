"""由 JobProcess 启动的 PDF 页码 worker；路径始终限制在 VIBEFLOW_CASE_ROOT。"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from pdf_pages import number_pdf, safe_pdf_relative


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--execution-id")
    args = parser.parse_args()
    root = Path(os.environ["VIBEFLOW_CASE_ROOT"]).resolve()

    def resolve(value: str) -> Path:
        relative = safe_pdf_relative(value)
        target = (root / relative).resolve()
        if root != target and root not in target.parents:
            raise ValueError("PDF 路径越出算例根目录")
        return target

    source = resolve(args.input)
    output = resolve(args.output)
    pages = number_pdf(source, output)
    if args.execution_id:
        import uuid
        identifier=str(uuid.UUID(args.execution_id))
        manifest=json.loads((root.parent/'manifest.json').read_text(encoding='utf8'))
        if root.name!='run' or manifest['attempt_id']!=root.parent.name:
            raise ValueError('Registered PDF worker requires its own attempt directory')
        def record(path):
            data=path.read_bytes()
            return {'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        value={'schema':'vibeflow.pdf-number.v1','execution_id':identifier,'attempt_id':manifest['attempt_id'],
            'job_id':manifest['source']['job_id'],'case_id':manifest['source']['case_id'],
            'input':safe_pdf_relative(args.input),'output':safe_pdf_relative(args.output),'pages':pages,
            'input_record':record(source),'output_record':record(output),
            'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'library_sha256':hashlib.sha256(Path(__file__).with_name('pdf_pages.py').read_bytes()).hexdigest()}
        receipt=root/'.vibeflow/pdf-receipts'/f'{identifier}.json'
        receipt.parent.mkdir(parents=True,exist_ok=True)
        with receipt.open('x',encoding='utf8') as stream:
            json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
    print(f"PDF page numbering succeeded: {pages} pages -> {args.output}")


if __name__ == "__main__":
    main()
