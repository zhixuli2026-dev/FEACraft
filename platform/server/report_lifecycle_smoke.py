"""Real compiler/HTTP lifecycle with file barriers; never touches developer services.

Only the temporary app delays process start. Production preparation, compiler,
finish hooks, validation, publication, cancellation and Job persistence are real.
"""
import io
import json
from pathlib import Path
import time
import zipfile

from app.services.casedict import set_block
from testsupport import IsolatedServer

TEST_APP = '''from pathlib import Path
import asyncio,json
import shlex
import sys
from app.main import app
from app.services import jobrunner
from app.services.runtime_config import get_runtime_setting
from app.storage import _fs_path

original_start = jobrunner.start_job
async def controlled_start(job_id, command, case_s3_prefix, **kwargs):
    argv = kwargs.get("argv")
    if argv and Path(argv[1]).name == "report_worker.py":
        kwargs["argv"] = [sys.executable, "-u", str(Path(__file__).with_name("controlled_report_worker.py")),
                           str(job_id), *argv[1:]]
    elif kwargs.get("runtime") == "wsl" and "latexmk" in command:
        controlled = (f"printf 'CONTROLLED_READY\\n'; while [ ! -f .vibeflow/test-release-job-{job_id} ]; "
                      f"do sleep 0.05; done; " + command)
        cwd = jobrunner.win_to_wsl_path(_fs_path(case_s3_prefix, "."))
        kwargs["argv"] = ["wsl.exe", "-d", get_runtime_setting("wsl_distro", "vibeflow-linux"),
                          "--exec", "bash", "-lc", f"cd {shlex.quote(cwd)} && " + controlled]
    return await original_start(job_id, command, case_s3_prefix, **kwargs)
jobrunner.start_job = controlled_start
original_spawn=asyncio.create_subprocess_exec
async def controlled_spawn(original_spawn,*args,**kwargs):
    if len(args)>1 and Path(str(args[1])).name in {'registered_latex_worker.py','registered_report_worker.py'}:
        manifest=json.loads((Path(kwargs['cwd']).parent/'manifest.json').read_text(encoding='utf-8'))
        args=(sys.executable,'-u',str(Path(__file__).with_name('controlled_report_worker.py')),
            str(manifest['source']['job_id']),*args[1:])
    return await original_spawn(*args,**kwargs)
from testsupport import intercept_owned_spawn
asyncio.create_subprocess_exec=intercept_owned_spawn(original_spawn,controlled_spawn)
'''

TEST_WORKER = '''from pathlib import Path
import runpy
import shutil
import sys
import time
job_id = int(sys.argv[1])
registered=Path(sys.argv[2]).name in {'registered_latex_worker.py','registered_report_worker.py'}
report=Path(sys.argv[2]).name=='registered_report_worker.py'
case_root=Path.cwd().parents[3] if registered else Path.cwd()
release = case_root/f".vibeflow/test-release-job-{job_id}"
print("CONTROLLED_READY", flush=True)
deadline = time.monotonic() + 90
while not release.exists():
    if time.monotonic() > deadline:
        raise RuntimeError("Test barrier was not released")
    time.sleep(0.025)
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
if report:
    captured=case_root/f'.vibeflow/test-captures/job-{job_id}.html'
    captured.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile('E-report/build/report.html',captured)
if registered: raise SystemExit(0)
work = Path(sys.argv[sys.argv.index("--work-dir") + 1])
captured = Path(f".vibeflow/test-captures/job-{job_id}.html")
captured.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(work / "out/report.html", captured)
'''

BLUE = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="40"><rect width="100" height="40" fill="blue"/></svg>'


def main():
    api = IsolatedServer("report-lifecycle", app="report_acceptance_app:app")
    (api.root / "report_acceptance_app.py").write_text(TEST_APP, encoding="utf-8")
    (api.root / "controlled_report_worker.py").write_text(TEST_WORKER, encoding="utf-8")
    pending = set()
    with api:
        api.create_case("report-lifecycle")
        case_root = api.root / "files/workspaces/1/cases/1"

        def raw(path):
            from urllib.parse import quote
            return api.call("GET", api.prefix + "/raw?path=" + quote(path, safe=""), raw=True)

        def ready(job_id):
            for _ in range(300):
                job = api.call("GET", f"/jobs/{job_id}")
                if "CONTROLLED_READY" in job.get("log_backlog", ""):
                    return job
                if job["status"] not in ("queued", "running"):
                    raise AssertionError(job)
                time.sleep(0.05)
            raise TimeoutError("Compiler did not reach the test barrier")

        def start(latex=False):
            result = api.call("POST", api.prefix + ("/latex:build" if latex else "/report:build"),
                              {"recipe": "pdflatex"} if latex else {})
            job_id = result["job_id"]
            pending.add(job_id)
            ready(job_id)
            return job_id

        def release(job_id, expected="succeeded"):
            (case_root / f".vibeflow/test-release-job-{job_id}").write_text("release", encoding="utf-8")
            result = api.wait_job(job_id, expected=expected)
            pending.discard(job_id)
            return result

        def report(marker):
            api.put("E-report/report.md", f"# {marker}\n\nFrozen plot: ![plot](../Ref/plot.svg)\n\n$x^2$\n")

        def public_pair():
            return raw("E-report/build/report.html"), raw("E-report/build/report.docx")

        def tex(marker):
            api.put("E-report/latex/main.tex", '\\documentclass{article}\n\\begin{document}\n'
                    + f'\\typeout{{BUILD_SOURCE_{marker}}}\n{marker.replace("_", " ")}\n\\end{{document}}\n')

        try:
            api.put("Ref/plot.svg", BLUE)
            report("BASELINE")
            first = start()
            release(first)
            baseline = public_pair()
            assert b"BASELINE" in baseline[0] and b"data:image/svg+xml" in baseline[0]
            assert b"<math" in baseline[0], "HTML math should render without fetching MathJax"
            with zipfile.ZipFile(io.BytesIO(baseline[1])) as archive:
                media = [archive.read(name) for name in archive.namelist() if name.startswith("word/media/")]
                assert any(b'fill="blue"' in content for content in media)
            manifest = json.loads(raw("E-report/build/report-manifest.json"))
            assert manifest["job_id"] == first and set(manifest["inputs"]) == {
                '0-caseDict/caseDict','E-report/report.md','Ref/plot.svg'}
            print("1. Real pandoc HTML embedded image/MathML and DOCX image + committed manifest [OK]", flush=True)

            stale = start()
            api.put("Ref/plot.svg", BLUE.replace("blue", "red"))
            report("EDITED_DURING_BUILD")
            failed = release(stale, expected="failed")
            assert failed['attempt']['reason_code']=='input_stale', failed
            captured = (case_root / f".vibeflow/test-captures/job-{stale}.html").read_bytes()
            assert b"BASELINE" in captured and b"EDITED_DURING_BUILD" not in captured
            assert public_pair() == baseline
            print("2. Concurrent Markdown/image edits rejected; compiler consumed frozen content; baseline retained [OK]", flush=True)

            report("ENGINEERING_CHANGE")
            changed = start()
            info = api.call("GET", api.prefix + "/caseDict")
            api.call("PUT", api.prefix + "/caseDict", {"content": set_block(
                info["content"], "caseMeta", {"discipline": "fluid"})})
            release(changed, expected="failed")
            assert public_pair() == baseline
            print("3. Concurrent engineering edit rejected before publication [OK]", flush=True)

            report("OLDER")
            older = start()
            report("NEWER")
            newer = start()
            release(newer)
            latest_pair = public_pair()
            assert b"NEWER" in latest_pair[0]
            release(older)
            assert public_pair() == latest_pair
            assert json.loads(raw("E-report/build/report-manifest.json"))["job_id"] == newer
            print("4. Reversed completion order cannot overwrite newest report [OK]", flush=True)

            report("CANCELLED")
            cancelled = start()
            api.call("POST", f"/jobs/{cancelled}:cancel")
            api.wait_job(cancelled, expected="cancelled")
            pending.discard(cancelled)
            assert public_pair() == latest_pair
            print("5. Cancelled build retains published pair [OK]", flush=True)

            tex("LATEX_BASELINE")
            latex_base = start(latex=True)
            release(latex_base)
            pdf = raw("E-report/latex/build/main.pdf")
            assert pdf.startswith(b"%PDF")
            tex("LATEX_FROZEN")
            latex_stale = start(latex=True)
            tex("LATEX_CHANGED")
            latex_result = release(latex_stale, expected="failed")
            assert "BUILD_SOURCE_LATEX_FROZEN" in latex_result["log_backlog"]
            assert raw("E-report/latex/build/main.pdf") == pdf
            print("6. Real pdflatex uses source snapshot; stale source blocks publication and retains PDF [OK]", flush=True)

            latex_engineering = start(latex=True)
            info = api.call("GET", api.prefix + "/caseDict")
            api.call("PUT", api.prefix + "/caseDict", {"content": set_block(
                info["content"], "caseMeta", {"discipline": "thermal"})})
            release(latex_engineering, expected="failed")
            assert raw("E-report/latex/build/main.pdf") == pdf
            print("7. Engineering changes during real LaTeX compile also preserve baseline PDF [OK]", flush=True)

            # Successful/cancelled/failed jobs have all run their cleanup hooks.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not list((case_root / ".vibeflow/report-jobs").glob("job-*")):
                    break
                time.sleep(0.05)
            assert not list((case_root / ".vibeflow/report-jobs").glob("job-*"))
            assert not list((case_root / ".vibeflow/latex-jobs").glob("job-*"))
            print(f"REPORT_LIFECYCLE_OK EVIDENCE={api.root}", flush=True)
        finally:
            for job_id in pending:
                (case_root / f".vibeflow/test-release-job-{job_id}").write_text("cleanup", encoding="utf-8")
                api.call("POST", f"/jobs/{job_id}:cancel")


if __name__ == "__main__":
    main()
