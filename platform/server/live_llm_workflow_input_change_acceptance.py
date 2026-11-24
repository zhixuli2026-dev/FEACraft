"""Exercise the real confirmation endpoint after a completed producer's inputs change."""
import asyncio
import hashlib
import json
from urllib.parse import quote

from app.services.casedict import set_block
from cae_fixtures import cavity_2d
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put, write
from live_llm_mpi_workflow_acceptance import FIXTURE, vectors


GEOMETRY = """from pathlib import Path
import cadquery as cq

def build_geometry(ctx):
    width = float(Path(__file__).with_name("width.txt").read_text())
    return {"box": cq.Workplane("XY").box(width, 1, 1)}
"""


def scenario(api, *, store=None, **_):
    run_dir = "C-run/case-1"
    doc = api.prefix + "/caseDict"
    inputs = cavity_2d()
    saved = api.call("GET", doc)
    text = set_block(saved["content"], "mesh", {"source": "blockMesh", "qualityTargets": {
        "maxNonOrtho": 70, "maxSkewness": 4, "maxAspectRatio": 100, "policy": "fail"}})
    text = set_block(text, "numericalMethod", {"solver": "icoFoam"})
    write(api, "PUT", doc, {"content": text, "expected_revision": saved["dict_revision"]})
    for path, body in inputs.items():
        put(api, path, body)
    width = "A-geo/width.txt"
    put(api, width, "1\n")
    preview = put(api, "A-geo/geometry.py", GEOMETRY)
    api.wait_job(preview["auto_preview_job"])
    revision = api.call("GET", doc)["dict_revision"]
    run = api.call("POST", api.prefix + "/workflow-runs", {"target": "solve", "run_dir": run_dir,
        "expected_revision": revision, "parameters": {"geometry_mode": "export"},
        "budget": {"timeout_seconds": 120, "max_log_bytes": 16777216}})
    route = "/workflow-runs/" + run["id"]
    jobs = []

    def waiting(key):
        current = api.call("GET", route)
        assert current["status"] not in {"failed", "interrupted", "cancelled"}, current
        step = next(row for row in current["steps"] if row["key"] == key)
        if step["status"] == "awaiting_confirmation":
            return current, {"state": step["confirmation_state"], "confirmed": True,
                "expected_revision": step["confirmation_revision"], "workflow_step_id": step["id"],
                "expected_step_version": step["version"]}

    for stage, kind in (("B", "geometry"), ("C", "mesh"), ("D", "solve")):
        state, request = wait_for(lambda: waiting("confirm_" + stage), "Waiting for " + kind, 240)
        producer = next(step for step in state["steps"] if step["key"] == kind)
        jid = producer["attempts"][-1]["job_id"]
        job = api.wait_job(jid)
        wait_for(lambda: api.call("GET", "/testing/workflow-job-drain/" + str(jid))["verified"],
                 "Waiting for producer write drain", 120)
        assert job["attempt"]["process_stop"]["verified"] and job["log_backlog"]
        jobs.append({"kind": kind, "job": jid, "publication": job["attempt"]["outputs"]["publication"]["id"]})
        if stage != "D":
            write(api, "POST", doc + ":advance", request)

    def graph():
        result = api.call("GET", api.prefix + "/dependencies?run_dir=" + quote(run_dir))
        assert not result["needs_inspection"], result
        return {item["kind"]: (item["publication_id"], item["status"]) for item in result["channels"]}

    def raw(path):
        return api.call("GET", api.prefix + "/raw?path=" + quote(path), raw=True)

    baseline = graph()
    for job in jobs:
        assert baseline[job["kind"]] == (job["publication"], "fresh"), baseline
    field_path = run_dir + "/0.5/U"
    field = raw(field_path)
    assert len(vectors(field)) == 400
    before_jobs = [job["id"] for job in api.call("GET", api.prefix + "/jobs")]
    observations = []
    for path, old, new, stale in (
        (run_dir + "/system/blockMeshDict", "(20 20 1)", "(24 20 1)", {"mesh", "solve"}),
        (run_dir + "/constant/transportProperties", "0.01;", "0.02;", {"mesh", "solve"}),
        (width, "1\n", "2\n", {"geometry", "mesh", "solve"}),
    ):
        original = raw(path).decode()
        assert old in original
        put(api, path, original.replace(old, new))
        changed = graph()
        for job in jobs:
            kind = job["kind"]
            assert changed[kind] == (job["publication"], "stale" if kind in stale else "fresh"), changed
        events = api.prefix + "/workflow-events"
        cursor = api.call("GET", events)["watermark"]
        rejected = api.call("POST", doc + ":advance", request, expect=409)
        assert rejected["detail"]["code"] == "input_stale", rejected
        direct = api.call("POST", doc + ":advance", {"state": "D", "confirmed": True,
                          "expected_revision": request["expected_revision"]}, expect=409)
        assert isinstance(direct["detail"], str) and direct["detail"], direct
        assert api.call("GET", route) == state
        current_doc = api.call("GET", doc)
        assert current_doc["workflow_state"] == "C" and current_doc["dict_revision"] == request["expected_revision"]
        assert [job["id"] for job in api.call("GET", api.prefix + "/jobs")] == before_jobs
        assert raw(field_path) == field
        page = api.call("GET", events + f"?after={cursor}")
        assert not page["events"] and not page["has_more"] and not page["blocked"], page
        observations.append({"edited": path, "rejected": rejected["detail"], "direct_rejection": direct["detail"], "stale": sorted(stale),
                             "stage": current_doc["workflow_state"], "new_events": 0})
        put(api, path, original)
        assert graph() == baseline
    write(api, "POST", doc + ":advance", request)
    assert api.call("GET", route)["status"] == "succeeded"
    proof = {"backend": "s3" if store else "fs", "run_id": run["id"], "producers": jobs,
             "field_sha256": hashlib.sha256(field).hexdigest(), "observations": observations,
             "restored_inputs_confirmed": True}
    (api.root / "input-change.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf8")
    return proof


if __name__ == "__main__":
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE, store_volume_max=12))
