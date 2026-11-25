"""Run real geometry-to-report jobs and check the published OpenFOAM source build."""

import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from app.services.casedict import set_block
from cae_benchmarks import thermal_files
from cae_fixtures import cavity_2d
from registered_cae_benchmarks import thermal_errors
from testsupport import IsolatedServer


def verify_case(api, solver, expected):
    thermal = solver == "laplacianFoam"
    run = "C-run/conduction" if thermal else "C-run/case-1"
    inputs = thermal_files() if thermal else cavity_2d()
    if thermal:
        mesh = f"{run}/system/blockMeshDict"
        inputs[mesh] = inputs[mesh].replace("(20 2 2)", "(10 4 2)")
    end_time = 1 if thermal else .5
    field = "T" if thermal else "p"
    lx, ly, lz = (1, 1, 1) if thermal else (.1, .1, .01)
    current = api.call("GET", api.prefix + "/caseDict")
    content = current["content"]
    blocks = {
        "caseMeta": {"discipline": "thermal" if thermal else "fluid",
                     "analysisType": "steady" if thermal else "transient",
                     "dimensions": "3D" if thermal else "2D", "units": {"length": "m", "temperature": "K"}},
        "geometry": {"generator": "cadquery", "parameters": {"lx": lx, "ly": ly, "lz": lz}},
        "mesh": {"source": "blockMesh", "qualityTargets": {
            "policy": "fail", "maxNonOrtho": 70, "maxSkewness": 4, "maxAspectRatio": 100}},
        "numericalMethod": {"solver": solver},
        "postProcessing": {"runDir": run, "fields": {field: {"unit": "K" if thermal else '"m^2/s^2"'}},
                           "dataExtraction": {"volumeAverages": {"mean": {"field": field, "time": end_time}}}},
    }
    for key, value in blocks.items():
        content = set_block(content, key, value)
    api.call("PUT", api.prefix + "/caseDict", {"content": content, "expected_revision": current["dict_revision"]})
    for name, data in inputs.items():
        api.put(name, data)
    preview = api.put("A-geo/geometry.py", """import cadquery as cq
def build_geometry(ctx):
    p = ctx.data['geometry']['parameters']
    return cq.Workplane('XY').box(float(p['lx']), float(p['ly']), float(p['lz']), centered=(False, False, False))
""")
    if preview.get("auto_preview_job"):
        api.wait_job(preview["auto_preview_job"])
    flow = api.call("POST", api.prefix + "/workflow-runs", {
        "target": "report", "run_dir": run,
        "expected_revision": api.call("GET", api.prefix + "/caseDict")["dict_revision"],
        "budget": {"timeout_seconds": 300, "max_log_bytes": 16777216},
    }, extra_headers={"Idempotency-Key": str(uuid4())})
    url = "/workflow-runs/" + flow["id"]
    case = next((api.root / "files" / "workspaces").glob("*/cases/" + str(api.case_id)))
    confirmations = []
    deadline = time.monotonic() + 300
    while flow["status"] != "succeeded":
        if flow["status"] in {"failed", "cancelled", "interrupted"}:
            raise AssertionError(json.dumps(flow))
        if time.monotonic() >= deadline:
            api.call("POST", url + ":cancel", {"expected_version": flow["version"]})
            raise TimeoutError("Source solver workflow timed out")
        for step in flow["steps"]:
            if step["status"] == "awaiting_confirmation":
                stage = step["confirmation_state"]
                if stage == "D" and not (case / run / "solve-toolchain.json").is_file():
                    # Publication commits before the local working files are mirrored.
                    break
                api.call("POST", api.prefix + "/caseDict:advance", {
                    "state": stage, "confirmed": True, "expected_revision": step["confirmation_revision"],
                    "workflow_step_id": step["id"], "expected_step_version": step["version"],
                })
                confirmations.append(stage)
                break
        time.sleep(.2)
        flow = api.call("GET", url)

    assert confirmations == ["B", "C", "D", "E"], confirmations
    jobs = {}
    for step in flow["steps"]:
        if step["attempts"]:
            jobs[step["key"]] = api.wait_job(step["attempts"][-1]["job_id"])
    assert set(jobs) == {"geometry", "mesh", "solve", "postprocess", "report"}
    assert all(job["attempt"]["process_stop"]["verified"] for job in jobs.values())
    work = case / jobs["solve"]["attempt"]["workspace"]["run_path"]
    result = api.call("GET", api.prefix + "/solve/results")
    toolchain = result["toolchain"]
    assert toolchain["executable"].endswith("/" + expected["library"])
    assert toolchain["executable_sha256"] == expected["library_sha256"]
    assert all(toolchain["source_build"][key] == value for key, value in expected.items())
    summary = result["evidence"]["parsed_log"]["summary"]
    assert summary["finished_cleanly"] and summary["last_time"] == end_time
    if thermal:
        l2, max_error = thermal_errors(work, run)
        assert l2 < 1e-6 and max_error < 1e-6
        numerical = {"temperature_l2_K": l2, "temperature_max_error_K": max_error}
    else:
        fields = [row["path"] for row in result["evidence"]["outputs"] if row["path"].endswith("/U")]
        assert len(fields) == 5 and summary["time_points"] == 100
        numerical = {"velocity_output_times": fields, "time_points": summary["time_points"]}
    report = api.call("GET", api.prefix + "/report/results")
    publication = report["publication"]
    assert not publication["stale"]
    return {"workflow_id": flow["id"], "confirmations": confirmations, "solve_job": jobs["solve"]["id"],
            "report_publication": publication["id"], "toolchain": toolchain, "numerical": numerical}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-dir", required=True, help="Absolute library path on the solver host")
    parser.add_argument("--manifest", type=Path, required=True, help="Local copy of the build manifest")
    parser.add_argument("solver", choices=("icoFoam", "laplacianFoam"))
    args = parser.parse_args()
    records = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected = next(record for record in records if record["solver"] == args.solver)
    os.environ["VIBEFLOW_OPENFOAM_SOURCE_LIBRARY_DIR"] = args.library_dir
    with IsolatedServer("openfoam-source-" + args.solver.lower()) as api:
        api.create_case("OpenFOAM source workflow")
        result = verify_case(api, args.solver, expected)
        (api.root / "source-workflow.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print("OPENFOAM_SOURCE_WORKFLOW_OK", api.root, flush=True)


if __name__ == "__main__":
    main()
