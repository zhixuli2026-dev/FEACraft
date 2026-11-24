"""Owned, bounded read-only inventory in the configured native filesystem."""
import asyncio,base64,hashlib,json,sys,time,uuid
from pathlib import Path
from . import cli_native_inventory_reader as protocol,process_identity_native as identity
from .async_ops import finish_before_cancel
from .storage_usage import MeasurementUnavailable,_Enough

READER=Path(protocol.__file__).resolve()

async def read(request,deadline):
    from .owned_process import OwnedProcess
    from .control_identity import read as probe
    owned=OwnedProcess();reason='executor_error'
    try:
        async with asyncio.timeout(max(0,deadline-time.monotonic())):
            protocol.validate(request)
            encoded=base64.b64encode(json.dumps(request,separators=(',',':')).encode()).decode('ascii')
            argv=[str(Path(getattr(sys,'_base_executable',sys.executable)).resolve()),'-I','-S','-u',str(READER),'--host',encoded]
            await owned.prepare(argv,cwd=READER.parent,env=None,runtime='windows')
            protocol.validate(request);await owned.verify_registration();await owned.release()
            raw=await protocol.bounded(owned.proc.stdout,protocol.LIMIT)
            if await owned.wait_root_exit():raise MeasurementUnavailable('Native inventory reader failed')
            result=json.loads(raw)
            if (not isinstance(result,dict) or set(result)!={'schema','nonce','reader','status','root_inode','entries','bytes'}
                    or result['schema']!=protocol.SCHEMA or result['nonce']!=request['nonce'] or result['status'] not in protocol.STATUSES
                    or type(result['entries']) is not int or not 0<=result['entries']<=request['max_entries']+1
                    or type(result['bytes']) is not int or result['bytes']<0
                    or result['root_inode'] is not None and (type(result['root_inode']) is not int or result['root_inode']<=0)):
                raise MeasurementUnavailable('Native inventory response is invalid')
            birth=identity.validate(result['reader'])
            if birth['platform']!='linux' or birth['uid']!=65534:raise MeasurementUnavailable('Native inventory owner is invalid')
            await finish_before_cancel(owned.finish())
            if await probe(request['distribution'],'identity',{'expected':birth})!={'state':'exited','reason':'process_exited'}:
                raise MeasurementUnavailable('Native inventory reader exit is unverified')
            protocol.validate(request)
            return result
    except asyncio.CancelledError:
        reason='cancelled';raise
    except TimeoutError as error:
        reason='timeout';raise MeasurementUnavailable('Storage measurement exceeded its deadline') from error
    finally:
        try:
            if owned.proc is not None and not owned.closed and owned.stop_result is None:
                await finish_before_cancel(owned.stop(reason=reason,grace_ms=0))
        finally:owned.close()

def scan(settings,counter,*,user_id,root_inode):
    from .runtime_config import get_runtime_setting
    helpers={name:hashlib.sha256(READER.with_name(name).read_bytes()).hexdigest() for name in protocol.FILES}
    for attempt in range(3):
        counter.check()
        request={'schema':protocol.SCHEMA,'nonce':uuid.uuid4().hex+uuid.uuid4().hex,
            'distribution':get_runtime_setting('wsl_distro','vibeflow-linux'),'root':settings.codex_wsl_state_root,'user_id':user_id,
            'max_entries':counter.maximum-counter.entries,'stop_after':None if counter.stop_after is None else max(0,counter.stop_after-counter.bytes),
            'seconds':min(10,counter.deadline-time.monotonic()),'helpers':helpers}
        result=asyncio.run(read(request,counter.deadline));counter.check()
        if result['status']!='missing' or attempt==2:
            break
        # An atomic CLI file replacement can remove a listed entry before stat.
        # Rescan the same root under the original deadline and visit budget.
        if result['root_inode']!=root_inode:
            raise MeasurementUnavailable('Native CLI state root changed')
        counter.entries+=result['entries']
        if counter.entries>=counter.maximum:
            raise MeasurementUnavailable('Storage measurement entry limit exceeded')
    status=result['status']
    if status in {'complete','lower_bound','entry_limit'} and result['root_inode']!=root_inode:
        raise MeasurementUnavailable('Native CLI state root changed')
    if status in {'complete','lower_bound','entry_limit'}:
        counter.entries+=result['entries'];counter.bytes+=result['bytes']
    if status=='entry_limit' or counter.entries>counter.maximum:
        raise MeasurementUnavailable('Storage measurement entry limit exceeded')
    if status=='lower_bound':
        if counter.stop_after is None or counter.bytes<=counter.stop_after:
            raise MeasurementUnavailable('Native inventory lower bound is invalid')
        raise _Enough()
    if status=='missing':raise FileNotFoundError('Native inventory entry disappeared')
    failures={'deadline':'Storage measurement exceeded its deadline','redirected':'Storage measurement cannot follow links or reparse points',
        'changed':'Storage directory changed','special':'Storage measurement encountered a special file','unavailable':'Native CLI state root is unavailable'}
    if status in failures:raise MeasurementUnavailable(failures[status])
    if status!='complete':raise MeasurementUnavailable('Native inventory outcome is invalid')
    if counter.stop_after is not None and counter.bytes>counter.stop_after:raise _Enough()
