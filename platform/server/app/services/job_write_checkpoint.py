"""Bound proof that uncoordinated Job content writes have drained.

The runner calls record_drained only after callbacks, evidence, staging sync,
logs, finalizer children and private bundle creation have returned. Subsequent
alias writes still use case_write.coordinate. This is neither OS isolation nor
permission to replay a terminal/unknown attempt or to resume its staging sync.
Original owners write v1. Historical terminal recovery writes v2; validated
bundle-copy recovery writes v3. Both require original cleanup, durable history
and exact owner exit, and neither may invent an original owner's drain receipt.
"""
from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import select

from ..models import Case, Job, JobAttempt
from .case_write import CaseWriteConflict
from .durable_events import json_text, now
from .workflow_state import ExecutionConflict

SCHEMA = 'vibeflow.job-write-checkpoint.v1'
FIELDS = {'schema', 'case_id', 'job_id', 'attempt_id', 'case_prefix', 'claim_sha256',
          'state', 'process_identity_json', 'child_processes_json', 'stop', 'recorded_at'}


def claim_digest(attempt):
    if not isinstance(attempt.claim_token, str) or not attempt.claim_token:
        raise ValueError('Write checkpoint requires a claimed attempt')
    return hashlib.sha256(attempt.claim_token.encode('utf8')).hexdigest()


def validate(case, job, attempt):
    """Reject missing, malformed, rebound or incompletely stopped evidence."""
    from . import process_record, process_stop, native_child_records
    value = json.loads(attempt.write_checkpoint_json)
    if isinstance(value,dict) and value.get('schema')=='vibeflow.job-write-checkpoint.v6':
        from .cancelled_archive_recovery import validate_certificate
        return validate_certificate(case,job,attempt,value)
    if isinstance(value,dict) and value.get('schema')=='vibeflow.job-write-checkpoint.v5':
        from .job_output_finalizing import validate_certificate
        return validate_certificate(case,job,attempt,value)
    if isinstance(value,dict) and value.get('schema')=='vibeflow.job-write-checkpoint.v4':
        from .job_scope_recovery import validate_certificate
        return validate_certificate(case,job,attempt,value)
    recovered=isinstance(value,dict) and value.get('schema') in {'vibeflow.job-write-checkpoint.v2','vibeflow.job-write-checkpoint.v3'}
    if (not isinstance(value, dict) or set(value) != (FIELDS|{'recovery'} if recovered else FIELDS)
            or value['schema'] not in {SCHEMA,'vibeflow.job-write-checkpoint.v2','vibeflow.job-write-checkpoint.v3'}
            or type(value['case_id']) is not int or type(value['job_id']) is not int
            or value['case_id'] != case.id or value['job_id'] != job.id or job.case_id != case.id
            or value['attempt_id'] != attempt.id or attempt.job_id != job.id
            or value['case_prefix'] != case.s3_prefix or value['claim_sha256'] != claim_digest(attempt)
            or value['process_identity_json'] != attempt.process_identity_json
            or value['child_processes_json'] != attempt.child_processes_json):
        raise ValueError('Job write checkpoint no longer matches its owner and source')
    stamp = value['recorded_at']
    if not isinstance(stamp, str) or len(stamp) > 64 or datetime.fromisoformat(stamp).utcoffset() != timedelta(0):
        raise ValueError('Write checkpoint timestamp requires UTC')
    children = native_child_records.document(job, attempt)['children']
    if value['state'] == 'not_started':
        if json.loads(attempt.process_identity_json) != {} or children or value['stop'] is not None:
            raise ValueError('Never-started checkpoint contains a native launch')
    elif value['state'] == 'stopped':
        identity = process_record.for_attempt(json.loads(attempt.process_identity_json), job, attempt)
        stop = process_stop.validate(value['stop'], identity)
        if stop['verified'] is not True:
            raise ValueError('Parent cleanup is not verified')
        for child in children.values():
            if (child['phase'] not in {'stopped', 'failed'} or child['stop'] is None
                    or child['stop']['verified'] is not True):
                raise ValueError('Child cleanup is not verified')
    else:
        raise ValueError('Unknown Job write checkpoint state')
    persisted_stop = json.loads(attempt.output_manifest_json).get('process_stop')
    if persisted_stop is not None and persisted_stop != value['stop']:
        raise ValueError('Terminal cleanup differs from the write checkpoint')
    if recovered:
        if value['state']!='stopped':raise ValueError('Recovered history requires original physical cleanup')
        if value['schema']=='vibeflow.job-write-checkpoint.v3':
            from .publication_preparation_recovery import validate_certificate
        else:
            from .job_drain_recovery import validate_certificate
        validate_certificate(case,job,attempt,value)
    return value


async def assert_native_open_locked(db, job, attempt):
    """Under case control, prevent any new native work after a drain checkpoint."""
    if attempt.write_checkpoint_json != '{}' or 'bundle_preparation' in json.loads(attempt.output_manifest_json):
        raise ExecutionConflict('state_conflict', 'Job content writes are already closed')
    from .case_validation import assert_writable_locked
    try:
        await assert_writable_locked(db, job.case_id)
    except CaseWriteConflict as error:
        raise ExecutionConflict(error.code, str(error)) from error


async def record_drained(owner, stop, *, not_started=False):
    """Short committed checkpoint; caller has awaited every uncoordinated writer.

Only the pre-spawn preparation failure path can assert not_started. Unknown
registration/cleanup never becomes a checkpoint merely because finish ran.
Missing physical evidence leaves the attempt blocking future read validation.
"""
    from ..db import SessionLocal
    from .job_execution import _owned, attempt_event
    async with SessionLocal() as db:
        job, attempt = await _owned(db, owner)
        case = await db.get(Case, job.case_id, populate_existing=True)
        if attempt.write_checkpoint_json != '{}':
            previous = validate(case, job, attempt)
            if previous['stop'] != stop or (previous['state'] == 'not_started') != not_started:
                raise ExecutionConflict('state_conflict', 'Write drainage was recorded differently')
            return True
        if not_started:
            if attempt.phase not in {'claimed', 'cancelled'} or stop is not None:
                return False
        elif stop is None or stop.get('verified') is not True:
            return False
        value = {'schema':SCHEMA, 'case_id':case.id, 'job_id':job.id, 'attempt_id':attempt.id,
            'case_prefix':case.s3_prefix, 'claim_sha256':claim_digest(attempt),
            'state':'not_started' if not_started else 'stopped',
            'process_identity_json':attempt.process_identity_json,
            'child_processes_json':attempt.child_processes_json, 'stop':stop, 'recorded_at':now().isoformat()}
        attempt.write_checkpoint_json = json_text(value)
        try:
            validate(case, job, attempt)
        except (ValueError, TypeError, KeyError, AttributeError):
            # Leave the missing-evidence state intact, including on failed Jobs.
            await db.rollback()
            return False
        await attempt_event(db, job, attempt, 'attempt.content_drained', {'state':value['state']})
        await db.commit()
        return True


async def require_drained_locked(db, case):
    """All case Jobs, including this validator, must have no uncoordinated writer.

    Queued/unclaimed deliveries cannot write before the guarded claim. All other
    attempts, including old terminal history, require positive drain evidence.
    The case lock serializes this inspection with claim and checkpoint creation.
    """
    rows = (await db.execute(select(Job, JobAttempt).outerjoin(JobAttempt, JobAttempt.job_id == Job.id)
        .where(Job.case_id == case.id).execution_options(populate_existing=True))).all()
    for job, attempt in rows:
        requires_recovery = False
        try:
            if attempt is None:
                if job.status == 'queued':
                    continue
                requires_recovery = True
                raise ValueError('Legacy Job has no write-drain history')
            if (attempt.claim_token is None and attempt.phase in {'queued', 'cancelled'}
                    and job.status in {'queued', 'cancelled'} and attempt.write_checkpoint_json == '{}'
                    and json.loads(attempt.process_identity_json) == {}
                    and json.loads(attempt.child_processes_json) == {}):
                continue
            requires_recovery = (job.status in {'succeeded','failed','cancelled','interrupted'}
                or attempt.write_checkpoint_json != '{}')
            validate(case, job, attempt)
            requires_recovery = False
            if job.status == 'succeeded':
                from .job_execution import completion_visibility
                visibility = completion_visibility(attempt)
                if visibility in {'pending', 'invalid'}:
                    # Finish the earlier publisher's coordinated aliases before
                    # a new validator can exclude those outstanding writes.
                    requires_recovery = visibility == 'invalid'
                    raise ValueError('Publication aliases have not settled')
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            conflict = CaseWriteConflict('validation_job_busy',
                '算例仍有作业写入或未核验的收尾状态，暂不能校验来源')
            conflict.requires_recovery = requires_recovery
            raise conflict from error
