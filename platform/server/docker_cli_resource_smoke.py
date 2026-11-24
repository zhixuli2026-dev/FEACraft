"""Observe kernel-enforced CPU, PID and memory limits in the Docker CLI profile."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import re
import subprocess

from app.services.docker_cli_profile import container_flags


CPU_AND_PIDS = '''
import errno
import json
from pathlib import Path
import subprocess
import sys

root = Path('/sys/fs/cgroup')
stats = root/'cpu.stat' if (root/'cpu.stat').is_file() else root/'cpu/cpu.stat'
def throttled():
    return int(dict(line.split() for line in stats.read_text().splitlines())['nr_throttled'])

before = throttled()
spin = 'import time; end=time.monotonic()+1.5\\nwhile time.monotonic()<end: pass'
busy = [subprocess.Popen([sys.executable,'-I','-c',spin]) for _ in range(2)]
for process in busy:
    assert process.wait(timeout=8) == 0
periods = throttled()-before
assert periods > 0

children = []
count = None
try:
    for _ in range(64):
        try:
            children.append(subprocess.Popen(['/bin/sleep','10']))
        except OSError as error:
            assert error.errno == errno.EAGAIN
            current = root/'pids.current' if (root/'pids.current').is_file() else root/'pids/pids.current'
            count = int(current.read_text())
            break
finally:
    for child in children:
        child.terminate()
    for child in children:
        child.wait(timeout=3)
assert count == 64
print(json.dumps({'cpu_throttled_periods':periods, 'pids_at_denial':count, 'children_joined':len(children)}))
'''


def docker(*args, expected=0):
    command = subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf8', timeout=45)
    if command.returncode != expected:
        raise RuntimeError(f'Docker {args[0]} returned {command.returncode}: {command.stderr[:300]}')
    return command.stdout.strip()


def verify(image, output):
    original = set(docker('ps','--no-trunc','--format','{{.ID}} {{.Names}}').splitlines())
    ident = docker('image','inspect','--format','{{.Id}}',image)
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', ident)
    result = {'image':ident,'real_llm_calls':0,'mock_llm_calls':0,'passed':False}
    try:
        result['cpu_and_pids'] = json.loads(docker('run','--rm',*container_flags(), '--entrypoint','python',
            ident,'-I','-c',CPU_AND_PIDS))
        with ExitStack() as stack:
            oversized = docker('create', *container_flags(), '--entrypoint','python', ident,
                               '-I','-c','buffer=bytearray(800*1024**2); print(len(buffer))')
            assert re.fullmatch(r'[0-9a-f]{64}', oversized)
            stack.callback(docker,'rm','--force',oversized)
            docker('start','--attach',oversized,expected=137)
            record = json.loads(docker('inspect','--format','{{json .State}}',oversized))
            result['memory'] = {'oom_killed':record['OOMKilled'], 'running':record['Running'], 'exit_code':record['ExitCode']}
            assert result['memory'] == {'oom_killed':True,'running':False,'exit_code':137}
        result['passed'] = True
    finally:
        result['original_containers_match'] = original == set(docker('ps','--no-trunc','--format','{{.ID}} {{.Names}}').splitlines())
        result['passed'] = result['passed'] and result['original_containers_match']
        output.write_text(json.dumps(result,indent=2)+'\n', encoding='utf8')
        print('CLI_RESOURCE_EVIDENCE='+str(output), flush=True)
    assert result['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--image', default='vibeflow/api:cli-discovery-20261118')
    args = parser.parse_args()
    verify(args.image, args.output)
