"""Imported historical state may regress on failure, but cannot manufacture new evidence."""
import io
import json
import sys
import time
import urllib.request
import zipfile

from app.services.casedict import CASE_DICT_TEMPLATE, set_block, set_workflow_state
from testsupport import IsolatedServer


def main():
    with IsolatedServer("workflow-fallback") as api:
        api.create_case("fallback-owner")
        workspace = api.call("GET", "/workspaces")[0]
        historical = set_workflow_state(CASE_DICT_TEMPLATE, "D")
        historical = set_block(historical, "errors", {"summary": {"open": 1, "blocking": 0}})
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("0-caseDict/caseDict", historical)
        boundary = "vibeflow-fallback-fixture"
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                   'filename="historical.zip"\r\nContent-Type: application/zip\r\n\r\n').encode()
        payload += archive.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(api.base + f"/api/v1/workspaces/{workspace['id']}/cases:import",
            data=payload, headers={"Authorization": "Bearer " + api.token,
                                   "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with api.opener.open(request, timeout=30) as response:
            imported = json.load(response)
        api.prefix = f"/cases/{imported['id']}"
        before = api.call("GET", api.prefix)
        assert before["workflow_state"] == "D"
        api.advance("E", expect=409)
        job = api.call("POST", api.prefix + "/jobs", {
            "type": "solve", "command": f'"{sys.executable}" -c "raise SystemExit(7)"',
            "runtime": "windows"})
        api.wait_job(job["id"], expected="failed")
        for _ in range(50):
            after = api.call("GET", api.prefix)
            if after["workflow_state"] == "C":
                break
            time.sleep(0.1)
        assert after["workflow_state"] == "C" and after["dict_revision"] == before["dict_revision"] + 1
        events = api.call("GET", api.prefix + "/events")["events"]
        fallback = next(event for event in events if event["type"] == "workflow.fallback")
        assert fallback["previous_state"] == "D" and fallback["workflow_state"] == "C"
        assert fallback["job_id"] == job["id"] and fallback["regressed"] is True
        assert any(event.get("source") == "job-failure-fallback" for event in events)
        report = api.call("POST", api.prefix + "/jobs", {
            "type": "convert", "command": f'"{sys.executable}" -c "raise SystemExit(9)"',
            "runtime": "windows"})
        api.wait_job(report["id"], expected="failed")
        assert api.call("GET", api.prefix)["workflow_state"] == "C"
        print("WORKFLOW_FALLBACK_OK: imported D -> failed solve -> C; convert cannot advance")


if __name__ == "__main__":
    main()
