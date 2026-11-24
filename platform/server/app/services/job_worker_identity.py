"""Read-only worker birth evidence bound to the committed exclusive claim."""
import hashlib
import json
import os
import sys
from pathlib import Path

from . import process_identity_native as identity

SCHEMA = 'vibeflow.job-worker.v1'


def capture(job, attempt, worker_id, token):
    return {'schema': SCHEMA, 'case_id': job.case_id, 'job_id': job.id,
            'attempt_id': attempt.id, 'worker_id': worker_id,
            'claim_sha256': hashlib.sha256(token.encode()).hexdigest(),
            'process': identity.observe(os.getpid())}


def document(job, attempt):
    value = json.loads(attempt.worker_identity_json)
    if (not isinstance(value, dict) or set(value) != {
            'schema', 'case_id', 'job_id', 'attempt_id', 'worker_id', 'claim_sha256', 'process'}
            or value['schema'] != SCHEMA or type(value['case_id']) is not int
            or type(value['job_id']) is not int or value['case_id'] != job.case_id
            or value['job_id'] != job.id or attempt.job_id != job.id
            or value['attempt_id'] != attempt.id or not attempt.worker_id
            or value['worker_id'] != attempt.worker_id or not attempt.claim_token
            or value['claim_sha256'] != hashlib.sha256(attempt.claim_token.encode()).hexdigest()):
        raise ValueError('Invalid Job worker ownership evidence')
    identity.validate(value['process'])
    return value


def inspect(job, attempt):
    try:
        value = document(job, attempt)
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'state': 'unverified', 'reason': 'invalid_or_legacy_worker_identity'}
    process = value['process']
    if sys.platform.startswith('linux') and process['platform'] == 'linux':
        # capture() saves the PID as seen by the worker itself. A sibling
        # container can have an unrelated process, or no process, at that PID.
        try:
            if process['host_id'] != identity.host_id():
                return {'state': 'mismatch', 'reason': 'host_changed'}
            if process['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
                return {'state': 'mismatch', 'reason': 'boot_changed'}
            if process['pid_namespace'] != str(os.stat('/proc/self/ns/pid').st_ino):
                return {'state': 'unverified', 'reason': 'different_worker_pid_namespace'}
        except (OSError, ValueError):
            return {'state': 'unverified', 'reason': 'identity_unavailable'}
    return identity.probe(process)
