"""Fixed FloTHERM compilation worker using the owned native child channel."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services import flotherm_library_artifacts as artifacts, attempt_workspace as workspace
from app.services.native_execution_client import require_supervision, run_native
from app.services.postprocess_artifacts import context


def main():
    parser = argparse.ArgumentParser()
    for key in ('source-dir', 'destination-dir', 'distro', 'bashrc', 'execution-id', 'timeout', 'max-log-bytes'):
        parser.add_argument('--'+key, required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    identity = context(root)
    path = workspace.checked_path(root, artifacts.CONFIG)
    config = json.loads(path.read_text(encoding='utf8'))
    source = config['source']
    if source != artifacts.snapshot(root):
        raise ValueError('FloTHERM sealed inputs changed')
    for key in ('source_dir', 'destination_dir', 'distro', 'bashrc', 'execution_id'):
        if config[key] != getattr(args, key):
            raise ValueError('FloTHERM compilation invocation differs')
    if config['max_log_bytes'] != int(args.max_log_bytes):
        raise ValueError('FloTHERM log budget changed')
    stamps = {p: workspace.checked_path(root, p).stat().st_mtime_ns for p in source['files']}
    log = root/artifacts.LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('xb') as stream:
        purpose = 'flotherm_library_linux' if args.distro == 'native-linux' else 'flotherm_library'
        result = run_native(purpose, {'distro': args.distro, 'execution_id': args.execution_id,
            'config_sha256': workspace.digest_file(path)['sha256']}, timeout=int(args.timeout), sink=stream.write)
        stream.flush()
        os.fsync(stream.fileno())
    if result.returncode:
        raise ValueError('FloTHERM compilation failed with exit '+str(result.returncode))
    native = json.loads(workspace.checked_path(root, artifacts.NATIVE).read_text(encoding='utf8'))
    outputs, deleted = artifacts.changes(root, source, stamps)
    receipt = {'schema': 'vibeflow.flotherm-library-publication.v1', 'case_id': identity['source']['case_id'],
        'job_id': identity['source']['job_id'], 'attempt_id': identity['attempt_id'], 'execution_id': args.execution_id,
        'inputs': source, 'outputs': outputs, 'deleted': deleted, 'native': native,
        'worker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'log': workspace.digest_file(log)}
    with (root/artifacts.RECEIPT).open('x', encoding='utf8') as stream:
        json.dump(receipt, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == '__main__':
    with require_supervision():
        main()
