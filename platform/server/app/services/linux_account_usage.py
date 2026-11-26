"""Read account usage from the committed, verified Linux execution receipts."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select, tuple_

from ..models import (AcpThread, AcpTurn, DurableEvent, JobAttempt, NativeCliSession, NativeRequest,
                      NativeSession, NativeVizSession)
from . import (acp_execution, codex_records, docker_cli_usage, job_delivery, linux_gate_compute,
               native_request_records, process_record, process_stop, terminal_records, viz_records)
from .linux_account_capacity import JOURNAL_ERRORS, REQUEST_KINDS, possibly_linux


@dataclass(frozen=True)
class Observation:
    stopped: bool
    cpu_ns: int | None = None
    memory_bytes: int | None = None


@dataclass
class Usage:
    scopes: int = 0
    verified_stops: int = 0
    cpu_samples: int = 0
    memory_samples: int = 0
    cpu_used_ns: int | None = None
    max_execution_memory_peak_bytes: int | None = None

    def record(self, observation):
        self.scopes += 1
        self.verified_stops += observation.stopped
        if observation.cpu_ns is not None:
            self.cpu_samples += 1
            self.cpu_used_ns = (self.cpu_used_ns or 0) + observation.cpu_ns
        if observation.memory_bytes is not None:
            self.memory_samples += 1
            self.max_execution_memory_peak_bytes = max(self.max_execution_memory_peak_bytes or 0,
                                                        observation.memory_bytes)


def _native_stop(stop):
    if not stop or not stop['verified']:
        return Observation(False)
    usage = (stop.get('linux_compute_usage') or {}).get('usage', {})
    return Observation(True, usage.get('cpu_used_ns'), usage.get('memory_peak_bytes'))


def _terminal(row):
    if possibly_linux(row.owner_identity_json):
        journal, stop = terminal_records.document(row)
        if journal['root'] is not None:
            yield _native_stop(stop)


def _visualization(row):
    if possibly_linux(row.owner_identity_json):
        _, root, _, stop = viz_records.document(row)
        if root is not None:
            yield _native_stop(stop)


def _native_request(row):
    if not possibly_linux(row.owner_identity_json):
        return
    children = native_request_records.document(row)['children'].values()
    for child in children:
        if child['identity'] is not None or child['phase'] in {'preparing', 'running'}:
            yield _native_stop(child['stop'])


def _container_stop(stop):
    if not stop or stop.get('verified') is not True or stop.get('scope_empty') is not True:
        return Observation(False)
    sample = stop.get('resource_usage')
    if sample is None:
        return Observation(True)
    try:
        docker_cli_usage.validate(sample, stop['containers']['worker']['id'])
    except JOURNAL_ERRORS:
        return Observation(True)  # Cleanup and resource samples have separate proofs.
    return Observation(True, sample['cpu_usage_usec'] * 1000, sample['memory_peak_bytes'])


def _codex(row):
    _, stop = codex_records.document(row)
    if not stop or stop['schema'] != codex_records.NOT_STARTED:
        yield _container_stop(stop)


def _acp(row):
    if row.phase in {'failed', 'cancelled'} and row.process_json is None and row.stop_json is None:
        return
    yield _container_stop(acp_execution.cleanup_evidence(row))


def _job_observation(job, attempt, events):
    process = process_record.public(job, attempt)
    if process is not None and process['host']['platform'] != 'linux':
        return None
    stop = process_stop.public(job, attempt)
    if not stop or not stop['verified']:
        return Observation(False)
    if len(events) != 1:
        return Observation(True)
    try:
        policy = json.loads(attempt.input_manifest_json)['linux_compute_policy']
        receipt = linux_gate_compute.validate_report(json.loads(events[0].payload_json), process['launch_nonce'], policy)
    except JOURNAL_ERRORS:
        return Observation(True)
    return _native_stop({**stop, 'linux_compute_usage': receipt})


def _window(model, start, end):
    return model.ended_at >= start, model.ended_at < end


async def _jobs(db, user_id, start, end):
    query = job_delivery.jobs_requested_by(user_id, latest_only=False).where(
        *_window(JobAttempt, start, end), JobAttempt.process_identity_json != '{}')
    rows = await db.stream(query.execution_options(yield_per=64))
    try:
        async for page in rows.partitions():
            journal = defaultdict(list)
            keys = [(case.id, attempt.id) for case, _, attempt in page]
            events = await db.scalars(select(DurableEvent).where(
                tuple_(DurableEvent.case_id, DurableEvent.attempt_id).in_(keys),
                DurableEvent.type.in_((job_delivery.EVENT, linux_gate_compute.CONTROL))))
            for event in events:
                journal[event.attempt_id, event.type].append(event)
            for _, job, attempt in page:
                if not possibly_linux(attempt.worker_identity_json, worker=True):
                    continue
                try:
                    delivery = job_delivery.validate_intent(job, attempt, journal[attempt.id, job_delivery.EVENT])
                except job_delivery.ExecutionConflict:
                    continue  # Damaged delivery cannot establish the charged account.
                if delivery is None or delivery['requested_by'] == user_id:
                    observation = _job_observation(job, attempt, journal[attempt.id, linux_gate_compute.CONTROL])
                    if observation is not None:
                        yield observation
    finally:
        await rows.close()


async def summarize(db, user_id, days):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    native = select(NativeRequest).where(NativeRequest.kind.in_(REQUEST_KINDS))
    sources = (
        ('terminal', NativeSession, select(NativeSession), _terminal),
        ('visualization', NativeVizSession, select(NativeVizSession), _visualization),
        ('agent_shell', NativeRequest, native.where(NativeRequest.kind == 'agent.shell'), _native_request),
        ('native_document', NativeRequest, native.where(NativeRequest.kind != 'agent.shell'), _native_request),
        ('codex', NativeCliSession, select(NativeCliSession).where(NativeCliSession.runtime == 'docker'), _codex),
        ('acp', AcpTurn, select(AcpTurn).join(AcpThread, AcpThread.id == AcpTurn.thread_id)
         .where(AcpThread.runtime == 'linux', AcpThread.created_by == user_id), _acp),
    )
    total = Usage()
    per_kind = {}
    for kind, model, query, observe in sources:
        usage = per_kind[kind] = Usage()
        rows = await db.stream_scalars(query.where(model.created_by == user_id, *_window(model, start, end))
                                       .execution_options(yield_per=64))
        try:
            async for row in rows:
                try:
                    observations = tuple(observe(row))
                except JOURNAL_ERRORS:
                    observations = (Observation(False),)
                for observation in observations:
                    usage.record(observation)
                    total.record(observation)
        finally:
            await rows.close()
    jobs = per_kind['job'] = Usage()
    async for observation in _jobs(db, user_id, start, end):
        jobs.record(observation)
        total.record(observation)
    return {'schema': 'vibeflow.linux-execution-usage.v1', 'from': start, 'until': end,
            'total': total, 'by_kind': per_kind}
