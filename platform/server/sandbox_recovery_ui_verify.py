"""Verify actual browser download; inject/restore corruption only in its isolated fixture."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('action', choices=['verify', 'corrupt', 'restore'])
    args = parser.parse_args()
    root = args.root.resolve()
    workspace = Path(__file__).resolve().parents[2]
    assert root.parent == workspace / 'tmp' and root.name.startswith('sandbox-recovery-ui-')
    ready = json.loads((root / 'ready.json').read_text())
    assert Path(ready['root']).resolve() == root
    evidence = json.loads((root / 'browser-checks.json').read_text(encoding='utf8'))
    with sqlite3.connect(root / 'test.sqlite') as connection:
        profile, receipt = connection.execute(
            'SELECT profile_json, result_json FROM native_requests WHERE id=? AND kind=? AND phase=?',
            (evidence['recovery_id'], 'terminal.recover', 'succeeded')).fetchone()
    profile, receipt = json.loads(profile), json.loads(receipt)
    archive = Path(profile['root']).resolve() / 'recovery.zip'
    assert archive.is_relative_to(root / 'files' / '.terminal-recovery'), archive
    backup = root / 'archive-before-corruption.zip'
    if args.action == 'corrupt':
        assert not backup.exists()
        payload = archive.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == receipt['archive_sha256']
        backup.write_bytes(payload)
        damaged = bytearray(payload)
        damaged[-1] ^= 1
        archive.write_bytes(damaged)
        print('ACTUAL_ARCHIVE_BYTE_CHANGED')
        return
    if args.action == 'restore':
        payload = backup.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == receipt['archive_sha256']
        archive.write_bytes(payload)
        print('ACTUAL_ARCHIVE_RESTORED')
        return
    payload = (root / 'browser-recovery.zip').read_bytes()
    assert len(payload) == receipt['archive_bytes']
    assert hashlib.sha256(payload).hexdigest() == receipt['archive_sha256']
    assert payload == archive.read_bytes()
    with zipfile.ZipFile(root / 'browser-recovery.zip') as downloaded:
        assert downloaded.testzip() is None
        assert downloaded.read('Ref/browser-output.txt') == (ready['proof'] + '\n').encode()
        assert downloaded.getinfo('Ref/browser-empty/').is_dir()
        assert downloaded.getinfo('Ref/browser-output.txt').external_attr >> 16 & 0o777 == 0o600
        manifest = evidence['detail']['manifest']
        assert set(downloaded.namelist()) == set(manifest['files']) | {name+'/' for name in manifest['directories']}
        for name, expected in manifest['files'].items():
            data = downloaded.read(name)
            assert len(data) == expected['size'] and hashlib.sha256(data).hexdigest() == expected['sha256']
    result = {'passed': True, 'archive_sha256': receipt['archive_sha256'], 'archive_bytes':len(payload),
              'all_files_match_manifest': True, 'unique_shell_proof_matches':True,
              'empty_directory_preserved':True, 'file_mode_0600_preserved':True,
              'llm_calls':0, 'mock_llm_calls':0}
    (root / 'download-verification.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
