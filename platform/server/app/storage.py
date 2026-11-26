"""对象存储层 — 双后端：本地文件系统(fs, 当前默认) / SeaweedFS S3(s3, 团队服务器形态)

文件事实源布局：
  {storage_root}/workspaces/{user_id}/cases/{case_id}/0-caseDict/caseDict ...

切换方式：VIBEFLOW_STORAGE_BACKEND=fs|s3（默认 fs）
"""
import hashlib
import io
import json
import os
import shutil
import time
import uuid
from contextlib import closing
from pathlib import Path

from .config import get_settings

# ======================= S3 (SeaweedFS) 实现 =======================
_client = None


def _send_small_s3_body(request, **_):
    """Send small buffered PUTs immediately when the SDK requests a handshake."""
    if request.headers.get("Expect", "").lower() != "100-continue":
        return
    body = request.body
    if isinstance(body, io.BytesIO):
        with body.getbuffer() as buffer:
            size = buffer.nbytes
    elif isinstance(body, (bytes, bytearray)):
        size = len(body)
    else:
        return
    if size <= 64 * 1024:
        del request.headers["Expect"]


def s3():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config as BotoConfig
        s = get_settings()
        raw = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint,
            aws_access_key_id=s.s3_access_key,
            aws_secret_access_key=s.s3_secret_key,
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"},retries={'total_max_attempts':1}),
            region_name=s.s3_region,
        )
        raw.meta.events.register("before-sign.s3.PutObject", _send_small_s3_body)
        from .services.s3_live_store import Client
        _client = Client(raw)
    return _client


def _s3_key(s3_prefix: str, path: str) -> str:
    path = path.replace("\\", "/").lstrip("/")
    if ".." in path.split("/"):
        raise ValueError("path traversal not allowed")
    return f"{s3_prefix.rstrip('/')}/{path}"


def _s3_put(s3_prefix, path, data: bytes, content_type="application/octet-stream"):
    s3().put_object(Bucket=get_settings().s3_bucket, Key=_s3_key(s3_prefix, path),
                    Body=data, ContentType=content_type)


def _s3_body(s3_prefix, path):
    obj = s3().get_object(Bucket=get_settings().s3_bucket, Key=_s3_key(s3_prefix, path))
    return obj["Body"]


def _s3_get(s3_prefix, path) -> bytes:
    with closing(_s3_body(s3_prefix, path)) as body:
        return body.read()


def _s3_size(s3_prefix, path) -> int:
    obj = s3().head_object(Bucket=get_settings().s3_bucket, Key=_s3_key(s3_prefix, path))
    return int(obj["ContentLength"])


def _s3_get_range(s3_prefix, path, start: int, end: int) -> bytes:
    obj = s3().get_object(Bucket=get_settings().s3_bucket, Key=_s3_key(s3_prefix, path),
                          Range=f"bytes={start}-{end}")
    return obj["Body"].read()


def _s3_delete(s3_prefix, path):
    s3().delete_object(Bucket=get_settings().s3_bucket, Key=_s3_key(s3_prefix, path))


def _s3_exists(s3_prefix, path) -> bool:
    from botocore.exceptions import ClientError
    try:
        s3().head_object(Bucket=get_settings().s3_bucket, Key=_s3_key(s3_prefix, path))
        return True
    except ClientError:
        return False


def _s3_list(s3_prefix, subdir="") -> list[dict]:
    from .services.storage_listing_budget import ListingBudget
    budget = ListingBudget()
    s = get_settings()
    directory = subdir.replace("\\", "/").strip("/")
    if directory == ".":
        directory = ""
    prefix = _s3_key(s3_prefix, directory).rstrip("/") + "/"
    paginator = s3().get_paginator("list_objects_v2")
    out = []
    pages = iter(paginator.paginate(Bucket=s.s3_bucket, Prefix=prefix))
    while True:
        budget.check()
        try:page = next(pages)
        except StopIteration:break
        budget.check()
        for item in page.get("Contents", []):
            budget.visit()
            if item["Key"].endswith("/"):
                continue
            # Internal object inventories are relative to the queried prefix.
            rel = item["Key"][len(prefix):]
            out.append({"path": rel, "size": item["Size"],
                        "modified": item["LastModified"].isoformat(),
                        "etag": item.get("ETag", "").strip('"')})
    out.sort(key=lambda item: item["path"])
    budget.check()
    return out


def _s3_presign(s3_prefix, path, expires=3600) -> str:
    return s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().s3_bucket, "Key": _s3_key(s3_prefix, path)},
        ExpiresIn=expires,
    )


# ======================= 本地文件系统实现 =======================
def _fs_root() -> Path:
    root = Path(get_settings().storage_fs_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _fs_path(s3_prefix: str, path: str) -> Path:
    from .storage_paths import contains_file_stage

    path = path.replace("\\", "/").lstrip("/")
    if ".." in path.split("/"):
        raise ValueError("path traversal not allowed")
    if contains_file_stage(path):
        raise ValueError("file staging is private")
    root = _fs_root().resolve()
    prefix = Path(s3_prefix.strip("/"))
    if prefix.anchor or ".." in prefix.parts:
        raise ValueError("storage prefix must be relative to the storage root")
    declared = root / prefix
    try:
        base = declared.resolve()
    except RuntimeError as error:
        raise ValueError("storage prefix cannot be resolved") from error
    if not base.is_relative_to(root):
        raise ValueError("storage prefix escapes storage root")
    # The prefix is the recorded namespace. Resolving a link must not select
    # a different case, workspace, or local staging directory inside the root.
    if base != declared:
        raise ValueError("storage prefix resolves to a different directory")
    # resolve 会处理最近存在父目录的符号链接；目标不存在时也不能借链接逃逸 prefix。
    try:
        p = (base / path).resolve()
    except RuntimeError as error:
        raise ValueError("storage path cannot be resolved") from error
    if not p.is_relative_to(base):
        raise ValueError("path escapes storage prefix")
    if contains_file_stage(p.relative_to(root).as_posix()):
        raise ValueError("file staging is private")
    return p


def _fs_put(s3_prefix, path, data: bytes, content_type="application/octet-stream"):
    p = _fs_path(s3_prefix, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, p)
    finally:
        temporary.unlink(missing_ok=True)


def _fs_get(s3_prefix, path) -> bytes:
    return _fs_path(s3_prefix, path).read_bytes()


def _fs_size(s3_prefix, path) -> int:
    return _fs_path(s3_prefix, path).stat().st_size


def _fs_get_range(s3_prefix, path, start: int, end: int) -> bytes:
    with _fs_path(s3_prefix, path).open("rb") as f:
        f.seek(start)
        return f.read(end - start + 1)


def _fs_delete(s3_prefix, path):
    p = _fs_path(s3_prefix, path)
    if p.is_file():
        p.unlink()


def _fs_exists(s3_prefix, path) -> bool:
    return _fs_path(s3_prefix, path).is_file()


def _fs_entries(s3_prefix, subdir="", *, budget):
    from .storage_paths import is_file_stage
    from .services.storage_usage import _ordinary_info
    import stat
    base = _fs_path(s3_prefix, subdir or ".")
    root = _fs_path(s3_prefix, "")
    if not base.is_dir():
        return
    pending = [base]
    while pending:
        budget.check()
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                budget.visit()
                if is_file_stage(entry.name):
                    continue
                st = _ordinary_info(entry.stat(follow_symlinks=False))
                if stat.S_ISDIR(st.st_mode):
                    pending.append(Path(entry.path))
                else:
                    rel = Path(entry.path).relative_to(root).as_posix()
                    yield rel, st
    budget.check()


def _fs_list(s3_prefix, subdir="") -> list[dict]:
    from .services.storage_listing_budget import ListingBudget
    budget = ListingBudget()
    out = [{"path": rel, "size": info.st_size,
            "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(info.st_mtime))}
           for rel, info in _fs_entries(s3_prefix, subdir, budget=budget)]
    out.sort(key=lambda x: x["path"])
    budget.check()
    return out


# ======================= 统一入口 =======================
def _use_s3() -> bool:
    return get_settings().storage_backend == "s3"


def ensure_bucket():
    if not _use_s3():
        _fs_root()  # 确保目录存在
        return

    from botocore.exceptions import ClientError

    settings = get_settings()
    client = s3()
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
        return
    except ClientError as error:
        if error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 404:
            raise
    configuration = {}
    if settings.s3_region != 'us-east-1':
        configuration['CreateBucketConfiguration'] = {'LocationConstraint': settings.s3_region}
    try:
        client.create_bucket(Bucket=settings.s3_bucket, **configuration)
    except ClientError as error:
        if error.response.get('Error', {}).get('Code') != 'BucketAlreadyOwnedByYou':
            raise
        client.head_bucket(Bucket=settings.s3_bucket)


def put_bytes(s3_prefix, path, data: bytes, content_type="application/octet-stream"):
    from .services.attempt_workspace import assert_mutable_path
    assert_mutable_path(s3_prefix, path)
    (_s3_put if _use_s3() else _fs_put)(s3_prefix, path, data, content_type)
    from .services.case_write import record_write
    record_write(s3_prefix, path)


def put_text(s3_prefix, path, text: str, content_type="text/plain; charset=utf-8"):
    put_bytes(s3_prefix, path, text.encode("utf-8"), content_type)


def get_bytes(s3_prefix, path, *, expected_size=None) -> bytes:
    if expected_size is None:
        return (_s3_get if _use_s3() else _fs_get)(s3_prefix, path)
    if type(expected_size) is not int or expected_size < 0:
        raise ValueError("Expected file size must be a nonnegative byte count")
    stream = _s3_body(s3_prefix, path) if _use_s3() else _fs_path(s3_prefix, path).open("rb")
    with closing(stream):
        data = stream.read(expected_size + 1)
    if len(data) != expected_size:
        raise RuntimeError("Source changed while reading file content")
    return data


def file_size(s3_prefix, path) -> int:
    return (_s3_size if _use_s3() else _fs_size)(s3_prefix, path)


def get_range(s3_prefix, path, start: int, end: int) -> bytes:
    """读取闭区间 [start, end]；调用方负责范围校验。"""
    return (_s3_get_range if _use_s3() else _fs_get_range)(s3_prefix, path, start, end)


def delete(s3_prefix, path):
    from .services.attempt_workspace import assert_mutable_path
    assert_mutable_path(s3_prefix, path)
    (_s3_delete if _use_s3() else _fs_delete)(s3_prefix, path)
    from .services.case_write import record_write
    record_write(s3_prefix, path)


def exists(s3_prefix, path) -> bool:
    return (_s3_exists if _use_s3() else _fs_exists)(s3_prefix, path)


def list_tree(s3_prefix, subdir="") -> list[dict]:
    if not _use_s3():
        return _fs_list(s3_prefix, subdir)
    directory = subdir.replace("\\", "/").strip("/")
    if directory == ".":
        directory = ""
    # Public paths are case-relative on both backends. Keep the private S3
    # listing contract used by immutable attempt/publication inventories.
    return [{**item, "path": directory + "/" + item["path"] if directory else item["path"]}
            for item in _s3_list(s3_prefix, directory)]


def presign_download(s3_prefix, path, expires=3600) -> str:
    if not _use_s3():
        raise ValueError("fs 下载请使用带 case_id 的 /raw 路由")
    return _s3_presign(s3_prefix, path, expires)


def copy_prefix(src_prefix: str, dst_prefix: str):
    """复制整个前缀（case 脚手架/克隆用）"""
    for item in list_tree(src_prefix):
        body = get_bytes(src_prefix, item["path"])
        put_bytes(dst_prefix, item["path"], body)


class StagingConflict(RuntimeError):
    """本地作业产物与更新后的远端事实源冲突。"""


def _staging_manifest_path(stage_prefix: str) -> Path:
    name = stage_prefix.rstrip("/").rsplit("/", 1)[-1]
    return _fs_path(".staging", f"{name}.manifest.json")


def _terminal_cleanup_marker(stage_prefix: str) -> Path:
    _check_stage_prefix(stage_prefix)
    name=stage_prefix.rstrip('/').rsplit('/',1)[-1]
    # Keep the final name lexical so even a dangling marker blocks sync.
    return _fs_path('.staging','.') / (name+'.terminal-cleanup.json')


def _check_stage_prefix(stage_prefix: str):
    normalized = stage_prefix.replace("\\", "/").strip("/")
    if not normalized.startswith(".staging/job-") or ".." in normalized.split("/"):
        raise ValueError("invalid job staging prefix")


def _remote_version(item: dict):
    if item.get("etag"):
        return ("etag", item["etag"])
    return ("stat", item.get("size"), item.get("modified"))


def prepare_job_staging(s3_prefix: str, job_id: int) -> str:
    """Reserve node capacity and download a version-pinned staging snapshot."""
    from .services.staging_download import prepare
    stage_prefix = f".staging/job-{job_id}-{uuid.uuid4().hex}"
    _check_stage_prefix(stage_prefix)
    return prepare(s3_prefix, stage_prefix)


def sync_job_staging(stage_prefix: str, s3_prefix: str):
    """Legacy low-level CAS merge; app callers use staging_sync.sync with SQL."""
    from .services import staging_preparation
    _check_stage_prefix(stage_prefix)
    if staging_preparation.registered(stage_prefix) or os.path.lexists(_terminal_cleanup_marker(stage_prefix)):
        raise StagingConflict('终端副本已进入清理，禁止把部分删除的目录重新上传')
    manifest_path = _staging_manifest_path(stage_prefix)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("source_prefix") != s3_prefix:
        raise ValueError("staging source prefix mismatch")
    from .services.attempt_workspace import _reserved
    # Legacy staging cannot rewrite or delete immutable execution versions,
    # including copies made before this exclusion was introduced.
    initial = {path:value for path,value in manifest["files"].items() if not _reserved(path)}
    local_paths = {item["path"] for item in _fs_list(stage_prefix) if not _reserved(item['path'])}
    remote = {item["path"]: item for item in list_tree(s3_prefix)}
    uploads = {}
    for path in sorted(local_paths):
        data = _fs_get(stage_prefix, path)
        digest = hashlib.sha256(data).hexdigest()
        if path in initial and initial[path]["sha256"] == digest:
            continue
        uploads[path] = (data, digest)
    deletions = [path for path in initial if path not in local_paths]

    # 全部冲突先预检，避免在已知冲突时只上传一半产物。
    for path, (data, digest) in list(uploads.items()):
        before = initial.get(path)
        current = remote.get(path)
        if current is None:
            if before is not None:
                raise StagingConflict(f"远端文件已被其他变更删除：{path}")
            continue
        if before is not None and _remote_version(current) == _remote_version(before):
            continue
        # 重试时已上传的相同内容视为完成；不同内容才是真实冲突。
        if hashlib.sha256(get_bytes(s3_prefix, path)).hexdigest() == digest:
            uploads.pop(path)
            continue
        raise StagingConflict(f"远端文件已被其他变更修改：{path}")
    for path in list(deletions):
        current = remote.get(path)
        if current is None:
            deletions.remove(path)  # 重试时已删除
        elif _remote_version(current) != _remote_version(initial[path]):
            raise StagingConflict(f"远端文件已被其他变更修改，禁止删除：{path}")

    if uploads or deletions:
        from botocore.exceptions import ClientError
        from .services.s3_case_files import require_conditions
        from .services.attempt_objects import arguments
        require_conditions(s3_prefix)
        def mutate(path, data):
            current = remote.get(path)
            etag = current.get('etag') if current else None
            if current and not etag:
                raise StagingConflict('远端文件缺少条件写入版本：' + path)
            condition = {'IfMatch': '"' + etag.strip('"') + '"'} if etag else {'IfNoneMatch': '*'}
            try:
                if data is None:
                    s3().delete_object(**arguments(s3_prefix,path), IfMatch=condition['IfMatch'])
                else:
                    import mimetypes
                    s3().put_object(**arguments(s3_prefix,path), Body=data,
                                   ContentType=mimetypes.guess_type(path)[0] or 'application/octet-stream', **condition)
            except ClientError as error:
                if error.response['ResponseMetadata']['HTTPStatusCode'] == 412:
                    raise StagingConflict('同步期间远端文件发生变化：' + path) from error
                raise
        for path, (data, _) in uploads.items():
            mutate(path, data)
        for path in deletions:
            mutate(path, None)
    return {"uploaded": len(uploads), "deleted": len(deletions)}


def cleanup_job_staging(stage_prefix: str, *, synchronized=True):
    """Dispose of a published job stage or a stage discarded before startup."""
    from .services.staging_cleanup import clean
    clean(stage_prefix, synchronized=synchronized)
