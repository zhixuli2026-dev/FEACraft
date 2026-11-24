"""HTTP duplicate submissions and replay after a real disposable API restart."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import subprocess
import sys
import time

from testsupport import IsolatedServer, SERVER


def main():
    with IsolatedServer("job-idempotency-api") as api:
        case = api.create_case("idempotency")
        first_token, first_prefix = api.token, api.prefix
        body = {"command":"echo IDEMPOTENCY_ONCE"}
        headers = {"Idempotency-Key":"client-request-1"}
        def submit():
            return api.call("POST", first_prefix+"/jobs", body, extra_headers=headers)
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = list(pool.map(lambda _: submit(), range(6)))
        assert len({job["id"] for job in jobs}) == 1
        job_id = jobs[0]["id"]
        ended = api.wait_job(job_id)
        assert ended["attempt"]["number"] == 1 and ended["attempt"]["phase"] == "succeeded"
        changed = api.call("POST", first_prefix+"/jobs", {"command":"echo CHANGED"},
            extra_headers=headers, expect=409)
        assert changed["detail"]["code"] == "idempotency_conflict"
        for key in ("", "bad key", "x"*129):
            api.call("POST", first_prefix+"/jobs", body, extra_headers={"Idempotency-Key":key}, expect=400)
        with sqlite3.connect(api.root / "test.sqlite") as db:
            assert db.execute("SELECT count(*) FROM jobs WHERE case_id=?", (case["id"],)).fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM job_attempts WHERE job_id=?", (job_id,)).fetchone()[0] == 1
            phases = [json.loads(row[0])["status"] for row in db.execute(
                "SELECT payload_json FROM durable_events WHERE type='attempt.state_changed' ORDER BY seq")]
            assert phases == ["claimed", "starting", "running", "finalizing", "succeeded"], phases
        original_log = api.call("GET", f"/jobs/{job_id}/log")["chunk"]
        assert original_log.count("IDEMPOTENCY_ONCE") == 1
        api.create_case("other-owner")
        api.call("POST", first_prefix+"/jobs", body, extra_headers=headers, expect=404)
        other_job = api.call("POST", api.prefix+"/jobs", body, extra_headers=headers)
        assert other_job["id"] != job_id
        api.wait_job(other_job["id"])
        api.token, api.prefix = first_token, first_prefix

        api.proc.terminate()
        api.proc.wait(timeout=15)
        api.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(SERVER),
            "--host", "127.0.0.1", "--port", str(api.port), "--no-access-log"], cwd=api.root,
            env=api.env, stdout=api.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for _ in range(150):
            assert api.proc.poll() is None
            try:
                if api.call("GET", "/api/health", api_prefix=False).get("ok"):
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            raise TimeoutError("Restarted API not ready")
        replay = submit()
        assert replay["id"] == job_id and replay["status"] == "succeeded"
        restored = api.call("GET", f"/jobs/{job_id}")
        assert restored["phase"] == "succeeded" and restored["attempt"] == ended["attempt"]
        assert restored["ended_at"] == ended["ended_at"] or abs(restored["ended_at"]-ended["ended_at"]) < 1
        assert api.call("GET", f"/jobs/{job_id}/log")["chunk"] == original_log
        api.call("POST", first_prefix+"/jobs", {"command":"echo CHANGED"}, extra_headers=headers, expect=409)
        (api.root / "idempotency-evidence.json").write_text(json.dumps({"passed":True,
            "concurrent_http_requests":6, "job_count":1, "attempt_count":1,
            "payload_conflict":True, "authorization_before_lookup":True, "scope_isolation":True,
            "restart_replay_same_job":True, "history_phase_persisted":True,
            "log_unchanged":True, "phases":phases}, indent=2), encoding="utf-8")
    print("JOB_IDEMPOTENCY_API_OK "+str(api.root))


if __name__ == "__main__":
    main()
