"""Convert sealed FloTHERM inputs inside the attempt's owned working directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services import attempt_workspace as workspace, flotherm_convert_artifacts as artifacts
from app.services.flotherm_convert_job import convert
from app.services.native_execution_client import require_supervision
from app.services.postprocess_artifacts import context


def main():
    parser = argparse.ArgumentParser()
    for name in ('ir', 'settings', 'output', 'execution-id'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    identity = context(root)
    config = json.loads(workspace.checked_path(root, artifacts.CONFIG).read_text(encoding='utf8'))
    if (config['source'] != artifacts.snapshot(root) or config['ir_path'] != args.ir
            or config['settings_path'] != (args.settings or None) or config['output_dir'] != args.output
            or config['execution_id'] != args.execution_id or config['engine_sha256'] != artifacts.engine_hashes()):
        raise ValueError('FloTHERM sealed conversion differs from its invocation')
    artifacts.empty_output(root, args.output)
    convert(root, args.ir, args.settings or None, args.output)
    observed = artifacts.snapshot(root)['files']
    outputs = {p: r for p, r in observed.items() if p not in config['source']['files']}
    receipt = {'schema': 'vibeflow.flotherm-convert-publication.v1',
        'case_id': identity['source']['case_id'], 'job_id': identity['source']['job_id'],
        'attempt_id': identity['attempt_id'], 'execution_id': args.execution_id,
        'inputs': config['source'], 'outputs': outputs, 'engine_sha256': config['engine_sha256'],
        'worker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    path = root / artifacts.RECEIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf8') as stream:
        json.dump(receipt, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == '__main__':
    with require_supervision():
        main()
