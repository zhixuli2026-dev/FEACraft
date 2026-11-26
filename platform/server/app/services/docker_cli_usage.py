"""Read Linux cgroup counters from an owned CLI worker before it stops."""
import asyncio
import json
import re
import subprocess

from ..config import get_settings


SCHEMA = 'vibeflow.docker-cli-usage.v1'
COUNTERS = """
import json
from pathlib import Path

root = Path('/sys/fs/cgroup')

def number(path):
    return int((root / path).read_text())

def fields(path):
    return dict(line.split() for line in (root / path).read_text().splitlines())

if (root / 'cgroup.controllers').is_file():
    stats = fields('cpu.stat')
    source = 'cgroup-v2'
    cpu = int(stats['usage_usec'])
    peak = number('memory.peak') if (root / 'memory.peak').is_file() else None
    denied = int(fields('pids.events')['max'])
else:
    stats = fields('cpu/cpu.stat')
    source = 'cgroup-v1'
    cpu = number('cpuacct/cpuacct.usage') // 1000
    peak = number('memory/memory.max_usage_in_bytes')
    denied = int(fields('pids/pids.events')['max'])

print(json.dumps({'source': source, 'cpu_usage_usec': cpu, 'memory_peak_bytes': peak,
                  'cpu_throttled_periods': int(stats['nr_throttled']), 'pid_limit_hits': denied}))
"""


def validate(value, worker_id):
    if (not isinstance(value, dict)
            or set(value) != {'schema', 'container_id', 'source', 'cpu_usage_usec', 'memory_peak_bytes',
                              'cpu_throttled_periods', 'pid_limit_hits'}
            or value['schema'] != SCHEMA or not isinstance(worker_id, str)
            or not re.fullmatch(r'[0-9a-f]{64}', worker_id) or value['container_id'] != worker_id
            or value['source'] not in {'cgroup-v1', 'cgroup-v2'}):
        raise ValueError('Invalid Docker CLI resource sample')
    numbers = [value[key] for key in ('cpu_usage_usec', 'cpu_throttled_periods', 'pid_limit_hits')]
    if value['source'] == 'cgroup-v1' or value['memory_peak_bytes'] is not None:
        numbers.append(value['memory_peak_bytes'])
    if any(type(number) is not int or number < 0 for number in numbers):
        raise ValueError('Invalid Docker CLI resource counter')


def _read(worker_id):
    if not re.fullmatch(r'[0-9a-f]{64}', worker_id):
        raise ValueError('Invalid Docker CLI worker ID')
    try:
        result = subprocess.run([get_settings().docker_cli_client, 'exec', worker_id, 'python', '-I', '-c', COUNTERS],
                                capture_output=True, timeout=3, check=True)
        value = {'schema': SCHEMA, 'container_id': worker_id, **json.loads(result.stdout)}
        validate(value, worker_id)
    except (subprocess.SubprocessError, OSError, ValueError, TypeError):
        # The worker may already have exited or unavailable controllers may reject the read.
        return None
    return value


async def sample(worker_id):
    return await asyncio.to_thread(_read, worker_id)
