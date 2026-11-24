"""Kill real APIs before publication and after index commit, then let the lease expire."""
import asyncio
from contextlib import closing
from functools import partial
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_job_repeat_acceptance import main, put
from live_llm_upgrade_rollback_acceptance import readback
from testsupport import SERVER


FIXTURE = '''
import asyncio,os
from fastapi import Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import DurableEvent,EventOutbox
from app.routers.workspaces import get_owned_case
from app.services import durable_events
original_delivery=durable_events.deliver_pending
original_publish=durable_events.publish_to_local_bus

async def delivery(*args,**kwargs):
    if (root/'hold-dispatch').exists():
        (root/'dispatch-paused').touch()
        return 0
    return await original_delivery(*args,**kwargs)

async def publish(event,factory):
    await original_publish(event,factory)
    cut=root/'interrupt-after-publish'
    if cut.exists() and cut.read_text()==event.id:
        (root/'published-before-ack').write_text(str(os.getpid()))
        await asyncio.Event().wait()

durable_events.deliver_pending=delivery
durable_events.publish_to_local_bus=publish

@app.get('/api/v1/cases/{case_id}/testing/outbox-state')
async def outbox_state(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    rows=await db.execute(select(DurableEvent,EventOutbox).join(EventOutbox,EventOutbox.event_id==DurableEvent.id)
                          .where(DurableEvent.case_id==case_id).order_by(DurableEvent.seq))
    return {'pid':os.getpid(),'events':[{'id':e.id,'seq':e.seq,'type':e.type,'payload':json.loads(e.payload_json),
            'status':o.status,'attempts':o.attempts} for e,o in rows]}
'''


def restart(api):
    assert api.proc.poll() is not None
    api.proc = subprocess.Popen([sys.executable, "-B", "-m", "uvicorn", api.app, "--app-dir", str(SERVER),
        "--host", "127.0.0.1", "--port", str(api.port), "--no-access-log"], cwd=api.root, env=api.env,
        stdout=api.log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    def ready():
        assert api.proc.poll() is None, "Restarted API stopped during startup"
        try:
            return api.call("GET", "/api/health", api_prefix=False)["ok"]
        except OSError:
            return False
    wait_for(ready, "Restarted API did not become healthy", 45)


def scenario(api, *, configuration, store=None, **_):
    api.create_case("Outbox delivery across process death")
    marker = "outbox" + uuid.uuid4().hex
    target = "Ref/outbox-target.txt"
    proof = "ROLLBACK_" + uuid.uuid4().hex
    put(api, "Ref/rollback.txt", proof)
    profile = api.call("POST", "/provider-profiles", {"provider":"deepseek", "name":"Outbox usage",
        "base_url":configuration["base_url"], "default_model":configuration["model"], "api_key":configuration["key"]})
    model_run = readback(api, {"case":api.case_id, "profile":profile["id"]}, configuration["model"], proof)
    spawned = api.call("POST", api.prefix + "/jobs", {"type":"shell", "command":"echo OUTBOX_ONCE",
        "runtime":"windows","timeout_seconds":30}, expect=(200, 202), extra_headers={"Idempotency-Key":uuid.uuid4().hex})
    jid = spawned["id"]
    job_route = "/jobs/" + str(jid)
    job = api.wait_job(jid)
    assert job["attempt"]["process_stop"]["verified"]
    assert api.call("POST", api.prefix + "/index:rebuild", {})["ok"]

    def deliveries():
        return api.call("GET", api.prefix + "/testing/outbox-state")
    wait_for(lambda: all(e["status"] == "delivered" for e in deliveries()["events"]), "Setup events still pending", 120)
    (api.root / "hold-dispatch").touch()
    wait_for((api.root / "dispatch-paused").exists, "Dispatcher did not reach the pre-claim boundary")
    cursor = api.call("GET", api.prefix + "/workflow-events")["watermark"]
    put(api, target, marker)
    committed = deliveries()
    event, = [e for e in committed["events"] if e["type"]=="files.changed" and target in e["payload"]["paths"]]
    assert (event["status"], event["attempts"]) == ("pending", 0)

    def projection():
        path = Path(api.env["VIBEFLOW_INDEX_ROOT"]) / f"case-{api.case_id}.sqlite"
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as conn:
            return {"receipt":conn.execute("SELECT digest,consumed_at FROM consumed_events WHERE event_id=?", (event["id"],)).fetchall(),
                    "hits":conn.execute("SELECT rowid,path FROM file_fts WHERE file_fts MATCH ?", (marker,)).fetchall(),
                    "build":conn.execute("SELECT value FROM meta WHERE key='building_uuid'").fetchone()[0]}

    def business():
        org = api.call("GET", "/org/mine")
        state = api.call("GET", job_route)
        assert state["status"] == "succeeded" and state["attempt"]["process_stop"]["verified"]
        log = api.call("GET", job_route + "/log?offset=0")["chunk"]
        assert "OUTBOX_ONCE" in log
        page = api.call("GET", api.prefix + f"/workflow-events?after={cursor}")
        assert not page["blocked"] and not page["has_more"]
        return {"events":page["events"],"job_ids":[j["id"] for j in api.call("GET", api.prefix + "/jobs")],
                "attempt":state["attempt"]["id"],"log_sha256":hashlib.sha256(log.encode()).hexdigest(),
                "usage":org["usage"],"billing":api.call("GET", f'/org/{org["org"]["id"]}/billing'),
                "content":api.call("GET", api.prefix + "/raw?path=" + target, raw=True).decode(),
                "conversation":api.call("GET", f'/conversations/{model_run["conversation"]}')}

    def inventory():
        if store is None:
            return
        pages = store.raw_client.get_paginator("list_objects_v2").paginate(Bucket=store.bucket)
        return {v["Key"]:(v["Size"],v["ETag"]) for page in pages for v in page.get("Contents", [])}

    before = business()
    initial = projection()
    assert not initial["receipt"] and not initial["hits"]
    assert before["content"] == marker and before["usage"]["llm_msgs_today"] > 0 and before["usage"]["running_jobs"] == 0
    assert before["job_ids"] == [jid]
    kill_owned_api(api, committed["pid"])
    (api.root / "hold-dispatch").unlink()
    cut = api.root / "interrupt-after-publish"
    cut.write_text(event["id"])
    restart(api)
    witness = api.root / "published-before-ack"
    wait_for(witness.exists, "First actual publication did not commit its FTS receipt", 90)
    first_usage = api.call("GET", "/org/mine")["usage"]
    first_inventory = inventory()
    claimed, = [e for e in deliveries()["events"] if e["id"] == event["id"]]
    assert claimed == {**event, "status":"delivering", "attempts":1}
    kill_owned_api(api, int(witness.read_text()))
    applied = projection()
    assert len(applied["receipt"]) == len(applied["hits"]) == 1 and applied["hits"][0][1] == target
    assert initial["build"] != applied["build"]
    cut.unlink()
    restart(api)
    def acknowledged():
        latest = next(e for e in deliveries()["events"] if e["id"] == event["id"])
        return latest if latest["status"] == "delivered" else None
    final = wait_for(acknowledged, "Production dispatcher did not reclaim its expired lease", 100)
    assert final == {**event,"status":"delivered","attempts":2}, final
    assert projection() == applied
    assert inventory() == first_inventory
    after = business()
    usage = {"before_publication":before["usage"],"after_publication":first_usage,"after_replay":after["usage"]}
    (api.root / "usage-comparison.json").write_text(json.dumps(usage,indent=2),encoding="utf8")
    # Initial S3 reads/publication legitimately write capacity/fence records.
    # The duplicate must leave even those physical bytes and ETags unchanged.
    assert after["usage"] == first_usage
    for key in before["usage"].keys() - {"storage_bytes_used","storage_mb_used"}:
        assert before["usage"][key] == first_usage[key], key
    assert after == {**before,"usage":first_usage}, [key for key in before if before[key] != after[key]]
    hits = api.call("GET", api.prefix + "/search?q=" + marker)["hits"]
    assert [h["path"] for h in hits] == [target]
    report = {"backend":"s3" if store else "fs", "event":event["id"], "seq":event["seq"], "claims":final["attempts"],
              "job_id":jid, "attempt_id":before["attempt"], "job_log_sha256":before["log_sha256"],
              "llm_messages":before["usage"]["llm_msgs_today"], "model":model_run, "index":applied,
              "identical_billing_and_usage":True, "identical_committed_events":True, "identical_conversation":True}
    (api.root / "outbox-restart.json").write_text(json.dumps(report,indent=2),encoding="utf8")
    api.create_case("Independent outbox result readback")
    return report


if __name__ == "__main__":
    asyncio.run(main(selected_factory=lambda config:partial(scenario,configuration=config), fixture_override=FIXTURE))
