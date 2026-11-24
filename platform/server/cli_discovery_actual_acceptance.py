"""Actual local CLI/WSL discovery; only --version, never model inference.

Version-directory names exercise selection policy, not claimed binary versions.
All candidates point to a real installed CLI or invoke it via a batch wrapper.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from app.services.cli_discovery import discover_cli, version_sort_key

ROOT = Path(__file__).resolve().parents[2]


def run(program, distro):
    folder = Path(tempfile.mkdtemp(prefix='cli-discovery-actual-', dir=ROOT / 'tmp'))
    result = {'passed': False, 'classification': 'no_llm', 'real_model_calls': 0,
              'mock_llm_calls': 0, 'checks': [], 'commands': [], 'evidence': str(folder)}

    def execute(argv):
        process = subprocess.run(argv, capture_output=True, timeout=40)
        record = {'argv': argv, 'returncode': process.returncode,
                  'stdout': process.stdout.decode('utf8', 'replace').strip(),
                  'stderr': process.stderr.decode('utf8', 'replace').strip()}
        result['commands'].append(record)
        return record

    def version(info):
        assert info is not None
        argv = [info['path'], '--version']
        if Path(info['path']).suffix.lower() in {'.cmd', '.bat'}:
            argv = [os.environ['COMSPEC'], '/d', '/c', *argv]
        observed = execute(argv)
        assert observed['returncode'] == 0 and 'codex' in observed['stdout'].lower(), observed
        return observed['stdout']

    def wsl(argv):
        # The production function deliberately requires an owner-supplied callback.
        return execute(argv[:3] + ['--user', 'nobody'] + argv[3:])

    try:
        program = program.resolve(strict=True)
        result['program'] = str(program)
        result['program_sha256'] = hashlib.sha256(program.read_bytes()).hexdigest()
        baseline_version = version({'path': str(program)})
        result['actual_binary_version'] = baseline_version
        versions = folder / 'versions with spaces'
        labels = ['0.151.0-alpha.1', '0.151.0-beta.1', '0.151.0-rc.2', '0.151.0', '0.150.9']
        for label in labels:
            directory = versions / label / 'bin'
            directory.mkdir(parents=True)
            (directory / 'codex.exe').symlink_to(program)
        info = discover_cli('codex', env={'VIBEFLOW_CODEX_DIR': str(versions), 'PATH': ''})
        assert info['source'] == 'version_dir' and info['version'] == '0.151.0'
        assert version(info) == baseline_version
        assert sorted(labels[:4], key=version_sort_key) == labels[:4]
        assert version_sort_key('0.151.0-rc.10') > version_sort_key('0.151.0-rc.2')
        result['checks'].append('actual_version_directory_selection_stable_rc_beta_alpha_and_numeric_suffix')

        selected = discover_cli('codex', env={'VIBEFLOW_CODEX': str(program),
                                            'VIBEFLOW_CODEX_DIR': str(versions), 'PATH': ''})
        assert selected['source'] == 'explicit' and version(selected) == baseline_version
        result['checks'].append('explicit_real_program_precedes_version_directory')

        for suffix in ('.exe', '.cmd', '.bat', ''):
            directory = folder / ('path ' + (suffix or 'no suffix'))
            directory.mkdir()
            candidate = directory / ('codex' + suffix)
            if suffix in {'.cmd', '.bat'}:
                candidate.write_text('@echo off\n"' + str(program) + '" %*\n', encoding='utf8')
            else:
                candidate.symlink_to(program)
            selected = discover_cli('codex', env={'PATH': str(directory)})
            assert selected['source'] == 'path'
            assert Path(selected['path']) == candidate.resolve()
            assert version(selected) == baseline_version
        result['checks'].append('supplied_environment_PATH_and_all_four_suffixes_resolve_and_execute_real_CLI')

        assert discover_cli('codex', env={'PATH': ''}) is None
        assert discover_cli('codex', env={'PATH': str(folder)}, path_dirs=[str(program.parent)])['source'] == 'path'
        broken = folder / 'operator missing.exe'
        try:
            discover_cli('codex', env={'VIBEFLOW_CODEX': str(broken), 'PATH': str(program.parent)})
        except FileNotFoundError:
            pass
        else:
            raise AssertionError('Invalid explicit path fell back to PATH')
        broken.symlink_to(program)
        assert version(discover_cli('codex', env={'VIBEFLOW_CODEX': str(broken)})) == baseline_version
        broken.unlink()
        result['checks'].append('empty_PATH_override_invalid_explicit_fail_closed_and_repair_without_restart')

        identity = execute(['wsl.exe', '-d', distro, '--user', 'nobody', '--exec', 'id', '-u'])
        assert identity['returncode'] == 0 and identity['stdout'] == '65534'
        selected = discover_cli('codex', env={'PATH': ''}, wsl_runner=wsl, distro=distro)
        assert selected['runtime'] == 'wsl' and selected['node_version'].startswith('v')
        actual = execute(['wsl.exe', '-d', distro, '--user', 'nobody', '--exec', selected['path'], '--version'])
        assert actual['returncode'] == 0 and 'codex' in actual['stdout'].lower()
        result['wsl_discovery'] = selected
        result['checks'].append('actual_WSL_nobody_Codex_discovery_Node_version_and_native_execution')

        # An actual PATH without Node tests optional Node reporting. The shell,
        # Codex and WSL still execute normally; no fixed command output is used.
        def no_node(argv):
            actual = argv[:3] + ['--user', 'nobody', '--exec', '/bin/bash', '--noprofile', '--norc', '-c',
                                 'export PATH=/mnt/d/vibeflow_reverse/.tools/codex/0.167.0-alpha.4.1/linux-x64/bin; ' + argv[-1]]
            return execute(actual)

        missing_node = discover_cli('codex', env={'PATH': ''}, wsl_runner=no_node, distro=distro)
        assert missing_node['runtime'] == 'wsl' and missing_node['node_version'] is None
        assert discover_cli('codex', env={'PATH': ''}) is None
        for invalid in (False, 0, '', True):
            try:
                discover_cli('codex', env={'PATH': ''}, wsl_runner=invalid)
            except ValueError:
                pass
            else:
                raise AssertionError('Non-callable WSL runner accepted')
        result['checks'].append('actual_missing_Node_is_optional_and_WSL_requires_explicit_callable')
        result['passed'] = True
    except Exception as error:
        result['failure'] = {'type': type(error).__name__, 'message': str(error)[:1400]}
    (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({k: v for k, v in result.items() if k != 'commands'}, ensure_ascii=False))
    assert result['passed'], str(folder / 'result.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('review', type=Path)
    parser.add_argument('--program', type=Path, required=True)
    parser.add_argument('--distro', default='vibeflow-linux')
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf8'))
    assert review['classification'] == 'no_llm'
    for name, digest in review['sources'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    run(args.program, args.distro)
