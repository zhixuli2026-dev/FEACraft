"""Inspect actual business scope records and corrupt only durable proof, no LLM."""
import asyncio
import json
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import NativeRequest, DurableEvent
from app.services import sandbox_image_scope as scopes
from app.services.native_scope_control import control

FIXTURE = '''
from app.services import sandbox_image_launch as _scope_launch
_scope_observed = _scope_launch.observed
async def _observe_live_scope(owner, child_id, preparation):
 await _scope_observed(owner, child_id, preparation)
 if preparation['stage'] == 'native_ready':
  from app.services import sandbox_image_scope as scopes
  from app.services.native_scope_control import control
  from app.db import SessionLocal
  from app.models import NativeRequest
  async with SessionLocal() as db:
   row = await db.get(NativeRequest, owner.id)
   child = scopes.records.document(row)['children'][child_id]
   evidence = await scopes.evidence(db, row, child)
  receipt = await control(evidence['planned']['distribution'], 'observe', {'scope': evidence['allocated']['result']})
  if receipt['result']['state'] != 'populated':
   raise ValueError('Actual business gate did not populate its selected cgroup')
  with (root / 'live-image-scopes.jsonl').open('a', encoding='utf8') as stream:
   stream.write(json.dumps({'request_id': owner.id, 'child_id': child_id, 'preparation': preparation, 'receipt': receipt}) + '\\n')
_scope_launch.observed = _observe_live_scope
'''


async def verify(api, sid, rid):
    report = {'passed': False, 'mock_llm_calls': 0, 'checks': [], 'actual_scope_observations': []}
    engine = create_async_engine(api.database_url or 'sqlite+aiosqlite:///' + (api.root / 'test.sqlite').as_posix())
    from sqlalchemy.ext.asyncio import async_sessionmaker
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    endpoint = '/terminal/sessions/' + sid + '/image-cleanup'
    try:
        async with sessions() as db:
            rows = list(await db.scalars(select(NativeRequest).where(NativeRequest.kind == 'terminal.cleanup')))
            assert len(rows) == 2
            histories = []
            for row in rows:
                child = next(iter(scopes.records.document(row)['children'].values()))
                value = await scopes.evidence(db, row, child)
                assert value is not None and all(value[key] is not None for key in scopes.STAGES)
                await scopes.require_closed(db, row)
                histories.append(value)
            report['histories'] = histories
            report['checks'].append({'name': 'actual failed preparation and successful retry persist all five bound scope stages',
                                     'kind': 'actual_infrastructure'})
            identifier = scopes.event_id(child['id'], 'closed')
            original = (await db.get(DurableEvent, identifier)).payload_json
        for value in histories:
            observed = await control(value['planned']['distribution'], 'observe', {'scope': value['allocated']['result']})
            assert observed['result']['state'] == 'absent'
            report['actual_scope_observations'].append(observed)
        populated = [json.loads(line) for line in (api.root / 'live-image-scopes.jsonl').read_text(encoding='utf8').splitlines()]
        assert len(populated) == 1 and populated[0]['receipt']['result']['state'] == 'populated'
        assert any(value['allocated']['result'] == populated[0]['receipt']['result']['scope'] for value in histories)
        report['actual_live_scope'] = populated[0]
        report['checks'].append({'name': 'actual successful business gate populates its committed cgroup before payload release',
                                 'kind': 'actual_infrastructure'})
        report['checks'].append({'name': 'actual kernel confirms both business allocations revoked after cancellation and normal finish',
                                 'kind': 'actual_infrastructure'})
        changed = json.loads(original)
        changed['value']['receipt']['result']['state'] = 'populated'
        try:
            async with engine.begin() as db:
                await db.execute(update(DurableEvent).where(DurableEvent.id == identifier).values(payload_json=json.dumps(changed)))
            await asyncio.to_thread(api.call, 'GET', endpoint, expect=409)
            await asyncio.to_thread(api.call, 'POST', endpoint, body={'recovery_id': rid, 'confirm': True}, expect=409)
            await asyncio.to_thread(api.call, 'GET', f'/fixture/{api.case_id}/validation', api_prefix=False, expect=409)
            report['checks'].append({'name': 'corrupt revocation blocks completion retry and case admission', 'kind': 'protocol_fault'})
        finally:
            async with engine.begin() as db:
                await db.execute(update(DurableEvent).where(DurableEvent.id == identifier).values(payload_json=original))
        assert (await asyncio.to_thread(api.call, 'GET', endpoint))['status'] == 'completed'
        report['checks'].append({'name': 'restoring exact revocation restores idempotent completion', 'kind': 'actual_infrastructure'})
        report['passed'] = True
    finally:
        await engine.dispose()
        (api.root / 'image-scope-checks.json').write_text(json.dumps(report, indent=2), encoding='utf8')
