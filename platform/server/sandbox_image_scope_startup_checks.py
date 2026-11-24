"""Real API exits at durable scope boundaries; no model/CLI inference substitute."""
import asyncio
import json
import os
from pathlib import Path
import subprocess

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import NativeRequest, DurableEvent
from app.services import sandbox_image_scope as scopes, process_identity_native as identity
from app.services.native_scope_control import control
from app.services.owned_process import probe_native

FIXTURE = '''
import os,json
from pathlib import Path
from app.services import sandbox_image_scope as _scopes, sandbox_image_launch as _launch, process_identity_native as _identity
def _scope_fault(stage, owner, child_id, receipt=None):
 path = Path('scope-startup-fault.json')
 if path.exists() and json.loads(path.read_text())['stage'] == stage:
  path.unlink()
  with Path('scope-startup-deaths.jsonl').open('a', encoding='utf8') as stream:
   stream.write(json.dumps({'stage':stage,'request_id':owner.id,'child_id':child_id,
    'owner':_identity.observe(os.getpid()),'actual_uncommitted_receipt':receipt})+'\\n')
   stream.flush();os.fsync(stream.fileno())
  os._exit(81)
_plan = _scopes.plan
async def _planned(owner, child_id, request):
 await _plan(owner, child_id, request)
 _scope_fault('planned', owner, child_id)
_scopes.plan = _planned
_reserve = _launch.reserve
async def _prepared(owner, child_id, process, **kwargs):
 await _reserve(owner, child_id, process, **kwargs)
 if kwargs.get('scope_preparation') is not None:
  _scope_fault('prepared', owner, child_id)
_launch.reserve = _prepared
_allocated = _scopes.allocated
async def _allocation(owner, child_id, receipt):
 _scope_fault('before_allocated', owner, child_id, receipt)
 await _allocated(owner, child_id, receipt)
 _scope_fault('allocated', owner, child_id, receipt)
_scopes.allocated = _allocation
_admit = _scopes.admit
async def _admitted(owner, child_id, **kwargs):
 await _admit(owner, child_id, **kwargs)
 _scope_fault('admitted', owner, child_id)
_scopes.admit = _admitted
'''


async def after_retirement(api, sid, rid, proof, continuation=None, *, admitted=False):
    report = {'passed': False, 'llm_calls': 0, 'mock_llm_calls': 0, 'checks': [], 'crashes': [],
              'admitted_boundary': admitted, 'database': 'postgresql' if api.database_url else 'sqlite'}
    engine = create_async_engine(api.database_url or 'sqlite+aiosqlite:///' + (api.root / 'test.sqlite').as_posix())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    endpoint = '/terminal/sessions/' + sid + '/image-cleanup'
    body = {'recovery_id': rid, 'confirm': True}
    async def call(method, path, **kwargs):
        return await asyncio.to_thread(api.call, method, path, **kwargs)
    def record(name, kind='actual_infrastructure'):
        report['checks'].append({'name': name, 'kind': kind})
        print('SCOPE_STARTUP_PASS ' + name, flush=True)
    async def state(request_id):
        async with sessions() as db:
            row = await db.get(NativeRequest, request_id)
            child = next(iter(scopes.records.document(row)['children'].values()))
            return row.phase, child, await scopes.evidence(db, row, child)
    async def restart():
        api.proc = subprocess.Popen(api.proc.args, cwd=api.root, env=api.env, stdout=api.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        async with asyncio.timeout(40):
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET', '/api/health', api_prefix=False)).get('ok'):
                        return
                except OSError:
                    pass
                await asyncio.sleep(.1)
    try:
        async with sessions() as db:
            selected = json.loads((await db.get(NativeRequest, rid)).profile_json)
        image = Path(selected['image_root']) / 'volume.ext4'
        archive = Path(selected['root']) / 'recovery.zip'
        archive_bytes = archive.read_bytes()
        for stage in (('admitted',) if admitted else ('planned', 'prepared', 'before_allocated', 'allocated')):
            (api.root / 'scope-startup-fault.json').write_text(json.dumps({'stage': stage}))
            try:
                await call('POST', endpoint, body=body)
                raise AssertionError('Actual API did not exit at scope boundary')
            except (OSError, ConnectionError):
                pass
            assert await asyncio.to_thread(api.proc.wait, 15) == 81
            death = json.loads((api.root / 'scope-startup-deaths.jsonl').read_text(encoding='utf8').splitlines()[-1])
            assert death['stage'] == stage
            phase, child, value = await state(death['request_id'])
            assert phase == 'active' and child['phase'] == 'preparing' and child['identity'] is None and child['stop'] is None
            assert value['planned'] is not None and value['closed'] is None
            assert (value['admitted'] is not None) == admitted
            assert (value['prepared'] is not None) == (stage != 'planned')
            assert (value['allocated'] is not None) == (stage in {'allocated', 'admitted'})
            async with asyncio.timeout(45):
                while True:
                    exits = {'owner': identity.probe(death['owner'])}
                    if value['prepared'] is not None:
                        prepared = value['prepared']
                        exits['control_host'] = identity.probe(prepared['host']['host'])
                        exits['control_native'] = await probe_native(value['planned']['distribution'], prepared['native']['controller'])
                    if all(observed['state'] == 'exited' for observed in exits.values()):
                        break
                    await asyncio.sleep(.1)
            death['exit_observations'] = exits
            if death['actual_uncommitted_receipt'] is not None:
                descriptor = death['actual_uncommitted_receipt']['result']
                actual = await control(value['planned']['distribution'], 'observe', {'scope': descriptor})
                assert actual['result']['state'] == 'empty'
                death['actual_scope_before_retry'] = actual
            report['crashes'].append(death)
            await restart()
            assert (await call('GET', endpoint))['status'] == 'pending'
            assert (await state(death['request_id']))[2]['closed'] is None, 'GET must not perform recovery'
            assert image.exists() and archive.read_bytes() == archive_bytes
            record('actual_' + stage + '_API_exit_preserves_image_and_unregistered_child')
        if admitted:
            done = await call('POST', endpoint, body=body)
            assert done['status'] == 'completed' and done['deleted_this_request']
            phase, child, value = await state(death['request_id'])
            assert phase == 'failed' and value['closed'] is not None
            assert child['identity'] is None and child['stop'] is None and not image.exists()
            assert value['closed']['guardian']['receipt']['membership']['total'] >= 1
            assert value['closed']['guardian']['receipt']['verified']
            native = value['admitted']['native_preflight']['native']
            assert (await probe_native(native['distribution'],native['process']))['state'] == 'exited'
            async with sessions() as db:
                assert len(list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type == 'terminal.image_cleanup_abandoned')))) == 1
            report['histories'] = {'admitted': value}
            record('committed_guarded_admission_recovers_without_invented_business_birth')
        else:
            done = await call('POST', endpoint, body=body)
            assert done['status'] == 'completed' and done['deleted_this_request'] and not image.exists()
            histories = {}
            for death in report['crashes']:
                phase, child, value = await state(death['request_id'])
                assert phase == 'failed' and child['phase'] == 'preparing' and child['identity'] is None and child['stop'] is None
                assert value['admitted'] is None and value['closed'] is not None
                receipt = value['closed']['receipt']
                if death['stage'] == 'planned':
                    assert receipt is None
                else:
                    assert receipt['request']['operation'] == ('retire' if death['stage'] == 'allocated' else 'abandon')
                    assert receipt['result']['state'] == 'absent'
                    if death['stage'] in {'prepared', 'before_allocated'}:
                        assert receipt['result']['removed_this_call'] == (death['stage'] == 'before_allocated')
                histories[death['stage']] = value
            report['histories'] = histories
            async with sessions() as db:
                witnesses = list(await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.type == 'terminal.image_cleanup_abandoned')))
                assert len(witnesses) == 4
                assert all(json.loads(raw)['normal_stop_receipt_invented'] is False for raw in witnesses)
            await call('GET', f'/fixture/{api.case_id}/validation', api_prefix=False)
            await call('GET', f'/fixture/{api.case_id}/admin', api_prefix=False)
            record('four_unadmitted_requests_reconcile_without_invented_business_birth_or_stop')
            # Downgrading only the plan cannot turn mixed versions into legacy evidence.
            event_id = scopes.event_id(report['crashes'][0]['child_id'], 'planned')
            async with sessions() as db:
                original = (await db.get(DurableEvent, event_id)).payload_json
            changed = json.loads(original); changed['schema'] = scopes.SCHEMA
            try:
                async with engine.begin() as db:
                    await db.execute(update(DurableEvent).where(DurableEvent.id == event_id).values(payload_json=json.dumps(changed)))
                await call('GET', endpoint, expect=409)
                await call('POST', endpoint, body=body, expect=409)
                await call('GET', f'/fixture/{api.case_id}/validation', api_prefix=False, expect=409)
                record('schema_downgrade_revokes_negative_admission_proof', 'protocol_fault')
            finally:
                async with engine.begin() as db:
                    await db.execute(update(DurableEvent).where(DurableEvent.id == event_id).values(payload_json=original))
            assert await call('GET', endpoint) == done
            assert archive.read_bytes() == archive_bytes
        if continuation:
            report['llm_calls'] = await continuation(api, sid, rid, proof)
            record('real_model_reads_retained_snapshot_after_actual_scope_boundary_exits', 'real_llm')
        report['passed'] = True
        return report['llm_calls']
    finally:
        await engine.dispose()
        trace = api.root / 'upstream.jsonl'
        if trace.exists():
            report['llm_calls'] = sum(item['kind'] == 'http' and item['path'].endswith('/chat/completions')
                                      for item in map(json.loads, trace.read_text(encoding='utf8').splitlines()))
        (api.root / 'image-scope-startup-checks.json').write_text(json.dumps(report, indent=2), encoding='utf8')
