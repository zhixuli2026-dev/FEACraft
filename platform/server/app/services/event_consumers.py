"""Idempotent derived-state consumers run before an outbox delivery is acked."""
import asyncio

from .async_ops import finish_before_cancel
from . import indexsvc


def requires_files(event, payload):
    return (event.type in {'files.changed', 'artifact.published', 'artifact.invalidated',
                             'attempt.completion_visible'} or
               event.type == 'jobs.updated' and payload.get('status') in
               {'succeeded', 'failed', 'cancelled', 'interrupted'}) and indexsvc.enabled(event.case_id)


async def consume(event, prefix, payload):
    if requires_files(event, payload):
        # Thread work must drain even on timeout/cancellation. A competing
        # dispatcher waits for the same SQLite transaction, then sees its receipt.
        await finish_before_cancel(asyncio.to_thread(indexsvc.consume_event,
            event.case_id, prefix, event.id, event.type, payload))
