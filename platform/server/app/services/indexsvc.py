"""工作区文件索引服务（SQLite FTS5 全文索引与当前存储对账）。

存储：server/data/index/case-<id>.sqlite（每算例一库）。
- rebuild：全量扫描重建（文本文件读内容进 FTS；>5MB/二进制只索引路径名）
- 已提交事件消费与独立读取对账；业务写入不依赖索引成功
- 搜索：文件名 LIKE + FTS 内容 snippet（unicode61 分词）
"""
from __future__ import annotations

import os
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from datetime import datetime

from botocore.exceptions import ClientError

from ..storage import _fs_path, _use_s3, get_bytes, list_tree
from .file_read import FileRead
from .request_deadline import current

INDEX_DIR = Path(os.getenv("VIBEFLOW_INDEX_ROOT", Path(__file__).parent.parent.parent / "data" / "index"))

_EXCLUDE_DIRS = {".cache", "node_modules", "__pycache__", "build", "build-win",
                 "build-deb", "build-linux-package", "cmake", ".vibeflow"}
_MAX_TEXT = 5_000_000  # >5MB 不索引内容
_CONTENT_VERIFY_SECONDS = 60


class IndexBuildBusy(RuntimeError):
    pass


class IndexSourceChanged(RuntimeError):
    pass


_DDL = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, name TEXT NOT NULL, parent TEXT NOT NULL,
  size INTEGER NOT NULL, mtime INTEGER NOT NULL, suffix TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
  path UNINDEXED, name, content, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS indexed_content (
  path TEXT PRIMARY KEY, digest TEXT NOT NULL, fts_rowid INTEGER NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS consumed_events (
  event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, consumed_at REAL NOT NULL);
"""


def _db_path(case_id: int) -> Path:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    return INDEX_DIR / f"case-{case_id}.sqlite"


def _connect(case_id: int) -> sqlite3.Connection:
    # Derived-index contention must fail promptly rather than occupy the case
    # coordinator while native/business finalization waits for its SQL lock.
    conn = sqlite3.connect(str(_db_path(case_id)), timeout=0.2)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_DDL)
    except BaseException:
        conn.close()
        raise
    return conn


def _sqlite_busy(error):
    return (getattr(error, 'sqlite_errorcode', 0) & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _archive_legacy_build_marker(case_id: int, building_id: str):
    # Older workers used a marker before opening SQLite. Preserve that brief
    # compatibility guard while the SQLite writer lock proves exclusivity.
    path = _db_path(case_id).with_suffix(".building")
    try:
        age = time.time() - path.stat().st_mtime
        if age < 600:
            raise IndexBuildBusy("另一个索引构建仍在进行")
        os.replace(path, path.with_name(f"{path.stem}.stale-{building_id}"))
    except FileNotFoundError:
        pass


def _storage_entries(s3_prefix: str):
    from ..storage_paths import is_file_stage

    deadline = current()
    if _use_s3():
        for item in list_tree(s3_prefix):
            rel = item["path"]
            if any(part in _EXCLUDE_DIRS for part in PurePosixPath(rel).parts):
                continue
            modified = datetime.fromisoformat(item["modified"].replace("Z", "+00:00"))
            # Multiple published versions can have the same size and timestamp.
            yield rel, item["size"], int(modified.timestamp()), (item["modified"], item["etag"])
        return
    root = _fs_path(s3_prefix, ".")
    def failed(error):
        raise error
    # Prune excluded case-relative directories before traversing them. An
    # ancestor of the configured storage root may itself be named "build".
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        if deadline is not None:
            deadline.check()
        dirs[:] = sorted(name for name in dirs if name not in _EXCLUDE_DIRS and not is_file_stage(name))
        for name in sorted(files):
            if deadline is not None:
                deadline.check()
            if name in _EXCLUDE_DIRS or is_file_stage(name):
                continue
            rel = (Path(directory) / name).relative_to(root).as_posix()
            path = _fs_path(s3_prefix, rel)
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError as error:
                raise IndexSourceChanged('扫描期间文件发生变化，请重试') from error
            identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
            yield rel, stat.st_size, int(stat.st_mtime), identity


def _entries(s3_prefix):
    return sorted(_storage_entries(s3_prefix), key=lambda item: item[0])


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
        separators=(',', ':')).encode('utf8')).hexdigest()


def _meta(conn, key):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def _indexed_bytes(prefix, path, size):
    try:
        return get_bytes(prefix, path, expected_size=size)
    except (FileNotFoundError, ClientError, RuntimeError) as error:
        disappeared = isinstance(error, FileNotFoundError)
        if isinstance(error, ClientError):
            from .s3_live_store import missing
            disappeared = missing(error)
        resized = type(error) is RuntimeError and error.args == ('Source changed while reading file content',)
        if disappeared or resized:
            raise IndexSourceChanged('构建期间文件发生变化，请重试') from error
        raise


def _sync_index(conn, s3_prefix, rows, *, incremental, readers=None):
    paths = tuple(readers) if readers is not None else ()
    clause = ' WHERE path IN ('+','.join('?' for _ in paths)+')' if paths else ''
    if incremental:
        previous_files = {row[0]:row for row in conn.execute('SELECT * FROM files'+clause, paths)}
        previous_text = {path:(digest,rowid) for path,digest,rowid in conn.execute(
            'SELECT path,digest,fts_rowid FROM indexed_content'+clause, paths)}
    else:
        previous_files, previous_text = {}, {}
        conn.execute('DELETE FROM files')
        conn.execute('DELETE FROM file_fts')
        conn.execute('DELETE FROM indexed_content')

    contents = []
    deadline = current()
    for row in rows:
        if deadline is not None:
            deadline.check()
        rel, name, _, size, _, _ = row
        if previous_files.pop(rel,None) != row:
            conn.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)',row)
        if size > _MAX_TEXT:
            continue
        if readers is not None:
            with closing(readers[rel].chunks()) as chunks:
                data = b''.join(chunks)
        else:
            data = _indexed_bytes(s3_prefix, rel, size)
        digest = hashlib.sha256(data).hexdigest()
        contents.append((rel,digest))
        previous = previous_text.pop(rel,None)
        if previous is None or previous[0] != digest:
            # Preserve the row ID on replacement, and leave unchanged FTS
            # rows untouched. Removed and newly oversized files are below.
            rowid = previous[1] if previous else None
            cursor = conn.execute('INSERT OR REPLACE INTO file_fts (rowid,path,name,content) VALUES (?,?,?,?)',
                                  (rowid,rel,name,data.decode('utf8','replace')))
            conn.execute('INSERT OR REPLACE INTO indexed_content VALUES (?,?,?)',(rel,digest,cursor.lastrowid))
        del data
    conn.executemany('DELETE FROM files WHERE path=?',((p,) for p in previous_files))
    conn.executemany('DELETE FROM file_fts WHERE rowid=?',((r,) for _,r in previous_text.values()))
    conn.executemany('DELETE FROM indexed_content WHERE path=?',((p,) for p in previous_text))
    return _digest([rows, contents]), len(contents)


def _rebuild_locked(conn, s3_prefix, building_id, *, entries=None, only_if_changed=False):
    """Caller holds the index write transaction before reading the filesystem."""
    entries = _entries(s3_prefix) if entries is None else entries
    rows = []
    for rel, size, mtime, _ in entries:
        path = PurePosixPath(rel)
        rows.append((rel, path.name, path.parent.as_posix(), size, mtime, path.suffix.lower()))

    old_build = _meta(conn,'building_uuid')
    # An older application may have rebuilt these tables during a downgrade.
    # Only use row IDs produced by the same committed index build.
    incremental = bool(only_if_changed and old_build and _meta(conn,'content_catalog_build') == old_build)
    projection, content_count = _sync_index(conn,s3_prefix,rows,incremental=incremental)
    changed = not only_if_changed or projection != _meta(conn,'projection_fingerprint')
    if changed:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('building_uuid', ?)", (building_id,))
    else:
        building_id = old_build
    deadline = current()
    if deadline is not None:
        deadline.check()
    if entries != _entries(s3_prefix):
        raise IndexSourceChanged('构建期间文件发生变化，请重试')
    conn.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', [
        ('content_catalog_build', building_id),
        ('source_fingerprint', _digest(entries)),
        ('projection_fingerprint', projection),
        ('content_verified_at', str(time.time()))])
    return {'entries': len(rows), 'content_entries': content_count,
            'building_uuid': building_id, 'changed': changed}


def enabled(case_id: int) -> bool:
    return (INDEX_DIR / f'case-{case_id}.sqlite').exists()


@contextmanager
def _published_snapshot(case_id: int):
    if not enabled(case_id):
        yield None
        return
    uri = _db_path(case_id).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as conn:
        deadline = current()
        if deadline is not None:
            conn.set_progress_handler(lambda: int(deadline.expired), 1000)
        try:
            conn.execute('BEGIN')
            has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            yield conn if has_meta and _meta(conn, 'building_uuid') else None
            if deadline is not None:
                deadline.check()
        except sqlite3.OperationalError:
            if deadline is not None:
                deadline.check()
            raise


def refresh_paths(case_id, prefix, paths):
    """Refresh committed path hints under the existing case and index locks."""
    eligible = [PurePosixPath(path) for path in paths if not _EXCLUDE_DIRS.intersection(PurePosixPath(path).parts)]
    if not eligible or not enabled(case_id):
        return
    deadline = current()
    with closing(_connect(case_id)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        build = _meta(conn, 'building_uuid')
        if not build or _meta(conn,'content_catalog_build') != build:
            return  # The durable consumer can rebuild a legacy content catalog.
        readers, rows = {}, []
        for path in eligible:
            if deadline is not None:
                deadline.check()
            rel = path.as_posix()
            try:
                reader = FileRead(prefix, rel)
            except FileNotFoundError:
                readers[rel] = None
            else:
                readers[rel] = reader
                rows.append((rel, path.name, path.parent.as_posix(), reader.size, reader.mtime, path.suffix.lower()))
        before = conn.total_changes
        _sync_index(conn, prefix, rows, incremental=True, readers=readers)
        if conn.total_changes != before:
            # These hints can be truncated and cannot attest untouched paths.
            conn.execute("DELETE FROM meta WHERE key='source_fingerprint'")
        if deadline is not None:
            deadline.check()
        conn.commit()


def reconcile(case_id: int, s3_prefix: str):
    """Reconcile and publish under the derived index write transaction.

    Never enable an unused index. Metadata catches ordinary changes, while a
    periodic content check also repairs same-size writes with preserved mtime.
    An unchanged projection retains its build identity and all event receipts.
    """
    if not enabled(case_id):
        return {'indexed': False, 'changed': False}
    try:
        with closing(_connect(case_id)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not _meta(conn, 'building_uuid'):
                return {'indexed': False, 'changed': False}
            entries = _entries(s3_prefix)
            verified = float(_meta(conn, 'content_verified_at') or 0)
            elapsed = time.time() - verified
            versions_known = not _use_s3() or all(identity[1] for _, _, _, identity in entries)
            if (versions_known and _meta(conn, 'source_fingerprint') == _digest(entries)
                    and 0 <= elapsed < _CONTENT_VERIFY_SECONDS):
                return {'indexed': True, 'changed': False}
            result = _rebuild_locked(conn, s3_prefix, uuid.uuid4().hex,
                                    entries=entries, only_if_changed=True)
            deadline = current()
            if deadline is not None:
                deadline.check()
            conn.commit()
            return {'indexed': True, **result}
    except sqlite3.OperationalError as error:
        if not _sqlite_busy(error):
            raise
        # WAL readers can use the last committed build while another writer
        # owns the index. An interrupted first build has no published snapshot.
        with _published_snapshot(case_id) as published:
            return {'indexed': published is not None, 'changed': False}


def _consumed(conn, case_id, s3_prefix, event_id, event_type, payload):
    receipt = conn.execute('SELECT digest FROM consumed_events WHERE event_id=?', (event_id,)).fetchone()
    if receipt is None:
        return False
    if receipt[0] != _event_digest(case_id, s3_prefix, event_type, payload):
        raise ValueError('Consumed event identity differs from its original payload')
    return True


def _event_digest(case_id, s3_prefix, event_type, payload):
    return hashlib.sha256(json.dumps([case_id, s3_prefix, event_type, payload],
        sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf8')).hexdigest()


def already_consumed(case_id, s3_prefix, event_id, event_type, payload):
    path = INDEX_DIR / f'case-{case_id}.sqlite'
    if not path.exists():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)) as conn:
        # Pre-receipt indexes acquire their table in the normal write transaction.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='consumed_events'").fetchone():
            return False
        return _consumed(conn, case_id, s3_prefix, event_id, event_type, payload)


def consume_event(case_id, s3_prefix, event_id, event_type, payload):
    """Commit projection and receipt together; replay never repeats a rebuild.

    Read current files under the same SQLite lock as incremental writes/manual
    rebuilds. Late events cannot restore an earlier snapshot over newer writes.
    A killed worker releases this lock immediately, with no stale lock-file wait.
    """
    if not enabled(case_id):
        return 'index_not_enabled'
    conn = _connect(case_id)
    try:
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        if not _meta(conn, 'building_uuid'):
            return 'index_not_enabled'
        if _consumed(conn, case_id, s3_prefix, event_id, event_type, payload):
            return 'already_consumed'
        digest = _event_digest(case_id, s3_prefix, event_type, payload)
        _rebuild_locked(conn, s3_prefix, uuid.uuid4().hex, only_if_changed=True)
        conn.execute('INSERT INTO consumed_events VALUES (?,?,?)', (event_id, digest, time.time()))
        conn.commit()
        return 'consumed'
    finally:
        conn.close()


def rebuild(case_id: int, s3_prefix: str) -> dict:
    """全量重建（调用方负责 asyncio.to_thread 防阻塞）"""
    t0 = time.monotonic()
    building_id = uuid.uuid4().hex
    try:
        with closing(_connect(case_id)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _archive_legacy_build_marker(case_id, building_id)
            result = _rebuild_locked(conn, s3_prefix, building_id)
            deadline = current()
            if deadline is not None:
                deadline.check()
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as error:
        if not _sqlite_busy(error):
            raise
        raise IndexBuildBusy("另一个索引构建仍在进行") from error
    return {**result, "elapsed_ms": int((time.monotonic() - t0) * 1000)}


def status(case_id: int, s3_prefix: str | None = None) -> dict:
    if s3_prefix is not None:
        reconcile(case_id, s3_prefix)
    deadline = current()
    if deadline is not None:
        deadline.check()
    with _published_snapshot(case_id) as conn:
        if conn is None:
            return {"indexed": False}
        n = conn.execute("SELECT count(*) FROM files").fetchone()[0]
        mtime = int(_db_path(case_id).stat().st_mtime)
        return {"indexed": True, "entries": n, "indexed_at": mtime,
                "building_uuid": _meta(conn, 'building_uuid')}


def search(case_id: int, query: str, limit: int = 30, content: bool = True,
           *, s3_prefix: str | None = None) -> dict:
    if s3_prefix is not None:
        reconcile(case_id, s3_prefix)
    with _published_snapshot(case_id) as conn:
        if conn is None:
            return {"ready": False, "hits": []}
        hits = []
        # 文件名 LIKE
        for row in conn.execute(
                "SELECT path, name, size, mtime FROM files WHERE name LIKE ? OR path LIKE ? "
                "ORDER BY length(path), path LIMIT ?",
                (f"%{query}%", f"%{query}%", limit)):
            hits.append({"path": row[0], "name": row[1], "size": row[2],
                         "mtime": row[3], "via": "name"})
        # FTS 内容（FTS5 unicode61 对中文按字分词不佳但可用；查询加引号短语容错）
        if content and len(hits) < limit:
            try:
                for row in conn.execute(
                        "SELECT path, snippet(file_fts, 2, '', '', '…', 12) "
                        "FROM file_fts WHERE file_fts MATCH ? ORDER BY path LIMIT ?",
                        (f'"{query}"', limit - len(hits))):
                    if not any(h["path"] == row[0] and h["via"] == "name" for h in hits):
                        hits.append({"path": row[0], "snippet": row[1], "via": "content"})
            except sqlite3.OperationalError:
                pass  # 非法 FTS 查询语法时不炸（LIKE 结果仍在）
        return {"ready": True, "hits": hits}


def search_workspace(case_refs: list[tuple[int, str]], query: str,
                     limit: int = 30, content: bool = True) -> dict:
    """搜索同一已授权工作区的 case 索引，并在结果中保留明确归属。

    各 case 先保留自身相关度顺序，再轮转合并，避免第一个 case 占满全部名额。
    """
    groups = []
    indexed_cases = 0
    deadline = current()
    for case_id, case_name in case_refs:
        if deadline is not None:
            deadline.check()
        result = search(case_id, query, limit, content)
        if result["ready"]:
            indexed_cases += 1
        hits = [{**hit, "case_id": case_id, "case_name": case_name} for hit in result["hits"]]
        if hits:
            groups.append(hits)
    merged = []
    while len(merged) < limit and any(groups):
        for group in groups:
            if group and len(merged) < limit:
                merged.append(group.pop(0))
    return {"ready": indexed_cases > 0, "total_cases": len(case_refs),
            "indexed_cases": indexed_cases, "hits": merged}
