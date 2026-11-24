"""Measure real index rebuilds and verify persisted content across a failed rebuild."""
import argparse
from contextlib import closing, nullcontext
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import tracemalloc

from testsupport import SERVER


PREFIX = "workspaces/1/cases/17"


def load_baseline(path):
    spec = importlib.util.spec_from_file_location("app.services.indexsvc_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inventory(module, case_id):
    with closing(sqlite3.connect(module._db_path(case_id))) as conn:
        files = conn.execute("SELECT * FROM files ORDER BY path").fetchall()
        docs = [(key, path, name, hashlib.sha256(text.encode()).hexdigest()) for key, path, name, text in
                conn.execute("SELECT rowid,path,name,content FROM file_fts ORDER BY rowid")]
        meta = dict(conn.execute("SELECT * FROM meta"))
        receipts = conn.execute("SELECT * FROM consumed_events ORDER BY event_id").fetchall()
        catalog = conn.execute("SELECT * FROM indexed_content ORDER BY path").fetchall() if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='indexed_content'").fetchone() else None
    return {"files":files, "docs":docs, "meta":meta, "receipts":receipts, "catalog":catalog}


def measure(case_id, operation):
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = operation(case_id, PREFIX)
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return {"peak_python_bytes":peak, "seconds":round(elapsed,3), "result":result}


def sql(module, case_id, statement):
    with closing(sqlite3.connect(module._db_path(case_id))) as conn:
        conn.execute(statement)
        conn.commit()


def exercise(root, store, baseline):
    if store is not None:
        os.environ.update(store.settings())
    from app.config import get_settings
    get_settings.cache_clear()
    from app import storage
    from app.services import indexsvc
    assert indexsvc.INDEX_DIR == root / "index"
    assert Path(get_settings().storage_fs_root) == root / "files"
    assert storage._use_s3() == (store is not None)
    if store is not None:
        assert (get_settings().s3_bucket,get_settings().s3_endpoint) == (store.bucket,store.endpoint)

    def put(path, data):
        if store is None:
            storage._fs_put(PREFIX, path, data)
        else:
            store.client.put_object(Bucket=store.bucket, Key=PREFIX+"/"+path, Body=data)

    line = ("pressure velocity mesh 压力 🌊\n").encode()
    body = line*(1024*1024//len(line))
    for i in range(24):
        put(f"Ref/field-{i:02}.txt", f"unique{i:02}\n".encode()+body)
    put("Ref/empty.txt", b"")
    put("Ref/invalid.txt", b"bad\xff\xfe utf8")
    put("Ref/target.txt", b"originalterm")
    put("Ref/large.bin", b"ignoredterm "+b"x"*indexsvc._MAX_TEXT)
    put(".cache/hidden.txt", b"hiddenterm")
    del body
    # Initialize SDK/settings outside both measurement windows.
    entries = indexsvc._entries(PREFIX)
    assert len(entries) == 28, [e[0] for e in entries]
    report = {"listed_bytes":sum(e[1] for e in entries), "storage":"s3" if store else "fs"}
    versions = [("current",indexsvc,17)]
    if baseline is not None:
        versions.insert(0, ("baseline",load_baseline(baseline),18))
    observations = []
    for label, module, case_id in versions:
        rebuild = measure(case_id, module.rebuild)
        assert rebuild["result"]["entries"] == 28 and rebuild["result"]["content_entries"] == 27
        sql(module, case_id, "UPDATE meta SET value='0' WHERE key='content_verified_at'")
        unchanged = measure(case_id, module.reconcile)
        assert not unchanged["result"]["changed"]
        assert unchanged["result"]["building_uuid"] == rebuild["result"]["building_uuid"]
        state = inventory(module, case_id)
        assert state["meta"]["source_fingerprint"] == module._digest(module._entries(PREFIX))
        observations.append((state["files"],state["docs"],state["meta"]["projection_fingerprint"]))
        report[label] = {"rebuild":rebuild,"unchanged":unchanged}
    assert observations[0] == observations[-1]
    if baseline is not None:
        for op in ("rebuild","unchanged"):
            assert report["current"][op]["peak_python_bytes"] < report["baseline"][op]["peak_python_bytes"] / 2
        old = inventory(indexsvc,18)
        reopened = indexsvc.reconcile(18,PREFIX)
        assert not reopened["changed"]
        current = inventory(indexsvc,18)
        assert current["meta"]["building_uuid"] == old["meta"]["building_uuid"] and current["docs"] == old["docs"]
        assert current["meta"]["source_fingerprint"] == indexsvc._digest(indexsvc._entries(PREFIX))
        assert [v["path"] for v in indexsvc.search(18,"unique23")["hits"]] == ["Ref/field-23.txt"]
        report["prior_index_reopened"] = True
    assert len(indexsvc.search(17,"unique23")["hits"]) == 1
    for invisible in ("ignoredterm","hiddenterm"):
        assert not indexsvc.search(17,invisible)["hits"]

    event = (17,PREFIX,"index-stream-once","files.changed",{"paths":["Ref/target.txt"]})
    assert indexsvc.consume_event(*event) == "consumed"
    before = inventory(indexsvc,17)
    for op,ref in (("INSERT","NEW"),("DELETE","OLD"),("UPDATE","OLD")):
        sql(indexsvc,17,f"CREATE TRIGGER guard_{op} BEFORE {op} ON indexed_content "
                        f"WHEN {ref}.path='Ref/field-23.txt' BEGIN SELECT RAISE(ABORT,'unrelated text rewritten'); END")
    put("Ref/target.txt", b"modifiedterm")
    sql(indexsvc,17,"CREATE TRIGGER deny_build BEFORE INSERT ON meta WHEN NEW.key='building_uuid' "
                    "BEGIN SELECT RAISE(ABORT,'reject index commit'); END")
    # Force verification even on S3 with second-resolution inventory timestamps.
    sql(indexsvc,17,"UPDATE meta SET value='0' WHERE key='content_verified_at'")
    before["meta"]["content_verified_at"] = "0"
    try:
        indexsvc.reconcile(17,PREFIX)
    except sqlite3.IntegrityError as error:
        assert str(error) == "reject index commit"
    else:
        raise AssertionError("Faulted rebuild committed")
    assert inventory(indexsvc,17) == before
    sql(indexsvc,17,"DROP TRIGGER deny_build")
    fixed = indexsvc.reconcile(17,PREFIX)
    assert fixed["changed"] and fixed["building_uuid"] != before["meta"]["building_uuid"]
    assert [v["path"] for v in indexsvc.search(17,"modifiedterm")["hits"]] == ["Ref/target.txt"]
    assert not indexsvc.search(17,"originalterm")["hits"]
    assert indexsvc.consume_event(*event) == "already_consumed"
    assert inventory(indexsvc,17)["receipts"] == before["receipts"]
    report.update(rollback_verified=True, replay_verified=True, persisted_contents_match=True,incremental_update=True)
    if baseline is not None:
        indexsvc.rebuild(18,PREFIX)
        put("Ref/000-older.txt",b"downgradeterm")
        previous_build = versions[0][1].rebuild(18,PREFIX)["building_uuid"]
        put("Ref/target.txt",b"upgradedterm")
        assert indexsvc.reconcile(18,PREFIX)["changed"]
        state = inventory(indexsvc,18)
        expected = {(path,key,digest) for key,path,_,digest in state["docs"] if path!="Ref/invalid.txt"}
        assert expected == {(p,r,h) for p,h,r in state["catalog"] if p!="Ref/invalid.txt"}
        assert state["meta"]["content_catalog_build"] == state["meta"]["building_uuid"] != previous_build
        assert [v["path"] for v in indexsvc.search(18,"upgradedterm")["hits"]] == ["Ref/target.txt"]
        report["older_writer_followed_by_incremental_update"] = True
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--s3", action="store_true")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="index-streaming-",dir=SERVER.parents[1]/"tmp"))
    os.environ.update(VIBEFLOW_STORAGE_BACKEND="fs", VIBEFLOW_STORAGE_FS_ROOT=str(root/"files"),
                      VIBEFLOW_INDEX_ROOT=str(root/"index"))
    print(root,flush=True)
    context = nullcontext()
    if args.s3:
        from s3testsupport import ObjectStore
        context = ObjectStore(executable=SERVER.parents[1]/"third_party/seaweed-4.52-runtime/weed")
    with context as store:
        result = exercise(root,store,args.baseline)
    (root/"result.json").write_text(json.dumps(result,indent=2),encoding="utf8")
    print(json.dumps(result),flush=True)
