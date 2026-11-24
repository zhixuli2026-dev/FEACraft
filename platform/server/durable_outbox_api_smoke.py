"""Kill/restart only a disposable API; committed Job events must be replayed."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from testsupport import IsolatedServer, SERVER

APP = '''from pathlib import Path
from fault_fixture import FileBarrier
from app.services import durable_events
original = durable_events.dispatch_forever
async def controlled(factory, stop, **kwargs):
    await FileBarrier(Path(__file__).parent, "dispatch", timeout=60).wait_async()
    await original(factory, stop, **kwargs)
durable_events.dispatch_forever = controlled
from app.main import app
'''


def main():
    api = IsolatedServer("outbox-api-restart", app="controlled_outbox_api:app")
    (api.root / "controlled_outbox_api.py").write_text(APP, encoding="utf-8")
    def rows():
        with sqlite3.connect(api.root / "test.sqlite") as db:
            return db.execute("SELECT e.id,e.seq,e.payload_json,o.status FROM durable_events e "
                              "JOIN event_outbox o ON o.event_id=e.id "
                              "WHERE e.type='jobs.updated' ORDER BY e.seq").fetchall()

    with api:
        api.create_case("outbox-restart")
        job = api.call("POST", api.prefix+"/jobs", {"command":"echo DURABLE_RESTART_OK"})
        state = api.wait_job(job["id"])
        assert state["status"] == "succeeded"
        before = rows()
        assert len(before) == 2 and all(row[3] == "pending" for row in before), before
        assert [json.loads(row[2])["status"] for row in before] == ["running", "succeeded"]
        for _ in range(100):
            if (api.root / "dispatch.ready").exists():
                break
            time.sleep(0.01)
        assert (api.root / "dispatch.ready").exists() and api.proc.poll() is None
        # Job native process has finished. Kill this owned API through its live
        # Popen handle; Windows taskkill depends on unrelated process-enumeration
        # permissions and is not needed for this outbox crash window.
        api.proc.kill()
        api.proc.wait(timeout=10)
        stopped = False
        for _ in range(100):
            try:
                api.call("GET", "/api/health", api_prefix=False)
            except OSError:
                stopped = True
                break
            time.sleep(0.02)
        assert stopped, "Owned API listener survived process kill"
        # Same DB/files/JWT/port, unmodified production application on restart.
        api.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(SERVER),
            "--host", "127.0.0.1", "--port", str(api.port), "--no-access-log"], cwd=api.root,
            env=api.env, stdout=api.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        after = []
        for _ in range(200):
            assert api.proc.poll() is None, (api.root / "api.log").read_text(encoding="utf-8")
            try:
                api.call("GET", "/api/health", api_prefix=False)
                after = rows()
                if all(row[3] == "delivered" for row in after):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        assert len(after) == 2 and all(row[3] == "delivered" for row in after), after
        assert [row[:3] for row in before] == [row[:3] for row in after]
        restored = api.call("GET", f"/jobs/{job['id']}")
        assert restored["status"] == "succeeded" and restored["ended_at"]
        (api.root / "outbox-evidence.json").write_text(json.dumps({"passed":True,
            "owned_api_killed":True, "pending_before":len(before), "delivered_after":len(after),
            "event_ids_preserved":True, "job_preserved":True}, indent=2), encoding="utf-8")
    print("DURABLE_OUTBOX_API_OK "+str(api.root))


if __name__ == "__main__":
    main()
