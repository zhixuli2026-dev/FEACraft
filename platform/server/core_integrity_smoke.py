"""Offline regressions for case input, workflow and report evidence integrity."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.services.casedict import (CASE_DICT_PATH, CASE_DICT_TEMPLATE, WorkflowGateError,
                                   get_workflow_state, parse_casedict, set_workflow_state,
                                   update_case_dict)
from app.services.casedict import WorkflowTransition
from app.services.postprocess_executor import config_from_case_dict, postprocess_fingerprint
from app.services.postprocess_gate import report_gate_reason
from app.services.case_evidence import (case_input_fingerprint, capture_solve_inputs,
                                        complete_solve_evidence)


class CaseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("app.services.case_events.append_case_event"))
        self.enterContext(patch("app.services.provenance.record_case_revision"))

    def case(self, state="A"):
        return SimpleNamespace(s3_prefix="memory-only", workflow_state=state,
                               dict_revision=3, case_meta="{}", parse_status="ok")

    def test_truncated_blocks_are_rejected_before_writing(self):
        for suffix in ("\nunfinished { x 1;", "\nouter { inner { x 1; }"):
            case = self.case()
            with patch("app.storage.put_text") as write:
                with self.assertRaises(ValueError):
                    update_case_dict(case, CASE_DICT_TEMPLATE + suffix)
                write.assert_not_called()
            self.assertEqual((case.workflow_state, case.dict_revision), ("A", 3))

    def test_comments_and_strings_do_not_supply_workflow_state(self):
        text = '// workflowState E;\n' + CASE_DICT_TEMPLATE
        self.assertEqual(get_workflow_state(text), "A")
        changed = set_workflow_state(text, "B")
        self.assertTrue(changed.startswith('// workflowState E;'))
        self.assertEqual(get_workflow_state(changed), "B")
        self.assertEqual(parse_casedict('outer { note "}"; }')["outer"]["note"], "}")

    def test_regular_writes_cannot_transition_even_after_errors_are_cleared(self):
        for target in ("B", "C", "D", "E", "V"):
            with self.subTest(target=target), patch("app.storage.put_text") as write:
                with self.assertRaises(WorkflowGateError):
                    update_case_dict(self.case(), set_workflow_state(CASE_DICT_TEMPLATE, target))
                write.assert_not_called()

    def test_failure_regression_cannot_be_used_to_advance(self):
        with patch("app.storage.put_text") as write:
            with self.assertRaises(WorkflowGateError):
                update_case_dict(self.case(), set_workflow_state(CASE_DICT_TEMPLATE, "E"),
                                 source="job-failure-fallback", allow_failure_regression=True)
            write.assert_not_called()

    def test_invalid_workflow_tokens_are_rejected(self):
        for state in ("", "AB", "ABC", "X"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                set_workflow_state(CASE_DICT_TEMPLATE, state)

    def test_transition_requires_matching_confirmation_revision_and_order(self):
        for proof in (WorkflowTransition("A", "B", 3, False),
                      WorkflowTransition("A", "B", 2, True),
                      WorkflowTransition("A", "E", 3, True)):
            with self.subTest(proof=proof), patch("app.storage.put_text") as write:
                with self.assertRaises(WorkflowGateError):
                    update_case_dict(self.case(), set_workflow_state(CASE_DICT_TEMPLATE, proof.target_state),
                                     transition=proof)
                write.assert_not_called()
        case = self.case()
        with patch("app.storage.put_text"):
            update_case_dict(case, set_workflow_state(CASE_DICT_TEMPLATE, "B"),
                             transition=WorkflowTransition("A", "B", 3, True))
        self.assertEqual((case.workflow_state, case.dict_revision), ("B", 4))

    def test_duplicate_workflow_and_invalid_substring_are_rejected(self):
        for text in (CASE_DICT_TEMPLATE + "\nworkflowState E;",
                     CASE_DICT_TEMPLATE.replace("workflowState A;", "workflowState AB;")):
            with self.subTest(text=text[-30:]), self.assertRaises(ValueError):
                update_case_dict(self.case(), text)


class ReportIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.text = ("workflowState D; caseMeta {} postProcessing { runDir C-run/case-1; "
                     "dataExtraction { pointProbes { center { fields (U); points ((0 0 0)); } } } }")
        config = config_from_case_dict(parse_casedict(self.text))
        self.field = "C-run/case-1/0.5/U"
        self.metrics_path = "D-post/data/postprocess-1/metrics.json"
        self.manifest_path = "D-post/data/postprocess-1/artifact-manifest.json"
        self.files = {
            CASE_DICT_PATH: self.text.encode(), self.field: b"current solver field",
            "D-post/data/latest-postprocess.json": json.dumps({"job_id": 1}).encode(),
            self.metrics_path: json.dumps({"source": {
                "case_input_sha256": case_input_fingerprint(self.text),
                "postprocess_sha256": postprocess_fingerprint(config)},
                "readiness": {"report_ready": True}}).encode(),
        }
        self.set_manifest()

    def set_manifest(self, **overrides):
        record = {"path": self.field, "sha256": hashlib.sha256(self.files[self.field]).hexdigest()}
        manifest = {"inputs": [record], "artifacts": [{"path": self.metrics_path,
            "sha256": hashlib.sha256(self.files[self.metrics_path]).hexdigest()}]}
        manifest.update(overrides)
        self.files[self.manifest_path] = json.dumps(manifest).encode()

    def gate(self):
        with patch("app.storage.get_bytes", side_effect=lambda _prefix, path: self.files[path]), \
             patch("app.storage.exists", side_effect=lambda _prefix, path: path in self.files):
            return report_gate_reason(SimpleNamespace(s3_prefix="memory-only"))

    def test_unchanged_recorded_input_is_accepted(self):
        self.assertIsNone(self.gate())

    def test_changed_solver_field_invalidates_report(self):
        self.files[self.field] = b"new solve with different results"
        self.assertIsNotNone(self.gate())

    def test_deleted_solver_field_invalidates_report(self):
        del self.files[self.field]
        self.assertIsNotNone(self.gate())

    def test_changed_chart_invalidates_report(self):
        chart = "D-post/data/postprocess-1/chart.svg"
        self.files[chart] = b"new chart"
        self.set_manifest(artifacts=[{"path": chart, "sha256": hashlib.sha256(b"old chart").hexdigest()}])
        self.assertIsNotNone(self.gate())

    def test_changed_engineering_parameters_invalidate_report(self):
        self.files[CASE_DICT_PATH] = self.text.replace("caseMeta {}", "caseMeta { units { length mm; } }").encode()
        self.assertIsNotNone(self.gate())

    def test_workflow_bookkeeping_keeps_same_input_identity(self):
        self.files[CASE_DICT_PATH] = self.text.replace("workflowState D;", "workflowState E;").encode()
        self.assertIsNone(self.gate())

    def test_empty_input_manifest_is_not_evidence(self):
        self.set_manifest(inputs=[])
        self.assertIsNotNone(self.gate())

    def test_empty_artifact_manifest_is_not_evidence(self):
        self.set_manifest(artifacts=[])
        self.assertIsNotNone(self.gate())

    def test_invalid_json_containers_fail_closed(self):
        for path in (self.metrics_path, self.manifest_path):
            original = self.files[path]
            self.files[path] = b"[]"
            self.assertIsNotNone(self.gate())
            self.files[path] = original


class SolveEvidenceTests(unittest.TestCase):
    def test_exit_zero_alone_is_not_solve_evidence_and_inputs_are_frozen(self):
        with tempfile.TemporaryDirectory(prefix="vf-evidence-") as directory:
            root = Path(directory)
            files = {"0-caseDict/caseDict": CASE_DICT_TEMPLATE,
                     "C-run/case-1/system/controlDict": "application icoFoam;",
                     "C-run/case-1/0/U": "initial field", "C-run/case-1/1/U": "solved field",
                     ".vibeflow/jobs/job-7.log": ""}
            for name in ("points", "faces", "owner", "boundary"):
                files[f"C-run/case-1/constant/polyMesh/{name}"] = name
            for path, content in files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            snapshot = capture_solve_inputs(root, "C-run/case-1", 7)
            log = root / ".vibeflow/jobs/job-7.log"
            with self.assertRaises(ValueError):
                complete_solve_evidence(root, snapshot, log)
            log.write_text("Time = 1\nsmoothSolver: Solving for Ux, Initial residual = 0.1, "
                           "Final residual = 1e-9, No Iterations 2\nEnd\n", encoding="utf-8")
            result = complete_solve_evidence(root, snapshot, log)
            self.assertEqual(result["job_id"], 7)
            self.assertTrue(result["outputs"])
            (root / "C-run/case-1/system/controlDict").write_text("changed solver input", encoding="utf-8")
            with self.assertRaises(ValueError):
                complete_solve_evidence(root, snapshot, log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
