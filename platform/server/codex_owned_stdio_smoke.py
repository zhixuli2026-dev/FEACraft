"""Native stdio boundaries and real process retirement, without model credentials."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from app.services import process_identity_native as identity, process_stop
from app.services.codex_environment import create_isolated_codex_environment
from app.services.codex_stdio import CodexStdioClient, start_codex_stdio
from app.services.codex_turn import run_codex_turn

FIXTURE = r'''
import json,os,subprocess,sys,time
from pathlib import Path
mode=sys.argv[1];root=Path(sys.argv[2]);root.joinpath('executed').write_text('yes')
def send(value): print(json.dumps(value,ensure_ascii=False),flush=True)
for line in sys.stdin:
    request=json.loads(line);method=request.get('method');rid=request.get('id');params=request.get('params',{})
    if method=='initialize':
        if mode=='bad_initialize': result={}
        else: result={'userAgent':'fixture/1'}
    elif method=='initialized':
        if mode in {'blocked','detached'}:
            if mode=='detached':
                options={'creationflags':subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
                leaf=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
                root.joinpath('leaf.pid').write_text(str(leaf.pid))
            root.joinpath('blocked').write_text('yes');time.sleep(90)
        continue
    elif method=='echo':
        sys.stderr.write('diagnostic-noise\n' * 3000);sys.stderr.flush()
        result=params
    elif method=='eof': raise SystemExit(7)
    elif method=='invalid': print('not-json',flush=True);continue
    elif method=='too_large': print('x'*(1024*1024+1),flush=True);continue
    elif method=='flood':
        for i in range(300): send({'method':'fixture/event','params':{'i':i}})
        result={}
    elif method=='flood_bytes':
        for i in range(12): send({'method':'fixture/event','params':{'text':'a'*500000}})
        result={}
    elif method=='approval':
        send({'id':'approval-1','method':'item/commandExecution/requestApproval','params':{'threadId':'t','turnId':'a'}});result={}
    elif method=='permission':
        send({'id':'permission-1','method':'item/permissions/requestApproval','params':{'threadId':'t','turnId':'a'}});result={}
    elif method is None:
        root.joinpath('responses').open('a').write(str(rid)+'\n')
        send({'method':'fixture/responded','params':{'id':rid}});continue
    elif method=='turn/start':
        text=params['input'][0]['text'];active=rid
        if text=='foreign':
            send({'method':'turn/completed','params':{'threadId':'foreign','turn':{'id':'active','status':'completed'}}})
            send({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'old','status':'completed'}}})
            send({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'active','status':'failed','error':{'message':'actual-failure'}}}})
        elif text=='interrupted':
            send({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'active','status':'interrupted'}}})
        elif text=='wait-approval':
            send({'id':777,'method':'item/commandExecution/requestApproval','params':{'threadId':'t','turnId':'active'}})
        result={'turn':{'id':'active','status':'inProgress'}}
    elif method=='turn/interrupt':
        root.joinpath('interrupt.json').write_text(json.dumps(params))
        if mode!='ignore_interrupt':
            send({'method':'turn/completed','params':{'threadId':params['threadId'],'turn':{'id':params['turnId'],'status':'interrupted'}}})
        result={}
    else: result={}
    send({'id':rid,'result':result})
'''


async def wait_file(path):
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(.02)


async def main():
    root = Path(tempfile.mkdtemp(prefix='codex-owned-evidence-'))
    fixture = root / 'fixture.py'
    fixture.write_text(FIXTURE, encoding='utf8')
    checks = []
    stops = []
    try:
        CodexStdioClient([sys.executable])
        raise AssertionError('Missing private home must fail before launch')
    except ValueError:
        pass
    checks.append('private_home_is_required_before_native_launch')

    def instance(mode='normal'):
        directory = root / (str(len(list(root.iterdir()))) + '-' + mode)
        directory.mkdir()
        environment = create_isolated_codex_environment(directory / 'home')
        return CodexStdioClient([sys.executable, '-X', 'utf8', '-u', str(fixture), mode, str(directory)], environment), directory

    async def close(client):
        stop = await client.close()
        assert stop and stop['verified'], stop
        process_stop.validate(stop, client.transport.owned.record)
        stops.append(stop)

    async def expect_error(awaitable, contains=None):
        try:
            await awaitable
        except Exception as error:
            if contains is not None:
                assert contains in str(error), (contains, str(error))
            return
        raise AssertionError('Expected failure')

    client, directory = instance()
    gate = asyncio.Event()
    entered = asyncio.Event()
    async def register_root(record):
        assert record == client.transport.owned.record
        assert not (directory / 'executed').exists()
        entered.set()
        await gate.wait()
    async def register_bridge(record):
        assert record == client.transport.bridge_birth
        assert not (directory / 'executed').exists()
    client.before_release = register_root
    client.before_start = register_bridge
    task = asyncio.create_task(client.start())
    await entered.wait()
    assert not (directory / 'executed').exists()
    gate.set()
    await task
    await close(client)
    checks.append('root_and_bridge_callbacks_precede_native_cli')

    for point in ('before_release', 'before_start'):
        client, directory = instance()
        async def reject(_record):
            raise ValueError('registration rejected')
        setattr(client, point, reject)
        await expect_error(client.start(), 'registration rejected')
        assert not (directory / 'executed').exists()
        await close(client)
        checks.append(point + '_failure_prevents_execution')

    client, directory = instance()
    entered = asyncio.Event()
    async def hold(_record):
        entered.set()
        await asyncio.Event().wait()
    client.before_start = hold
    task = asyncio.create_task(client.start())
    await entered.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert not (directory / 'executed').exists()
    await close(client)
    checks.append('startup_cancellation_joins_scope')

    client, _directory = instance()
    await client.start()
    large = {'text': '\u4e2d\u6587' * 60000}
    assert await client.request('echo', large) == large
    assert len(client.transport.stderr_tail) <= 16384 and b'diagnostic-noise' in client.transport.stderr_tail
    replies = await asyncio.gather(*(client.request('echo', {'index': i}) for i in range(8)))
    assert replies == [{'index': i} for i in range(8)]
    await close(client)
    checks.extend(['large_unicode_rpc_crosses_64k_reader_boundary', 'stderr_never_enters_rpc', 'concurrent_requests_keep_response_identity'])

    for mode, method in (('normal', 'eof'), ('normal', 'invalid'), ('normal', 'too_large'),
                         ('normal', 'flood'), ('normal', 'flood_bytes')):
        client, _directory = instance(mode)
        await client.start()
        event_waiter = asyncio.create_task(client.next_event(timeout_seconds=10)) if method == 'eof' else None
        await expect_error(client.request(method, timeout_seconds=5))
        if event_waiter is not None:
            await expect_error(event_waiter)
            assert client.transport.exit_code == 7
        await close(client)
        checks.append(method + '_fails_closed_with_scope_receipt')

    client, _directory = instance('bad_initialize')
    await expect_error(client.start(), 'initialize response')
    await close(client)
    checks.append('invalid_initialize_retires_native_cli')

    # Independent native process remains alive across all scope stops.
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(90)'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        sentinel_birth = identity.observe(sentinel.pid)
        for cancelled in (False, True):
            client, directory = instance('detached')
            await client.start()
            await wait_file(directory / 'blocked')
            await wait_file(directory / 'leaf.pid')
            leaf = identity.observe(int((directory / 'leaf.pid').read_text()))
            operation = asyncio.create_task(client.request('large-blocked', {'text': 'x' * 800000}, timeout_seconds=.3 if not cancelled else 30))
            if cancelled:
                await asyncio.sleep(.1)
                operation.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await operation
            else:
                await expect_error(operation)
            await close(client)
            assert identity.probe(leaf)['state'] == 'exited', identity.probe(leaf)
            assert identity.probe(sentinel_birth)['state'] == 'match'
            checks.append(('cancel' if cancelled else 'timeout') + '_blocked_stdin_retires_detached_leaf')
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=10)

    client, directory = instance()
    await client.start()
    await client.request('approval')
    event = await client.next_event()
    results = await asyncio.gather(*(client.respond_to_approval(event['id'], 'accept') for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert (await client.next_event())['method'] == 'fixture/responded'
    assert (directory / 'responses').read_text().splitlines() == ['approval-1']
    await client.request('permission')
    event = await client.next_event()
    await expect_error(client.respond_to_approval(event['id'], 'accept'), 'outstanding')
    assert (directory / 'responses').read_text().splitlines() == ['approval-1']
    await close(client)
    checks.extend(['concurrent_approval_sends_exactly_once', 'permissions_cannot_use_generic_accept'])

    client, directory = instance()
    await client.start()
    await expect_error(run_codex_turn(client, 't', 'foreign', 'foreign-id'), 'actual-failure')
    result = await run_codex_turn(client, 't', 'interrupted', 'interrupted-id')
    assert result['status'] == 'interrupted'
    cancel = asyncio.Event()
    handler_entered = asyncio.Event()
    async def awaiting_approval(_event):
        handler_entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(run_codex_turn(client, 't', 'wait-approval', 'approval-id', on_event=awaiting_approval, cancel=cancel))
    await handler_entered.wait()
    cancel.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert json.loads((directory / 'interrupt.json').read_text()) == {'threadId': 't', 'turnId': 'active'}
    assert not client._server_requests
    await close(client)
    checks.extend(['foreign_terminal_events_cannot_complete_active_turn', 'interrupted_status_never_counts_as_completed',
                   'cancel_awaiting_approval_joins_interrupt_terminal'])

    client, directory = instance('ignore_interrupt')
    await client.start()
    await expect_error(run_codex_turn(client, 't', 'slow', 'timeout-id', turn_timeout_seconds=.05))
    assert client.stop_result and client.stop_result['verified']
    await close(client)
    checks.append('interrupt_without_terminal_ack_retires_scope')

    assert len(checks) == 22, checks
    value = {'passed': True, 'count': len(checks), 'checks': checks, 'scope_receipts': len(stops),
             'foreign_sentinel_untouched': True, 'actual_cli': False, 'evidence': str(root)}
    (root / 'stops.json').write_text(json.dumps(stops, indent=2), encoding='utf8')
    (root / 'result.json').write_text(json.dumps(value, indent=2), encoding='utf8')
    print(json.dumps(value))


if __name__ == '__main__':
    asyncio.run(main())
