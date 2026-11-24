"""Disposable native Linux state for explicitly selected WSL integration tests."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


class NativeStateFixture:
    def run(self, action, *arguments):
        from app.services.owned_process import wsl_path
        result = subprocess.run(['wsl.exe', '-d', 'vibeflow-linux', '--user', 'nobody', '--exec',
            '/usr/bin/python3', '-I', '-S', wsl_path(Path(__file__).resolve()), action, *arguments],
            capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('Native test state operation failed: ' + action)
        return json.loads(result.stdout)

    def __enter__(self):
        self.root = self.run('create')['root']
        self.previous = os.environ.get('VIBEFLOW_CODEX_WSL_STATE_ROOT')
        os.environ['VIBEFLOW_CODEX_WSL_STATE_ROOT'] = self.root
        return self

    def __exit__(self, *_):
        if self.previous is None:
            os.environ.pop('VIBEFLOW_CODEX_WSL_STATE_ROOT', None)
        else:
            os.environ['VIBEFLOW_CODEX_WSL_STATE_ROOT'] = self.previous
        self.run('remove', self.root)


if __name__ == '__main__':
    if os.getuid() != 65534:
        raise ValueError('Fixture requires the non-root worker')
    if sys.argv[1:] == ['create']:
        print(json.dumps({'root': tempfile.mkdtemp(prefix='vibeflow-cli-fixture-', dir='/tmp')}))
    elif len(sys.argv) == 3 and sys.argv[1] == 'remove':
        root = Path(sys.argv[2])
        if (not re.fullmatch('/tmp/vibeflow-cli-fixture-[a-z0-9_]{8}', str(root)) or root.resolve() != root
                or root.is_symlink() or root.stat().st_uid != os.getuid()):
            raise ValueError('Cleanup escaped this test fixture')
        shutil.rmtree(root)
        print(json.dumps({'removed': True}))
    else:
        raise ValueError('Unknown native fixture operation')
