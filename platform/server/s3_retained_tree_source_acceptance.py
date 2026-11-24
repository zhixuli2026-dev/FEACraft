"""Exercise the retained local source and the independent S3 journal."""
import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from app import storage
from app.config import get_settings
from app.services import s3_file_transactions as remote
from app.services.case_write import CaseWriteConflict
from app.services.s3_live_store import DIRECTORY
from app.services.staging_sync import StagingFile
from s3testsupport import ObjectStore
from testsupport import SERVER


PREFIX = 'workspaces/1/cases/19'
PATH = 'Ref/restored.bin'
SIZE = 12*1024*1024
OLD = b'original before tree publication'
MODE = {'ContentType': 'application/octet-stream'}


def rejected(operation, code):
    try:
        operation()
    except CaseWriteConflict as error:
        assert error.code == code, error
    else:
        raise AssertionError('Changed evidence was accepted')


def exercise(client, work, prefix, make_source):
    case = SimpleNamespace(id=int(prefix.rsplit('/', 1)[-1]), s3_prefix=prefix)
    journal = remote._directory(str(uuid.uuid4()))
    after_key = remote._after_object(journal, 0, PATH)
    before_key = f'{journal}/before/0'
    public = remote.objects.arguments(prefix, PATH)
    private = remote.objects.arguments(prefix, after_key)
    data = b'a'*SIZE
    changed = b'z'*SIZE
    current = work/PATH
    current.parent.mkdir(parents=True)
    current.write_bytes(data)
    source = make_source(remote.objects.digest(data))
    client.put_object(**public, Body=OLD, **MODE, IfNoneMatch='*')
    before, etag, original_metadata = remote.snapshot(prefix, PATH)
    remote._backup_before(prefix, PATH, before_key, SIZE, {PATH: before})
    metadata = remote._prepare_after(prefix, after_key, source, MODE)
    entry = {'path': PATH, 'before': before, 'before_metadata': original_metadata,
             'after': source.expected, 'after_object': after_key, 'after_metadata': metadata}

    def apply():
        remote._apply(case, journal, 0, entry, 'after', 'before', retained_source=source)

    raw = client.raw.raw
    call = raw._make_api_call
    uploads = []

    def modify_during_upload(operation, args):
        key = args.get('Key', '')
        if operation == 'PutObject' and key.startswith(f'{prefix}/{DIRECTORY}/objects/') and key.endswith('/restored.bin'):
            current.write_bytes(changed)
            uploads.append(key)
        return call(operation, args)

    with patch.object(raw, '_make_api_call', new=modify_during_upload):
        rejected(apply, 'recovery_evidence_invalid')
    [unpublished] = uploads
    with closing(raw.get_object(Bucket=public['Bucket'], Key=unpublished)['Body']) as body:
        assert body.read() == changed
    assert remote.snapshot(prefix, PATH) == (before, etag, original_metadata)
    current.write_bytes(data)

    raw.put_object(**private, Body=changed, **MODE)
    rejected(apply, 'file_recovery_required')
    assert remote.snapshot(prefix, PATH) == (before, etag, original_metadata)
    raw.put_object(**private, Body=data, **metadata)
    claimed, original = [], remote._temporary_copy

    @contextmanager
    def observe(size):
        claimed.append(size)
        with original(size) as stream:
            yield stream

    with patch.object(remote, '_temporary_copy', new=observe):
        apply()
        assert remote.snapshot(prefix, PATH)[0] == source.expected and claimed == []
        current.unlink()
        remote._apply(case, journal, 0, entry, 'before', 'after')
        restored, _, restored_metadata = remote.snapshot(prefix, PATH)
        assert restored == before and restored_metadata == original_metadata
        assert claimed == [len(OLD)]
        remote._apply(case, journal, 0, entry, 'after', 'before')
        assert remote.snapshot(prefix, PATH)[0] == source.expected
        assert claimed == [len(OLD), SIZE]
    return {'bytes': SIZE, 'with_retained_source_scratch_bytes': 0, 'journal_only_scratch_bytes': SIZE,
            'rollback_from_journal_without_local_source': True,
            'changed_upload_was_unpublished': True, 'changed_journal_rejected': True}


def run():
    runtime = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    with ObjectStore(executable=runtime, volume_max=12, authenticated=True) as store:
        with tempfile.TemporaryDirectory(dir=store.root, prefix='retained-source-') as directory:
            work = Path(directory)
            settings = dict(store.settings(), VIBEFLOW_STORAGE_FS_ROOT=str(work/'storage'),
                            VIBEFLOW_CODEX_STATE_ROOT=str(work/'cli'), VIBEFLOW_SKILLS_STORAGE_ROOT=str(work/'skills'))
            with patch.dict(os.environ, settings), patch.object(storage, '_client', None):
                get_settings.cache_clear()
                try:
                    with closing(storage.s3()) as client:
                        result = exercise(client, work, PREFIX,
                                          lambda expected: remote.TreeSnapshotFile(work, PATH, expected))
                        prefix = 'workspaces/1/cases/20'
                        snapshot = uuid.uuid4().hex
                        files = storage._fs_root()/'.terminal-recovery'/snapshot/'files'
                        result['ordinary'] = exercise(client, files, prefix,
                            lambda expected: remote.journal.RecoveryFileSource(PATH, expected, snapshot, prefix))
                        prefix = 'workspaces/1/cases/21'
                        stage = '.staging/job-21-'+uuid.uuid4().hex
                        result['staging'] = exercise(client, storage._fs_root()/stage, prefix,
                            lambda expected: StagingFile(stage, PATH, expected, prefix))
                finally:
                    get_settings.cache_clear()
    assert store.proc.poll() is not None
    return dict(result, store=str(store.root/'store.json'))


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    enforce([Path(__file__).resolve()])
    result = dict(run(), passed=True, real_model_calls=0, simulated_model_calls=0)
    args.report.write_text(json.dumps(result, indent=2)+'\n', encoding='utf8')
    print('S3_RETAINED_TREE_SOURCE_PASS', args.report, flush=True)
