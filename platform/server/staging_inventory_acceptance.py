"""Measure the actual filesystem work of staging reconciliation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc


COUNT = 4000
PREFIX = 'workspaces/1/cases/1'
STAGE = '.staging/job-1-9e6feaedd16d4977b82b05ef86d15e33'


def verify(args):
    repo = Path(__file__).resolve().parents[2]
    root = Path(tempfile.mkdtemp(prefix='staging-inventory-', dir=repo/'tmp'))
    print('STAGING_INVENTORY_ROOT', root, flush=True)
    os.environ.update(VIBEFLOW_STORAGE_FS_ROOT=str(root/'files'), VIBEFLOW_STORAGE_BACKEND='s3',
                      VIBEFLOW_S3_ENDPOINT='http://127.0.0.1:1', VIBEFLOW_S3_BUCKET='staging-inventory-isolated')
    from app.services.staging_sync import collect

    stage = root/'files'/STAGE
    stage.mkdir(parents=True)
    payload = b'unchanged local file\n'
    record = {'sha256':hashlib.sha256(payload).hexdigest(),'size':len(payload)}
    references = {}
    for index in range(COUNT):
        path = f'Ref/volume-{index//100:03}/member-{index:05}.dat'
        target = stage/path
        if index%100 == 0:
            target.parent.mkdir(parents=True)
        target.write_bytes(payload)
        references[path] = record
    manifest = stage.with_name(stage.name+'.manifest.json')
    manifest.write_text(json.dumps({'version':1,'source_prefix':PREFIX,'files':references}), encoding='utf-8')
    del references
    result = {'passed':False,'baseline':args.baseline,'file_count':COUNT,'manifest_bytes':manifest.stat().st_size,
              'actual_model_calls':0,'isolation':'real local filesystem with generated staging inventory'}
    try:
        tracemalloc.start()
        started = time.monotonic()
        try:
            assert collect(STAGE, PREFIX) == ({}, {}, {})
            result['elapsed_seconds'] = time.monotonic()-started
            result['python_peak_bytes'] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        if not args.baseline:
            before = json.loads(args.baseline_receipt.read_text(encoding='utf-8'))
            assert before['passed'] and before['baseline'] and before['file_count']==COUNT
            assert before['manifest_bytes']==result['manifest_bytes']
            result['baseline_peak_bytes'] = before['python_peak_bytes']
            result['reduction_bytes'] = before['python_peak_bytes']-result['python_peak_bytes']
            assert result['reduction_bytes'] >= COUNT*100, result
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type':type(error).__name__,'message':str(error)[:500]}
        raise
    finally:
        (root/'result.json').write_text(json.dumps(result,indent=2)+'\n', encoding='utf-8')
    print('STAGING_INVENTORY_PASS', root/'result.json', flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--baseline', action='store_true')
    mode.add_argument('--baseline-receipt', type=Path)
    verify(parser.parse_args())
