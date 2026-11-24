"""J15：staging 快照、并发冲突、删除保护、幂等重试和成功清理（离线可控 S3）。"""
import hashlib
import io
import os
from pathlib import Path
import shutil
import tempfile

ROOT = tempfile.mkdtemp(prefix="vibeflow-s3-staging-")
os.environ["VIBEFLOW_STORAGE_BACKEND"] = "s3"
os.environ["VIBEFLOW_STORAGE_FS_ROOT"] = str(Path(ROOT) / "fs")

from app import storage  # noqa: E402


class FakeS3:
    def __init__(self):
        self.data = {}
        self.versions = {}
        self.clock = 0

    def list(self, subdir=""):
        prefix = subdir.rstrip("/") + "/" if subdir else ""
        return [{"path": path, "size": len(content), "modified": str(self.versions[path]),
                 "etag": hashlib.sha256(content).hexdigest()}
                for path, content in sorted(self.data.items()) if path.startswith(prefix)]

    def get(self, path):
        return self.data[path]

    def put(self, path, content):
        self.clock += 1
        self.data[path] = content
        self.versions[path] = self.clock

    def delete(self, path):
        self.clock += 1
        self.data.pop(path, None)
        self.versions.pop(path, None)


remote = FakeS3()
storage._s3_list = lambda _prefix, subdir="": remote.list(subdir)
storage._s3_get = lambda _prefix, path: remote.get(path)
storage._s3_put = lambda _prefix, path, data, content_type=None: remote.put(path, data)
storage._s3_delete = lambda _prefix, path: remote.delete(path)

SOURCE = "workspaces/test/cases/staging"

# The low-level compatibility merge now requires actual conditional semantics.
# Native app callers additionally use the SQL journal, covered by staging_sync_smoke.
class ConditionalClient:
    def check(self, args):
        from botocore.exceptions import ClientError
        path=args['Key'].removeprefix(SOURCE+'/')
        content=remote.data.get(path)
        etag='"'+hashlib.sha256(content).hexdigest()+'"' if content is not None else None
        if (args.get('IfNoneMatch')=='*' and content is not None or
            args.get('IfMatch') is not None and args['IfMatch']!=etag):
            raise ClientError({'ResponseMetadata':{'HTTPStatusCode':412},'Error':{'Code':'412'}},'fixture')
        return path,content,etag
    def put_object(self,**args):
        path,_,_=self.check(args);remote.put(path,args['Body'])
        return {'ETag':'"'+hashlib.sha256(args['Body']).hexdigest()+'"'}
    def delete_object(self,**args):
        path,_,_=self.check(args);remote.delete(path);return {}
    def get_object(self,**args):
        from botocore.exceptions import ClientError
        path,data,etag=self.check(args)
        if data is None:raise ClientError({'ResponseMetadata':{'HTTPStatusCode':404},'Error':{'Code':'404'}},'fixture')
        return {'Body':io.BytesIO(data),'ETag':etag,'ContentLength':len(data)}
storage.s3=lambda:ConditionalClient()


def put_text(stage, path, text):
    storage._fs_put(stage, path, text.encode("utf-8"))


def delete(stage, path):
    storage._fs_delete(stage, path)


remote.put("Ref/shared.txt", b"BASE")
remote.put("Ref/remove.txt", b"DELETE")

try:
    # 两个快照写不同产物：互不覆盖；第二个快照的删除只作用于初始远端文件。
    first = storage.prepare_job_staging(SOURCE, 101)
    second = storage.prepare_job_staging(SOURCE, 102)
    assert first != second
    put_text(first, "D-post/a.txt", "A")
    put_text(second, "D-post/b.txt", "B")
    delete(second, "Ref/remove.txt")
    assert storage.sync_job_staging(first, SOURCE) == {"uploaded": 1, "deleted": 0}
    assert storage.sync_job_staging(second, SOURCE) == {"uploaded": 1, "deleted": 1}
    assert remote.get("D-post/a.txt") == b"A" and remote.get("D-post/b.txt") == b"B"
    assert "Ref/remove.txt" not in remote.data
    print("1. independent staging, different outputs and snapshot deletion: OK")

    # 同一路径并发写：后同步者必须冲突，不能覆盖先完成的结果，失败现场保留。
    third = storage.prepare_job_staging(SOURCE, 103)
    fourth = storage.prepare_job_staging(SOURCE, 104)
    put_text(third, "D-post/conflict.txt", "FIRST")
    put_text(fourth, "D-post/conflict.txt", "SECOND")
    storage.sync_job_staging(third, SOURCE)
    try:
        storage.sync_job_staging(fourth, SOURCE)
        raise AssertionError("same-path concurrent write was not rejected")
    except storage.StagingConflict:
        pass
    assert remote.get("D-post/conflict.txt") == b"FIRST"
    assert storage._fs_path(fourth, "D-post/conflict.txt").read_text() == "SECOND"
    print("2. same-path conflict preserves remote winner and local evidence: OK")

    # 第一次只上传一部分后网络失败；重试跳过相同内容并完成剩余上传。
    fifth = storage.prepare_job_staging(SOURCE, 105)
    put_text(fifth, "D-post/retry-a.txt", "RETRY_A")
    put_text(fifth, "D-post/retry-b.txt", "RETRY_B")
    original_put = remote.put
    failed = {"done": False}

    def flaky_put(path, content):
        if path == "D-post/retry-b.txt" and not failed["done"]:
            failed["done"] = True
            raise TimeoutError("injected S3 failure")
        original_put(path, content)

    remote.put = flaky_put
    try:
        storage.sync_job_staging(fifth, SOURCE)
        raise AssertionError("injected failure did not surface")
    except TimeoutError:
        pass
    assert storage._fs_path(fifth, "D-post/retry-b.txt").exists()
    remote.put = original_put
    assert storage.sync_job_staging(fifth, SOURCE) == {"uploaded": 1, "deleted": 0}
    assert remote.get("D-post/retry-a.txt") == b"RETRY_A"
    assert remote.get("D-post/retry-b.txt") == b"RETRY_B"
    storage.cleanup_job_staging(fifth)
    assert not storage._fs_path(fifth, ".").exists()
    assert not storage._staging_manifest_path(fifth).exists()
    print("3. partial failure idempotent retry and success cleanup: OK")

    # 本地删除与外部修改竞争时，外部更新绝不能被删除。
    sixth = storage.prepare_job_staging(SOURCE, 106)
    remote.put("Ref/shared.txt", b"EXTERNAL")
    delete(sixth, "Ref/shared.txt")
    try:
        storage.sync_job_staging(sixth, SOURCE)
        raise AssertionError("external update was deleted")
    except storage.StagingConflict:
        pass
    assert remote.get("Ref/shared.txt") == b"EXTERNAL"
    print("4. deletion conflict preserves newer remote content: OK")

    # A Windows directory lock must not silently erase the recovery manifest.
    from unittest.mock import patch
    seventh=storage.prepare_job_staging(SOURCE,107)
    manifest=storage._staging_manifest_path(seventh)
    denied=PermissionError('injected directory lock');denied.winerror=5
    with patch('shutil.rmtree',side_effect=denied):
        try:
            storage.cleanup_job_staging(seventh)
            raise AssertionError('failed directory cleanup was reported as complete')
        except PermissionError:pass
    assert manifest.exists() and storage._fs_path(seventh,'.').is_dir()
    original_remove=shutil.rmtree;calls=[]
    def transient_remove(*args,**kwargs):
        calls.append(True)
        if len(calls)==1:
            busy=OSError('injected transient sharing violation');busy.winerror=32;raise busy
        return original_remove(*args,**kwargs)
    with patch('shutil.rmtree',side_effect=transient_remove):storage.cleanup_job_staging(seventh)
    assert len(calls)==2 and not manifest.exists() and not storage._fs_path(seventh,'.').exists()
    print('5. failed cleanup retains manifest; bounded sharing-violation retry removes it only after directory: OK')

    storage.cleanup_job_staging(first)
    storage.cleanup_job_staging(second)
    storage.cleanup_job_staging(third)
    print("\n=== J15 staging concurrency/deletion/retry smoke passed ===")
finally:
    shutil.rmtree(ROOT, ignore_errors=True)
