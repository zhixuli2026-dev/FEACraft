"""Offline report snapshot invariants; compiler calls stubbed, publication uses real files."""
import copy
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app import storage
from app.services.casedict import CASE_DICT_TEMPLATE
from app.services.report_build import (capture_report_guard, prepare_report_snapshot,
    publish_report_artifacts, report_work_dir, validate_report_snapshot)
from app.services.latex_build import prepare_latex_snapshot, validate_latex_snapshot


class ReportSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="vf-report-"))
        self.enterContext(patch("app.storage._fs_root", return_value=Path(self.temp)))
        self.enterContext(patch("app.storage._use_s3", return_value=False))
        self.prefix = "case"
        storage.put_text(self.prefix, "0-caseDict/caseDict", CASE_DICT_TEMPLATE)
        storage.put_text(self.prefix, "E-report/report.md", "# Original\n![plot](../Ref/plot.svg)\n")
        storage.put_text(self.prefix, "Ref/plot.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.document = {"pandoc-api-version": [1, 23], "meta": {}, "blocks": [{"t": "Para", "c": [
            {"t": "Image", "c": [["", [], []], [], ["../Ref/plot.svg", ""]]}]}]}

    def snapshot(self, job_id=1):
        compiler = subprocess.CompletedProcess('pandoc',0,json.dumps(copy.deepcopy(self.document)).encode(),b'')
        with patch("app.services.pandoc_execution.run", return_value=compiler):
            return prepare_report_snapshot(self.prefix, job_id, "pandoc", capture_report_guard(self.prefix))

    def test_inline_svg_image_dependency_is_frozen_and_external_reference_rejected(self):
        self.document['blocks']=[{'t':'RawBlock','c':['html',
            '<svg><image href="../Ref/plot.svg"/><use href="#local"/></svg>']}]
        snapshot=self.snapshot()
        assert {item['path'] for item in snapshot['inputs']}=={'E-report/report.md','Ref/plot.svg'}
        work=storage._fs_path(self.prefix,report_work_dir(1))
        document=json.loads((work/'document.json').read_text(encoding='utf-8'))
        assert 'href="Ref/plot.svg"' in document['blocks'][0]['c'][1]
        assert 'href="#local"' in document['blocks'][0]['c'][1]
        self.document['blocks'][0]['c'][1]='<svg><image href="https://example.invalid/image.svg"/></svg>'
        with self.assertRaisesRegex(ValueError,'URI'):
            self.snapshot(2)

    def outputs(self, job_id=1, *, docx=True):
        work = storage._fs_path(self.prefix, report_work_dir(job_id))
        (work / "out").mkdir()
        (work / "out/report.html").write_text("<!DOCTYPE html><html>Frozen</html>", encoding="utf-8")
        if docx:
            with zipfile.ZipFile(work / "out/report.docx", "w") as archive:
                archive.writestr("word/document.xml", "<document>Frozen</document>")

    def test_image_snapshot_is_independent_and_change_prevents_publication(self):
        snapshot = self.snapshot()
        frozen = storage._fs_path(self.prefix, report_work_dir(1)) / "Ref/plot.svg"
        before = frozen.read_bytes()
        storage.put_text(self.prefix, "Ref/plot.svg", "changed")
        self.assertEqual(frozen.read_bytes(), before)
        self.outputs()
        storage.put_text(self.prefix, "E-report/build/report.html", "baseline")
        with self.assertRaises(ValueError):
            publish_report_artifacts(self.prefix, snapshot)
        self.assertEqual(storage.get_bytes(self.prefix, "E-report/build/report.html"), b"baseline")

    def test_partial_output_cannot_replace_baseline(self):
        snapshot = self.snapshot()
        self.outputs(docx=False)
        storage.put_text(self.prefix, "E-report/build/report.html", "baseline")
        with self.assertRaises(ValueError):
            publish_report_artifacts(self.prefix, snapshot)
        self.assertEqual(storage.get_bytes(self.prefix, "E-report/build/report.html"), b"baseline")

    def test_complete_pair_has_committed_manifest(self):
        snapshot = self.snapshot()
        self.outputs()
        publish_report_artifacts(self.prefix, snapshot)
        manifest = json.loads(storage.get_bytes(self.prefix, "E-report/build/report-manifest.json"))
        self.assertEqual(manifest["job_id"], 1)
        self.assertEqual(len(manifest["artifacts"]), 2)
        self.assertEqual({item["path"] for item in manifest["inputs"]}, {"E-report/report.md", "Ref/plot.svg"})

    def test_engineering_edit_invalidates_but_workflow_bookkeeping_does_not(self):
        snapshot = self.snapshot()
        storage.put_text(self.prefix, "0-caseDict/caseDict", CASE_DICT_TEMPLATE.replace("workflowState A;", "workflowState V;"))
        validate_report_snapshot(self.prefix, snapshot)
        storage.put_text(self.prefix, "0-caseDict/caseDict", CASE_DICT_TEMPLATE.replace("caseMeta\n{", "caseMeta\n{\n discipline fluid;"))
        with self.assertRaises(ValueError):
            validate_report_snapshot(self.prefix, snapshot)

    def test_external_resources_fail_before_compilation(self):
        for url in ("https://example.com/plot.svg", "//example.com/plot.svg", "../../outside.svg"):
            self.document["blocks"][0]["c"][0]["c"][2][0] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.snapshot(job_id=len(list(Path(self.temp).rglob("document.json"))) + len(url))

    def test_latex_snapshot_excludes_outputs_and_detects_added_sources(self):
        storage.put_text(self.prefix, "E-report/latex/main.tex", "original")
        storage.put_text(self.prefix, "E-report/latex/build/main.pdf", "old output")
        records = prepare_latex_snapshot(self.prefix, "E-report/latex", 8)
        storage.put_text(self.prefix, "E-report/latex/build/main.pdf", "new output")
        validate_latex_snapshot(self.prefix, "E-report/latex", records)
        storage.put_text(self.prefix, "E-report/latex/extra.tex", "new input")
        with self.assertRaises(ValueError):
            validate_latex_snapshot(self.prefix, "E-report/latex", records)

    def test_unquoted_html_image_is_frozen_and_remote_html_image_is_rejected(self):
        self.document["blocks"] = [{"t": "RawBlock", "c": ["html", "<img src=../Ref/plot.svg>"]}]
        self.snapshot()
        document = json.loads(storage.get_bytes(self.prefix, report_work_dir(1) + "/document.json"))
        self.assertIn('src="Ref/plot.svg"', document["blocks"][0]["c"][1])
        self.document["blocks"] = [{"t": "RawBlock", "c": ["html", "<img src=https://example.com/plot.svg>"]}]
        with self.assertRaises(ValueError):
            self.snapshot(2)

    def test_latex_copy_that_does_not_match_original_records_is_rejected(self):
        storage.put_text(self.prefix, "E-report/latex/main.tex", "original")
        original_copy = shutil.copytree

        def changed_during_copy(source, target, **kwargs):
            result = original_copy(source, target, **kwargs)
            (target / "main.tex").write_text("interleaved source", encoding="utf-8")
            return result

        with patch("app.services.latex_build.shutil.copytree", side_effect=changed_during_copy):
            with self.assertRaises(ValueError):
                prepare_latex_snapshot(self.prefix, "E-report/latex", 9)

    def test_invalid_markdown_exposes_a_useful_compiler_diagnostic(self):
        failure = subprocess.CalledProcessError(1, "pandoc", stderr=b"invalid YAML metadata")
        with patch("app.services.pandoc_execution.run", side_effect=failure):
            with self.assertRaisesRegex(ValueError, "invalid YAML metadata"):
                prepare_report_snapshot(self.prefix, 10, "pandoc", capture_report_guard(self.prefix))


if __name__ == "__main__":
    unittest.main(verbosity=2)
