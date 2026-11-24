"""I10A：CLI 版本与生成标记 force 的安全覆盖 golden。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from app.services.flotherm_output import MARKER, SCHEMA, prepare_output, write_manifest
from app.services.flotherm_case import generate_case
from app.services.flotherm_ir import build_ir

ROOT = Path(__file__).resolve().parent
CLI = ROOT / "flotherm2foam.py"


def run(*args):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


with tempfile.TemporaryDirectory(prefix="vf-flotherm-i10a-") as temp:
    root = Path(temp)
    version = run("--version")
    assert version.returncode == 0 and version.stdout.strip() == "flotherm2foam 0.1.0", version
    output = root / "case"
    ready = run("--output", output)
    assert ready.returncode == 0 and (output / MARKER).is_file(), ready.stderr
    generated = output / "system" / "controlDict"
    generated.parent.mkdir()
    generated.write_text("generated-old", encoding="utf-8")
    write_manifest(output, ["system/controlDict"])
    refused = run("--output", output)
    assert refused.returncode != 0 and generated.read_text() == "generated-old", refused
    forced = run("--output", output, "--force")
    assert forced.returncode == 0 and not generated.exists(), forced.stderr
    assert json.loads((output / MARKER).read_text())["files"] == []

    write_manifest(output, ["system/controlDict"])
    generated.parent.mkdir(exist_ok=True)
    generated.write_text("generated-new", encoding="utf-8")
    user_file = output / "user-notes.txt"
    user_file.write_text("DO_NOT_DELETE", encoding="utf-8")
    mixed = run("--output", output, "--force")
    assert mixed.returncode != 0, mixed
    assert generated.read_text() == "generated-new" and user_file.read_text() == "DO_NOT_DELETE"

    user_file.unlink()
    generated.unlink()
    (output / "system").rmdir()
    (output / MARKER).unlink()
    user_file.write_text("UNMARKED", encoding="utf-8")
    unmarked = run("--output", output, "--force")
    assert unmarked.returncode != 0 and user_file.read_text() == "UNMARKED"

    outside = root / "outside.txt"
    outside.write_text("OUTSIDE", encoding="utf-8")
    user_file.unlink()
    (output / MARKER).write_text(json.dumps({"schema": SCHEMA, "files": ["../outside.txt"]}), encoding="utf-8")
    traversal = run("--output", output, "--force")
    assert traversal.returncode != 0 and outside.read_text() == "OUTSIDE"

    try:
        prepare_output(output, force=True, studio=True)
    except ValueError as error:
        assert "Studio 不支持" in str(error)
    else:
        raise AssertionError("Studio force 未拒绝")
    try:
        prepare_output(output, keep_stage=True, studio=True)
    except ValueError:
        pass
    else:
        raise AssertionError("Studio keep-stage 未拒绝")
    assert outside.read_text() == "OUTSIDE"
    assert run("--output", output, "--keep-stage").returncode != 0

    document = output / MARKER
    document.write_text('[]', encoding='utf-8')
    malformed = run('--output', output, '--force')
    assert malformed.returncode == 2 and 'Traceback' not in malformed.stderr
    document.unlink()
    write_manifest(output, [])
    foreign = output/'notes-dir'
    foreign.mkdir()
    assert run('--output', output, '--force').returncode == 2 and foreign.is_dir()
    foreign.rmdir()

    generated = output/'old.foam'
    generated.write_text('generated', encoding='utf-8')
    write_manifest(output, [generated.name])
    alias = root/'linked-case'
    alias.symlink_to(output, target_is_directory=True)
    assert run('--output', alias, '--force').returncode == 2 and generated.read_bytes() == b'generated'
    actual_parent = root/'real-parent'
    actual_parent.mkdir()
    linked_parent = root/'linked-parent'
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    assert run('--output', linked_parent/'new-child').returncode == 2 and not list(actual_parent.iterdir())
    rogue = output/'outside-link'
    rogue.symlink_to(actual_parent, target_is_directory=True)
    assert run('--output', output, '--force').returncode == 2 and rogue.is_symlink() and generated.exists()
    rogue.unlink()

    marker_bytes = document.read_bytes()
    document.unlink()
    marker_target = root/'foreign-marker.json'
    marker_target.write_bytes(marker_bytes)
    document.symlink_to(marker_target)
    assert run('--output', output, '--force').returncode == 2 and generated.exists()
    assert marker_target.read_bytes() == marker_bytes
    document.unlink()
    write_manifest(output, [generated.name])

    other = output/'second.foam'
    other.write_bytes(b'second generated artifact')
    write_manifest(output,[generated.name,other.name])
    fault = r'''
import os, runpy, sys, time
from pathlib import Path
cli, destination, ready = sys.argv[1:]
unlink = Path.unlink
def interrupt(path, *args, **kwargs):
    result = unlink(path, *args, **kwargs)
    if path.name == 'old.foam':
        Path(ready).touch()
        until = time.monotonic() + 20
        while not Path(ready+'.release').is_file():
            if time.monotonic() >= until:
                raise TimeoutError('No release from recovery test')
            time.sleep(.025)
        os._exit(83)
    return result
Path.unlink = interrupt
sys.argv = [cli,'--output',destination,'--force']
runpy.run_path(cli,run_name='__main__')
'''
    ready = root/'generation-cleanup-paused'
    process = subprocess.Popen([sys.executable,'-B','-c',fault,str(CLI),str(output),str(ready)],cwd=ROOT)
    try:
        until = time.monotonic()+10
        while not ready.is_file() and process.poll() is None and time.monotonic()<until:
            time.sleep(.025)
        assert ready.exists() and process.poll() is None and not generated.exists() and other.exists()
        contender = run('--output', output, '--force')
        assert contender.returncode == 2 and other.exists(), contender
    finally:
        ready.with_suffix('.release').touch()
        assert process.wait(timeout=25)==83
    assert set(json.loads(document.read_bytes())['reset_pending']['files'])=={generated.name,other.name}
    generated.write_bytes(b'replaced following interrupted force')
    assert run('--output',output,'--force').returncode==2 and other.exists()
    assert generated.read_bytes()==b'replaced following interrupted force'
    generated.unlink()
    added = output/'new-user-file'
    added.write_bytes(b'keep')
    assert run('--output',output,'--force').returncode==2 and other.exists() and added.read_bytes()==b'keep'
    added.unlink()
    assert run('--output',output,'--force').returncode==0 and not other.exists()
    assert json.loads(document.read_bytes())['files']==[] and set(output.iterdir())=={document}
    assert outside.read_bytes()==b'OUTSIDE'

    actual = root/'converter-output'
    chip = {'id':'chip','type':'box','material':'silicon','mapping':'converted','power_w':5,
            'shape':{'kind':'box','origin':[0,0,0],'dimensions':[1,1,1]}}
    ir = build_ir('chip.ecxml',b'<model/>',[chip],{'silicon':{'conductivity_w_mk':130,'rho_cp':1000}})
    generate_case(ir,actual)
    artifacts = {p.relative_to(actual).as_posix() for p in actual.rglob('*') if p.is_file() and p.name!=MARKER}
    assert 'constant/fvOptions' in artifacts and 'system/blockMeshDict' in artifacts
    assert set(json.loads((actual/MARKER).read_bytes())['files'])==artifacts
    assert run('--output',actual).returncode==2
    forced = run('--output',actual,'--force')
    assert forced.returncode==0 and list(actual.iterdir())==[actual/MARKER],forced.stderr
print("I10A --version, generated-only force, zero-delete failures and Studio guard: OK")
print('I10A real links, marker conflicts, concurrent refusal, actual CLI exit and retry: OK')
print('I10A actual generated OpenFOAM directory resets to one valid marker: OK')
print("\n=== I10A Flotherm 安全 CLI 冒烟通过 ===")
