"""Remove stopped LaTeX compilation copies from their sealed attempt directory."""
import json
import re
import stat

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .. import storage
from ..db import SessionLocal
from ..models import Case, DurableEvent, Job, JobAttempt
from . import attempt_workspace, job_execution, job_worker_identity, job_write_checkpoint, request_deadline
from .case_write import bounded_case_read
from .durable_events import lock_case_execution
from .fs_directory import retry_sharing_violation
from .process_identity_native import host_id
from .staging_cleanup import remove_directory


COMMAND = 'Registered latexmk'
ABANDONED = ('cancelled', 'failed', 'interrupted')
EVENT = 'attempt.latex_compile_cleanup_completed'
COMPILE_DIRECTORY = re.compile(r'\.latex-compile-[a-f0-9]{32}')


def recovery_candidates():
    receipt = select(DurableEvent.id).where(DurableEvent.case_id == Job.case_id,
        DurableEvent.attempt_id == JobAttempt.id, DurableEvent.type == EVENT).exists()
    phases = (JobAttempt.phase == Job.status) | ((Job.status == 'cancelled') & (JobAttempt.phase == 'cancelling'))
    return ((Job.command == COMMAND) & Job.status.in_(ABANDONED) & phases
            & (JobAttempt.write_checkpoint_json != '{}') & ~receipt)


def remove_copies(prefix, manifest):
    deadline = request_deadline.current()
    root = storage._fs_path(prefix, '.')
    relative = f"{attempt_workspace.PREFIX}/{manifest['attempt_id']}"
    parent = attempt_workspace.checked_path(root, relative)
    disk_manifest = attempt_workspace.checked_path(root, relative + '/manifest.json')
    if json.loads(disk_manifest.read_text(encoding='utf-8')) != manifest:
        raise ValueError('LaTeX cleanup copy belongs to a different input seal')
    removed = []
    for path in parent.iterdir():
        deadline.check()
        if not COMPILE_DIRECTORY.fullmatch(path.name):
            continue
        try:
            child = attempt_workspace.checked_path(root, relative + '/' + path.name)
            identity = child.lstat()
            if not stat.S_ISDIR(identity.st_mode):
                raise ValueError('LaTeX cleanup requires a private directory')
            retry_sharing_violation(lambda: remove_directory(child, deadline.check, expected=identity))
        except FileNotFoundError:
            # A concurrent recovery may already have removed this copy.
            if path.exists() or path.is_symlink():
                raise
        removed.append(path.name)
    return removed


async def retire(job_id, *, owner=None):
    """Drain outside SQL, then record the cleanup on this attempt."""
    try:
        async with SessionLocal() as db:
            job = await db.get(Job, job_id)
            if job is None or job.command != COMMAND:
                return
            if owner is not None:
                job, attempt = await job_execution._owned(db, owner)
            else:
                attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id)
                    .order_by(JobAttempt.number.desc()).limit(1))
                if attempt is None or job.status not in ABANDONED:
                    return
                if attempt.phase != job.status and (job.status, attempt.phase) != ('cancelled', 'cancelling'):
                    return
            case = await db.get(Case, job.case_id)
            manifest = attempt_workspace.require_sealed(job, attempt)
            attempt_workspace.validate_identity(manifest)
            spec = json.loads(attempt.input_manifest_json)
            if (spec.get('handler', {}).get('key') != 'latex.build'
                    or spec['execution_spec']['case_prefix'] != case.s3_prefix
                    or job_worker_identity.document(job, attempt)['process']['host_id'] != host_id()):
                return
            job_write_checkpoint.validate(case, job, attempt)
            attempt_id, case_id, prefix = attempt.id, case.id, case.s3_prefix
            checkpoint = attempt.write_checkpoint_json
            input_manifest = attempt.input_manifest_json
            present = select(DurableEvent.id).where(DurableEvent.case_id == case_id,
                DurableEvent.attempt_id == attempt_id, DurableEvent.type == EVENT).limit(1)
            if await db.scalar(present) is not None:
                return
        removed = await bounded_case_read(remove_copies, prefix, manifest,
            code='latex_compile_cleanup', label='LaTeX 编译暂存清理', seconds=request_deadline.STAGING_CLEANUP_SECONDS)
        async with SessionLocal() as db:
            await lock_case_execution(db, case_id)
            case = await db.get(Case, case_id)
            job = await db.get(Job, job_id)
            attempt = await db.get(JobAttempt, attempt_id)
            if (case is None or case.s3_prefix != prefix or job is None or job.case_id != case_id
                    or attempt is None or attempt.job_id != job_id or attempt.write_checkpoint_json != checkpoint
                    or attempt.input_manifest_json != input_manifest or await db.scalar(present) is not None):
                return
            await job_execution.attempt_event(db, job, attempt, EVENT, {'removed': removed})
            await db.commit()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException, SQLAlchemyError,
            job_execution.ExecutionConflict) as error:
        print(f'[WARN] Job #{job_id} LaTeX compile cleanup deferred: {type(error).__name__}')
