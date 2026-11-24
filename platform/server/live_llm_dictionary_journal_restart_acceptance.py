"""Recover a three-file caseDict save after real API death before PostgreSQL commit."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import uuid

import httpx

from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_job_repeat_acceptance import main, write
from live_llm_outbox_restart_acceptance import restart


FIXTURE = '''
import asyncio,hashlib,os,uuid
from fastapi import Depends
from sqlalchemy import select
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.models import DurableEvent
from app.routers.workspaces import get_owned_case
from app.services import case_dict_edits, file_transactions as journal,s3_file_transactions as remote
from app.services.case_events import CASE_EVENTS_PATH
from app.services.provenance import PROVENANCE_PATH
from app.services.casedict import CASE_DICT_PATH

original=case_dict_edits.Draft.mirror
async def pause_after_dict(draft,db,case,*args,**kwargs):
    result=await original(draft,db,case,*args,**kwargs)
    if (root/'interrupt-dict').exists():
        marker=root/'dict-uncommitted.tmp'
        marker.write_text(json.dumps({'pid':os.getpid(),'revision':case.dict_revision,'mirror':case.case_meta}))
        marker.replace(root/'dict-uncommitted.json')
        while (root/'interrupt-dict').exists():await asyncio.sleep(.05)
    return result
case_dict_edits.Draft.mirror=pause_after_dict

@app.get('/api/v1/cases/{case_id}/testing/file-commit')
async def observe(case_id:int,tx:str='',user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db,write=False)
    paths=(CASE_DICT_PATH,CASE_EVENTS_PATH,PROVENANCE_PATH)
    def snapshot():
        def read(path):return storage.get_bytes(case.s3_prefix,path)
        hashes={p:hashlib.sha256(read(p)).hexdigest() for p in paths}
        if storage._use_s3():
            pending,_=remote._load(case,verify_blobs=False)
            receipt=(remote._directory(str(uuid.UUID(tx)))+'/resolution.json') if tx else None
        else:
            folder=journal._base(case)/'pending'
            pending=journal._load(case,folder) if folder.exists() else None
            receipt=(journal.PREFIX+'/history/'+str(uuid.UUID(tx))+'/resolution.json') if tx else None
        return {'files':hashes,'pending':{'id':pending['plan']['id'],'event':pending['event_id'],
                'digest':pending['plan_sha256']} if pending else None,
                'receipt':json.loads(read(receipt)) if receipt and storage.exists(case.s3_prefix,receipt) else None}
    result=await asyncio.to_thread(snapshot)
    commits=await db.scalars(select(DurableEvent.id).where(DurableEvent.case_id==case_id,DurableEvent.type==journal.EVENT)
                             .order_by(DurableEvent.seq))
    return {**result,'prefix':case.s3_prefix,'revision':case.dict_revision,'mirror':case.case_meta,'commits':list(commits)}
'''


def scenario(api, *, store=None, **_):
    route = api.prefix+"/caseDict"
    probe = api.prefix+"/testing/file-commit"
    # Seed a committed revision so all three files already have real history.
    initial = api.call("GET",route)
    write(api,"PUT",route,{"content":initial["content"]+"\n// initial revision\n",
                           "expected_revision":initial["dict_revision"]})
    seeded = api.call("GET",route)
    before = api.call("GET",probe)
    first,second = ["journal"+uuid.uuid4().hex for _ in range(2)]
    source = seeded["content"].replace("caseMeta\n{","caseMeta\n{\n    caseName "+first+";",1)
    assert first in source and first not in before["mirror"]
    target = "0-caseDict/caseDict"
    if store:
        store.client.put_object(Bucket=store.bucket,Key=before["prefix"]+"/"+target,Body=source.encode())
    else:
        path = Path(api.env["VIBEFLOW_STORAGE_FS_ROOT"])/before["prefix"]/target
        path.write_bytes(source.encode())
    external = api.call("GET",route)
    assert external["parsed"]["caseMeta"]["caseName"] == first
    baseline = api.call("GET",probe)
    assert baseline["revision"] == seeded["dict_revision"] and baseline["mirror"] == before["mirror"]
    content = source.replace(first,second)
    request = {"content":content,"expected_revision":baseline["revision"],"expected_sha256":external["content_sha256"]}
    flag = api.root/"interrupt-dict"
    flag.touch()
    def save():
        with httpx.Client(timeout=45,trust_env=False) as client:
            return client.put(api.base+"/api/v1"+route,json=request,headers={"Authorization":"Bearer "+api.token})
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(save)
        try:
            def reached():
                if active.done():
                    raise AssertionError("Save exited before the crash boundary: "+str(active.result().status_code))
                return (api.root/"dict-uncommitted.json").exists()
            wait_for(reached,"Save did not reach its precommit boundary",35)
            staged = json.loads((api.root/"dict-uncommitted.json").read_text())
            half = api.call("GET",probe)
            pending = half["pending"]
            assert pending and pending["event"] not in half["commits"]
            assert staged["revision"] == baseline["revision"]+1 and second in staged["mirror"]
            assert half["revision"] == baseline["revision"] and half["mirror"] == baseline["mirror"]
            assert half["commits"] == baseline["commits"]
            assert all(half["files"][p] != previous for p,previous in baseline["files"].items())
            assert half["files"][target] == hashlib.sha256(content.encode()).hexdigest()
            kill_owned_api(api,staged["pid"])
        finally:
            flag.unlink(missing_ok=True)
        try:
            active.result()
        except httpx.TransportError:
            pass
        else:
            raise AssertionError("Terminated API returned a save response")
    restart(api)
    recovered = api.call("GET",route)
    assert recovered == external
    observed = api.call("GET",probe+"?tx="+pending["id"])
    assert not observed["pending"] and observed["commits"] == baseline["commits"]
    assert observed["files"] == baseline["files"] and observed["mirror"] == baseline["mirror"]
    assert observed["receipt"] == {"decision":"rolled_back","transaction_id":pending["id"],
        "event_id":pending["event"],"plan_sha256":pending["digest"]}
    committed = write(api,"PUT",route,request)
    assert committed["dict_revision"] == baseline["revision"]+1
    fresh = api.call("GET",route)
    assert fresh["content"] == content and fresh["parsed"]["caseMeta"]["caseName"] == second
    end = api.call("GET",probe)
    assert second in end["mirror"] and len(end["commits"]) == len(baseline["commits"])+1
    assert pending["event"] not in end["commits"] and end["pending"] is None
    report = {"backend":"s3" if store else "fs","raw_before":baseline["files"],"raw_uncommitted":half["files"],
              "raw_recovered":observed["files"],"receipt":observed["receipt"],"source_diverged_from_sql":True,
              "committed_revision_before":baseline["revision"],"retry_revision":end["revision"]}
    (api.root/"dictionary-crash.json").write_text(json.dumps(report,indent=2),encoding="utf8")
    return report


if __name__ == "__main__":
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
