"""Real Windows/WSL startup gates; no API or developer database is involved."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from app.services import owned_process as owned
from app.services import process_identity_native as identity
from testsupport import SERVER


async def main():
    root=Path(tempfile.mkdtemp(prefix='owned-process-evidence-',dir=SERVER.parents[1]/'tmp'))
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    async def launch(argv,runtime='windows'):
        process=owned.OwnedProcess()
        return await process.prepare(argv,cwd=root,env=dict(os.environ),runtime=runtime)
    async def finish(process,code=0):
        output=await asyncio.wait_for(process.proc.stdout.read(),timeout=25)
        assert await asyncio.wait_for(process.proc.wait(),timeout=5)==code,output.decode('utf-8','replace')
        await process.finish()
        process.close()
        return output
    marker=root/'windows-payload.txt'
    script='from pathlib import Path; import os; Path('+repr(str(marker))+').write_text("payload"); print("PAYLOAD",os.getpid()); raise SystemExit(7)'
    process=await launch([sys.executable,'-c',script])
    try:
        assert process.record['pid']==process.proc.pid and identity.probe(process.record['host'])['state']=='match'
        assert not marker.exists()
        await asyncio.sleep(.1)
        assert not marker.exists()
        await process.release()
        output=await finish(process,7)
        assert b'PAYLOAD' in output and marker.read_text()=='payload'
        assert identity.probe(process.record['host'])['state']=='exited'
        done('base_interpreter_gate_holds_venv_payload_until_release_and_preserves_exit_code')
    finally:
        await process.abort()
    marker.unlink()
    original=asyncio.create_subprocess_exec
    created=asyncio.Event()
    returned=asyncio.Event()
    captured=[]
    async def held_creation(*args,**kwargs):
        proc=await original(*args,**kwargs)
        captured.append(proc)
        created.set()
        await returned.wait()
        return proc
    process=owned.OwnedProcess()
    with patch.object(asyncio,'create_subprocess_exec',held_creation):
        pending=asyncio.create_task(process.prepare([sys.executable,'-c',script],cwd=root,env=dict(os.environ),runtime='windows'))
        try:
            await asyncio.wait_for(created.wait(),timeout=10)
            birth=identity.observe(captured[0].pid)
            pending.cancel()
            await asyncio.sleep(.05)
            assert not pending.done() and not marker.exists()
        finally:
            returned.set()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('Cancellation was lost')
    assert process.closed and captured[0].returncode is not None
    assert identity.probe(birth)['state']=='exited' and not marker.exists()
    done('cancellation_during_creation_waits_for_actual_handle_then_cleans_without_payload')
    marker.write_text('reset')

    for scenario in ('disconnect','bad_nonce'):
        marker.unlink()
        process=await launch([sys.executable,'-c',script])
        try:
            if scenario=='disconnect':
                process.proc.stdin.close()
            else:
                process.proc.stdin.write(b'{"action":"release","nonce":"wrong"}\n')
                await process.proc.stdin.drain()
            await finish(process,125)
            assert not marker.exists()
            done('Windows_'+scenario+'_cannot_start_payload')
        finally:
            await process.abort()
        marker.write_text('reset')

    marker=root/'wsl-payload.txt'
    script='from pathlib import Path; import os; Path('+repr(owned.wsl_path(marker))+').write_text("native payload"); print("NATIVE",os.getpid()); raise SystemExit(7)'
    argv=['wsl.exe','-d','vibeflow-linux','--exec','python3','-c',script]
    process=await launch(argv,'wsl')
    try:
        assert process.record['native']['distribution']=='vibeflow-linux'
        native=process.record['native']['process']
        assert (await owned.probe_native('vibeflow-linux',native))['state']=='match'
        assert native['process_group']==native['session']==native['pid']
        assert not marker.exists()
        await process.release()
        output=await finish(process,7)
        assert marker.read_text()=='native payload' and output.startswith(b'NATIVE '),output
        assert 1<int(output.split()[1])<100,output
        assert (await owned.probe_native('vibeflow-linux',native))['state']=='exited'
        done('WSL_birth_scope_is_verified_before_second_release_and_payload_runs_in_private_PID_namespace')
    finally:
        await process.abort()

    marker.unlink()
    process=await launch(argv,'wsl')
    native=process.record['native']['process']
    await process.abort()
    assert not marker.exists() and (await owned.probe_native('vibeflow-linux',native))['state']=='exited'
    done('abort_before_native_release_exits_owned_helpers_without_starting_payload')

    original_probe=owned.probe_native
    async def reject_probe(*args):
        return {'state':'unverified','reason':'injected_probe_failure'}
    process=owned.OwnedProcess()
    with patch.object(owned,'probe_native',reject_probe):
        try:
            await process.prepare(argv,cwd=root,env=dict(os.environ),runtime='wsl')
        except (ValueError,RuntimeError):
            pass
        else:
            raise AssertionError('Failed native probe released payload')
    assert process.closed and not marker.exists()
    assert (await original_probe('vibeflow-linux',process.native['process']))['state']=='exited'
    done('native_probe_failure_after_handshake_closes_waiting_helper_before_registration')

    heartbeat=root/'native-heartbeat.txt'
    script='from pathlib import Path; import time; p=Path('+repr(owned.wsl_path(heartbeat))+');\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.03)'
    process=await launch(['wsl.exe','-d','vibeflow-linux','--exec','python3','-c',script],'wsl')
    try:
        native=process.record['native']['process']
        await process.release()
        for _ in range(300):
            if heartbeat.exists(): break
            await asyncio.sleep(.02)
        assert heartbeat.exists()
        await process.abort()
        observed=heartbeat.read_bytes()
        await asyncio.sleep(.2)
        assert heartbeat.read_bytes()==observed
        assert (await owned.probe_native('vibeflow-linux',native))['state']=='exited'
        done('closing_native_control_stops_its_running_payload_and_supervisor')
    finally:
        await process.abort()
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(root)}
    (root/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print('OWNED_PROCESS_OK '+json.dumps(result))


if __name__=='__main__':
    asyncio.run(main())
