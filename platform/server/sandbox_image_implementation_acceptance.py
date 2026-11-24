"""Durable launch provenance checks inside a real model acceptance scenario."""
import asyncio
import json

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import DurableEvent
from app.services import sandbox_image_launch as launch


async def verify(api, sid, rid):
    report = {'passed': False, 'real_llm_calls': 0, 'mock_llm_calls': 0, 'checks': []}
    engine = create_async_engine(api.database_url or 'sqlite+aiosqlite:///' + (api.root / 'test.sqlite').as_posix())
    endpoint = '/terminal/sessions/' + sid + '/image-cleanup'
    try:
        async with engine.connect() as db:
            records = list((await db.execute(select(DurableEvent.id, DurableEvent.type, DurableEvent.payload_json).where(
                DurableEvent.type.in_([launch.INTENT, 'terminal.image_cleanup_abandoned'])))).all())
        assert len(records) <= 128
        abandoned = [json.loads(raw) for _, kind, raw in records if kind == 'terminal.image_cleanup_abandoned']
        assert len(abandoned) == 1
        entries = [(identifier, raw, json.loads(raw)) for identifier, kind, raw in records if kind == launch.INTENT]
        assert len(entries) == 2
        expected = launch.implementations()
        for _, _, value in entries:
            launch.validate_implementations(value)
            assert all(value[key] == binding for key, binding in expected.items())
        report['implementations'] = expected
        report['checks'].append({'name': 'actual stored intents bind atomic host mode and exact host/native controller implementations',
                                 'kind': 'actual_infrastructure'})
        identifier, original, document = next(row for row in entries if row[2]['request_id'] == abandoned[0]['request_id'])
        changed = json.loads(original)
        changed['host_launch_sha256']['windows_process_create.py'] = '0' * 64
        try:
            launch.require_implementation(changed)
        except ValueError:
            pass
        else:
            raise AssertionError('Changed active launcher implementation accepted')
        try:
            async with engine.begin() as db:
                await db.execute(update(DurableEvent).where(DurableEvent.id == identifier).values(payload_json=json.dumps(changed)))
            await asyncio.to_thread(api.call, 'GET', endpoint, expect=409)
            await asyncio.to_thread(api.call, 'POST', endpoint, body={'recovery_id': rid, 'confirm': True}, expect=409)
            await asyncio.to_thread(api.call, 'GET', f'/fixture/{api.case_id}/validation', api_prefix=False, expect=409)
            report['checks'].append({'name': 'changed persisted host implementation revokes prior reconciliation and completion',
                                     'kind': 'protocol_fault'})
        finally:
            async with engine.begin() as db:
                await db.execute(update(DurableEvent).where(DurableEvent.id == identifier).values(payload_json=original))
        result = await asyncio.to_thread(api.call, 'GET', endpoint)
        assert result['status'] == 'completed'
        report['checks'].append({'name': 'restored exact durable intent restores completion without rerunning deletion',
                                 'kind': 'actual_infrastructure'})
        report['passed'] = True
    finally:
        await engine.dispose()
        (api.root / 'image-implementation-checks.json').write_text(json.dumps(report, indent=2), encoding='utf8')
