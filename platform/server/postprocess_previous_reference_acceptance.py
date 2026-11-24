"""Check version-pinned previous-run comparisons on real icoFoam fields."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
import uuid

from postprocess_array_formula_acceptance import FOAM, POINTS, RUN, UNIT, config
from testsupport import IsolatedServer, SERVER


METRIC = "on_mesh.value"


def options(previous=None, *, offset=False):
    post = config()
    if offset:
        points = [[p[0], p[1] + 0.005, p[2]] for p in POINTS]
        extraction = post["dataExtraction"]
        extraction["pointProbes"]["pressure_probes"]["points"] = [v for p in points for v in p]
        extraction["lineSamples"]["pressure_line"].update(start=points[0], end=points[1])
    if previous:
        post["derivedQuantities"]["referenceValues"] = {
            "archived_pressure": {"metric": METRIC, "unit": json.dumps(UNIT), "l2_max": 0.0,
                "previousRun": {"publicationId": previous, "metric": METRIC}},
        }
        post["objectives"] = {"comparisonTargets": {
            "historical_pressure": {"metric": METRIC, "reference": "archived_pressure", "type": "previousRun"},
        }}
    return post


def verify(foam_case):
    from app.services.casedict import set_block

    api = IsolatedServer("d12-prior-reference")
    report = {"passed": False, "models": 0, "native_jobs": [], "checks": []}
    print("D12_PREVIOUS_EVIDENCE", api.root, flush=True)
    try:
        with api:
            case = api.create_case("Previous pressure reference")
            for relative, digest in FOAM.items():
                body = (foam_case / relative).read_bytes()
                assert sha256(body).hexdigest() == digest, relative
                uploaded = api.put(RUN + "/" + relative, body.decode("utf8"))
                assert uploaded["content_sha256"] == digest

            def write_options(value):
                current = api.call("GET", api.prefix + "/caseDict")
                text = set_block(current["content"], "postProcessing", value)
                api.call("PUT", api.prefix + "/caseDict", {"content": text, "expected_revision": current["dict_revision"]})

            def result(publication):
                return api.call("GET", api.prefix + "/postprocess/results?" + urlencode({"publication_id": publication}))

            def run(label):
                submitted = api.call("POST", api.prefix + "/postprocess:run", {"run_dir": RUN},
                                     extra_headers={"Idempotency-Key": "previous-reference-" + label})
                job = api.wait_job(submitted["job_id"], timeout=100, expected="succeeded")
                stop = job["attempt"]["process_stop"]
                assert stop["verified"] and stop["scope_empty"], stop
                result = api.call("GET", api.prefix + "/postprocess/results?" + urlencode({"job_id": job["id"]}))
                report["native_jobs"].append({"job_id": job["id"], "publication": result["publication"]["id"],
                                              "verified_stop": True})
                return result

            write_options(options())
            first = run("baseline")
            old = first["publication"]["id"]
            value = next(row for row in first["metrics"]["metrics"] if row["name"] == METRIC)
            archived_metrics = first["result_dir"] + "/metrics.json"
            original = api.call("GET", api.prefix + "/raw?" + urlencode({"path": archived_metrics}), raw=True)
            corrupt_alias = '{"fixture":"edited visible alias"}'
            api.put(archived_metrics, corrupt_alias)
            assert api.call("GET", api.prefix + "/raw?" + urlencode({"path": archived_metrics}), raw=True) == corrupt_alias.encode()
            assert result(old)["metrics"] == first["metrics"]

            write_options(options(old))
            second = run("comparison")
            comparison, = second["metrics"]["comparisons"]
            assert comparison["status"] == "pass" and comparison["l2_relative"] == comparison["max_absolute_error"] == 0
            assert comparison["reference"]["value"] == value["value"]
            assert comparison["reference"]["sampling"] == value["sampling"]
            provenance = comparison["reference"]["provenance"]
            assert provenance["publication_id"] == old and provenance["metrics_sha256"] == sha256(original).hexdigest()
            source_inputs = second["metrics"]["source"]["input_publications"]
            binding, = [item for item in source_inputs if item["publication_id"] == old]
            assert binding["manifest_sha256"] == provenance["manifest_sha256"]
            assert binding["require_current"] is False and binding["paths"] == [archived_metrics]
            sealed, = [row for row in second["manifest"]["inputs"] if row["path"] == archived_metrics]
            assert sealed["sha256"] == provenance["metrics_sha256"]
            target, = second["metrics"]["extractions"]["comparisonTargets"]
            assert target["status"] == "pass" and target["evidence_kind"] == "regression"
            assert not second["metrics"]["readiness"]["can_claim_validation"]
            report["checks"].append("compared_against_immutable_version_despite_changed_visible_alias")
            report["reference"] = provenance

            historical = next(row for row in api.call("GET", api.prefix + "/postprocess/publications") if row["id"] == old)
            assert historical["status"] in {"committed", "superseded"} and not historical["current"], historical
            report["historical_status"] = historical["status"]
            write_options(options(old, offset=True))
            mismatch = run("mismatched-axis")
            difference, = mismatch["metrics"]["comparisons"]
            assert difference["status"] == "sampling_mismatch", difference
            assert not mismatch["metrics"]["readiness"]["report_ready"]
            assert result(old)["metrics"] == first["metrics"]
            report["checks"].append("historical_version_readable_and_changed_sampling_is_rejected")

            write_options(options(str(uuid.uuid4())))
            api.call("POST", api.prefix + "/postprocess:run", {"run_dir": RUN}, expect=400,
                     extra_headers={"Idempotency-Key":"previous-reference-missing"})
            report["checks"].append("missing_publication_refused_before_running")
            report["case_id"] = case["id"]
        report["api_exit"] = api.proc.poll()
        assert report["api_exit"] is not None
        report["passed"] = True
    except BaseException as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)[:1000]}
        raise
    finally:
        (api.root / "previous-reference.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    sys.path.insert(0, str(SERVER.parent / "tools"))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--foam-case", type=Path, required=True)
    verify(parser.parse_args().foam_case.resolve(strict=True))
