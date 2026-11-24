"""Verify public file updates, job publication and read-side index recovery without a WebSocket."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put


FIXTURE = '''
from app.services import durable_events
import asyncio,time
from contextvars import ContextVar
from app import storage
from app.db import SessionLocal
from app.models import Workspace
from app.services import file_transactions,storage_admission
original=durable_events.deliver_pending
async def dispatch(*args,**kwargs):
    if (root/'pause-index-delivery').exists():
        (root/'index-paused').touch()
        return 0
    return await original(*args,**kwargs)
durable_events.deliver_pending=dispatch

# Hold a real peer quota lock only after this writer's business commit.
original_finish=file_transactions.finish_committed_edit
short_settlement=ContextVar("short_settlement",default=False)
normal_bounds=storage_admission.bounds
def bounds(*,settlement=False):
    end,parent=normal_bounds(settlement=settlement)
    if settlement and short_settlement.get():end=min(end,time.monotonic()+.12)
    return end,parent
storage_admission.bounds=bounds
async def finish(db,case,*args,**kwargs):
    armed=root/'contend-after-save'
    if armed.exists() and not storage._use_s3():
        expire=armed.read_text()=='expire'
        armed.unlink()
        ready=asyncio.Event()
        async def compete():
            async with SessionLocal() as other:
                ws=await other.get(Workspace,case.workspace_id)
                await storage_admission.lock_owner(other,ws.user_id,wait=True)
                ready.set()
                await asyncio.sleep(.5)
                await other.rollback()
        task=asyncio.create_task(compete())
        try:
            await asyncio.wait_for(ready.wait(),10)
            token=short_settlement.set(expire)
            try:return await original_finish(db,case,*args,**kwargs)
            finally:short_settlement.reset(token)
        finally:
            await task
    return await original_finish(db,case,*args,**kwargs)
file_transactions.finish_committed_edit=finish
'''


def scenario(api, *, store=None, **_):
    base = api.prefix
    assert api.call("POST",base+"/index:rebuild",{})["ok"]
    def paths(q):
        result = api.call("GET",base+"/search?q="+q)
        assert result["ready"]
        return [hit["path"] for hit in result["hits"]]
    key = "Ref/edited.txt"
    old = "edit"+uuid.uuid4().hex
    flag = api.root/"pause-index-delivery"
    flag.touch()
    wait_for((api.root/"index-paused").exists,"Dispatcher was not paused")
    try:
        if store is None:
            (api.root/"contend-after-save").touch()
            reply = api.call("PUT",base+"/file",{"path":key,"content":old},expect=(200,409))
            current = api.call("GET",base+"/raw?path="+key,raw=True).decode()
            (api.root/"post-commit.json").write_text(json.dumps({"reply":reply,"saved_bytes_match":current==old}))
            assert reply.get("ok") and current==old, reply
            (api.root/"contend-after-save").write_text("expire")
            pending = api.call("PUT",base+"/file",{"path":"Ref/pending.txt","content":"settled"},expect=409)["detail"]
            assert pending["code"]=="file_cleanup_pending" and pending["committed"] and pending["case_id"]==api.case_id
            assert api.call("GET",base+"/raw?path=Ref/pending.txt",raw=True) == b"settled"
            (api.root/"settlement-timeout.json").write_text(json.dumps(pending))
        else:
            put(api,key,old)
        assert paths(old) == [key]
        stranger = api.call("POST","/auth/register",{"name":"Separate search workspace",
            "email":uuid.uuid4().hex+"@example.com","password":"isolated-test-123"})["token"]
        headers = {"Authorization":"Bearer "+stranger}
        api.call("GET",base+"/search?q="+old,expect=404,extra_headers=headers)
        api.call("PUT",base+"/file",{"path":key,"content":"other"},expect=404,extra_headers=headers)
        api.call("DELETE",base+"/file?path="+key,expect=404,extra_headers=headers)
        def replace(value):
            deadline = time.monotonic()+30
            while True:
                response = api.call("PUT",base+"/file",{"path":key,"content":value,
                    "expected_sha256":hashlib.sha256(old.encode()).hexdigest()},expect=(200,409))
                if response.get("detail",{}).get("code") != "storage_quota_busy":
                    return value,response
                if time.monotonic()>=deadline:
                    raise TimeoutError("File writer admission did not drain")
                time.sleep(.05)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(replace,["edit"+uuid.uuid4().hex for _ in range(2)]))
        assert sum(bool(r.get("ok")) for _,r in results)==1, results
        assert sum(r.get("detail",{}).get("code")=="file_conflict" for _,r in results)==1, results
        new, = [v for v,r in results if r.get("ok")]
        rejected, = [v for v,r in results if r.get("detail",{}).get("code")=="file_conflict"]
        assert paths(new) == [key] and not paths(old) and not paths(rejected)
        denied = api.call("DELETE",base+"/file?path="+key+"&expected_sha256="+hashlib.sha256(old.encode()).hexdigest(),expect=409)
        assert denied["detail"]["code"]=="file_conflict" and paths(new) == [key]
        api.call("DELETE",base+"/file?path="+key+"&expected_sha256="+hashlib.sha256(new.encode()).hexdigest())
        assert not paths(new)

        if store is not None:
            from app.services.s3_live_store import HEAD

            suffix = f'/{api.case_id}/{HEAD}'
            pages = store.raw_client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket)
            prefix, = (row['Key'].removesuffix('/'+HEAD) for page in pages
                       for row in page.get('Contents', []) if row['Key'].endswith(suffix))
            path = 'Ref/fast.txt'
            snapshot = store.client.snapshot(store.bucket, prefix)
            first, second = ('version'+uuid.uuid4().hex for _ in range(2))

            def freeze(content):
                return store.client._freeze(store.bucket, prefix, path, content.encode(), {})

            # LastModified comes from the immutable bodies. Prepare them in the
            # same second and publish each with the normal manifest CAS so the
            # actual HTTP search can finish between the two publications.
            with ThreadPoolExecutor(max_workers=2) as pool:
                for _ in range(5):
                    time.sleep(1-time.time()%1+.02)
                    before, after = pool.map(freeze, (first, second))
                    if before['modified'] == after['modified']:
                        break
                else:
                    raise AssertionError('Did not obtain two immutable versions with identical LastModified')

            value, etag = store.client._change(store.bucket, prefix, snapshot.value, snapshot.etag,
                                               path, snapshot.value['files'].get(path), before)
            args = {'Bucket': store.bucket, 'Key': prefix+'/'+path}
            first_info = store.client.head_object(**args)
            assert paths(first) == [path]
            store.client._change(store.bucket, prefix, value, etag, path, before, after)
            second_info = store.client.head_object(**args)
            assert first_info['LastModified'] == second_info['LastModified']
            assert first_info['ContentLength'] == second_info['ContentLength'] == len(first)
            assert first_info['ETag'] != second_info['ETag']
            hits = paths(second)
            (api.root/'same-second.json').write_text(json.dumps({'path': path, 'hits': hits, 'size': len(first),
                'modified': first_info['LastModified'].isoformat(), 'old_etag': first_info['ETag'],
                'new_etag': second_info['ETag']}))
            assert hits == [path] and not paths(first), 'New version missing at identical size and LastModified'
    finally:
        flag.unlink(missing_ok=True)

    public = "Ref/converted.txt"
    script = "from pathlib import Path\nimport uuid\nPath("+repr(public)+").write_text('result'+uuid.uuid4().hex)\n"
    put(api,"Ref/convert.py",script)
    def run(kind):
        headers = {"Idempotency-Key":uuid.uuid4().hex}
        def admitted():
            response = api.call("POST",base+"/jobs",{"runtime":"windows","type":kind,"command":"python Ref/convert.py"},
                                expect=(200,202,409),extra_headers=headers)
            if response.get("detail",{}).get("code")=="storage_quota_busy":return None
            assert "id" in response,response
            return response
        j = wait_for(admitted,"Job admission did not drain",45)
        return api.wait_job(j["id"])
    def raw(path):
        return api.call("GET",base+"/raw?path="+path,raw=True).decode()
    private_job = run("shell")
    private = raw(private_job["attempt"]["workspace"]["run_path"]+"/"+public)
    assert not paths(private)
    job = run("convert")
    published = raw(public)
    assert published != private and job["attempt"]["outputs"]["publication"]["status"] == "committed"
    index = Path(api.env["VIBEFLOW_INDEX_ROOT"])/f"case-{api.case_id}.sqlite"
    def indexed_without_search():
        with closing(sqlite3.connect(index.as_uri()+"?mode=ro",uri=True)) as conn:
            return conn.execute("SELECT path FROM file_fts WHERE file_fts MATCH ?",(published,)).fetchall()==[(public,)]
    wait_for(indexed_without_search,"Published job output did not reach FTS without a search request",90)
    assert paths(published)==[public] and not paths(private)
    report={"backend":"s3" if store else "fs","read_without_dispatch":True,"without_websocket":True,
            "same_version_winners":1,"cross_user_file_and_index_refused":True,
            "private_job":private_job["id"],"published_job":job["id"],"current_path":public,
            "published_sha256":hashlib.sha256(published.encode()).hexdigest(),"indexed_before_search":True}
    (api.root/"search-updates.json").write_text(json.dumps(report,indent=2),encoding="utf8")
    return report


if __name__ == "__main__":
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
