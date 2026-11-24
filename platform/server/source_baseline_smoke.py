"""Prove rollback snapshots exclude private state and reject damaged archives."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import source_baseline as baseline


def main():
    with tempfile.TemporaryDirectory(prefix="vf-source-baseline-") as directory:
        root = Path(directory)
        fixtures = {"AGENTS.md": "Use real authorized model calls only.",
            "platform/server/app/main.py": "print('product')",
            "platform/server/.env.example": "SECRET=replace-me",
            "platform/server/.env": "PRIVATE",
            "platform/server/data/files/field.txt": "PRIVATE",
            "platform/server/data/.vault_key": "PRIVATE",
            "platform/server/data/completion/openfoam.json": "{}",
            "platform/tools/.edge-profile/preferences.json": "PRIVATE",
            "platform/web/node_modules/lib/code.js": "dependency",
            "platform/infra/Dockerfile.api": "FROM python",
            "platform/infra/nginx-web.conf": "server {}",
            ".github/workflows/platform-ci.yml": "name: verify"}
        for name, text in fixtures.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        original_root = baseline.ROOT
        try:
            baseline.ROOT = root
            archive = root / "baseline.zip"
            with contextlib.redirect_stdout(io.StringIO()):
                baseline.create(archive)
                baseline.restore(archive, root / "restored")
            manifest = baseline.verify(archive)
            names = {item["path"] for item in manifest["files"]}
            expected = {name for name, text in fixtures.items() if text not in {"PRIVATE", "dependency"}}
            assert names == expected, (names, expected)
            for item in manifest["files"]:
                assert (root / "restored" / item["path"]).read_bytes() == (root / item["path"]).read_bytes()
            try:
                baseline.restore(archive, root / "restored")
                raise AssertionError("Restore overwrote an existing directory")
            except FileExistsError:
                pass
            try:
                baseline.create(archive)
                raise AssertionError("Baseline overwritten")
            except FileExistsError:
                pass
            corrupt = root / "corrupt.zip"
            with zipfile.ZipFile(archive) as source, zipfile.ZipFile(corrupt, "w") as dest:
                for name in source.namelist():
                    dest.writestr(name, b"corrupt" if name.endswith("main.py") else source.read(name))
            corrupt.with_suffix(".zip.sha256").write_text(baseline.sha(corrupt.read_bytes()), encoding="ascii")
            try:
                baseline.restore(corrupt, root / "not-created")
                raise AssertionError("Damaged baseline restored")
            except ValueError:
                assert not (root / "not-created").exists()
            for index,name in enumerate(('../AGENTS.md','AGENTS.md/extra.py','other.md','C:/AGENTS.md')):
                unsafe=root/f'unsafe-{index}.zip';data=b'invalid root'
                with zipfile.ZipFile(unsafe,'w') as dest:
                    dest.writestr(name,data)
                    dest.writestr(baseline.MANIFEST,json.dumps({'files':[{'path':name,'bytes':len(data),'sha256':baseline.sha(data)}]}))
                unsafe.with_suffix('.zip.sha256').write_text(baseline.sha(unsafe.read_bytes()),encoding='ascii')
                try:baseline.verify(unsafe)
                except ValueError:pass
                else:raise AssertionError('Unsafe root instruction path accepted: '+name)
        finally:
            baseline.ROOT = original_root
    print("SOURCE_BASELINE_OK privacy restore hashes exclusive_create corruption")


if __name__ == "__main__":
    main()
