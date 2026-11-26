"""One claimed Agent tool call -> one fenced request -> one owned native command."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import uuid
import sys
from functools import partial

from fastapi import HTTPException
from sqlalchemy import select

from ..db import SessionLocal
from ..config import get_settings
from ..models import Case,Conversation,NativeRequest,ToolCall,User
from .. import storage
from .async_ops import finish_before_cancel
from .case_write import coordinate,CaseWriteConflict
from .command_safety import command_block_reason
from .durable_events import json_text
from .native_execution_client import NativeComputeLimit,run_native
from .native_request_execution import scope
from . import native_request_records as records
from .owned_process import wsl_path
from .supervisor import register,unregister

MAX_OUTPUT=1024*1024
WINDOWS_LAUNCHER=Path(__file__).with_name('agent_shell_native.py').resolve()
LINUX_LAUNCHER=Path(__file__).with_name('agent_shell_linux.py').resolve()
_active={}


def sha(value):return hashlib.sha256(value.encode('utf8')).hexdigest()


async def linux_compute_policy(runtime):
    if sys.platform != 'linux' or runtime != 'windows':
        return None
    settings = get_settings()
    policy = {'cpu_limit_ns': settings.agent_shell_linux_cpu_limit_seconds * 1_000_000_000,
              'memory_limit_bytes': settings.agent_shell_linux_memory_limit_mb * 1024**2}
    if not any(policy.values()):
        return None
    from .linux_gate_compute import check_available
    try:
        await asyncio.to_thread(check_available, policy)
    except (OSError, ValueError) as error:
        raise CaseWriteConflict('agent_compute_unverified', 'Agent 命令的 Linux 资源控制器不可用') from error
    return policy


def profile(value):
    keys={'conversation_id','tool_call_id','args_sha256','command_sha256','runtime','distro','cwd','source_prefix','staging_prefix','timeout_seconds','authorization','launcher_sha256'}
    if not isinstance(value,dict) or set(value)-{'linux_compute_policy','linux_network','linux_launcher_sha256'}!=keys:
        raise ValueError('Invalid Agent shell profile')
    if type(value['conversation_id']) is not int or value['conversation_id']<=0:raise ValueError('Invalid Agent conversation')
    if not isinstance(value['tool_call_id'],str) or not 1<=len(value['tool_call_id'])<=40:raise ValueError('Invalid Agent tool identity')
    for name in ('args_sha256','command_sha256'):
        if not isinstance(value[name],str) or not re.fullmatch('[0-9a-f]{64}',value[name]):raise ValueError('Invalid Agent shell digest')
    if value['runtime'] not in {'windows','powershell','wsl'} or value['authorization'] not in {'approved','full_access'}:raise ValueError('Invalid Agent authorization profile')
    if 'linux_network' in value and (value['runtime'] != 'windows' or value['linux_network'] != 'private'):
        raise ValueError('Invalid Linux Agent network policy')
    if 'linux_launcher_sha256' in value:
        digest = value['linux_launcher_sha256']
        if value.get('linux_network') != 'private' or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
            raise ValueError('Invalid Linux Agent workspace launcher')
    if 'linux_compute_policy' in value:
        from .native_compute_v1 import validate_policy
        if value['runtime'] != 'windows' or not any(validate_policy(value['linux_compute_policy']).values()):
            raise ValueError('Invalid native Linux Agent compute policy')
    if value['runtime']=='windows':
        if not isinstance(value['launcher_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['launcher_sha256']):raise ValueError('Invalid Windows command launcher')
    elif value['launcher_sha256'] is not None:raise ValueError('Unexpected Windows command launcher')
    if value['runtime']=='wsl':
        if not isinstance(value['distro'],str) or not re.fullmatch('[a-z0-9-]+',value['distro']):raise ValueError('Invalid Agent WSL distribution')
    elif value['distro'] is not None:raise ValueError('Host Agent command selected a native distribution')
    if type(value['timeout_seconds']) is not int or not 1<=value['timeout_seconds']<=600:raise ValueError('Invalid Agent command deadline')
    if not isinstance(value['cwd'],str) or '\0' in value['cwd'] or not Path(value['cwd']).is_absolute():raise ValueError('Invalid Agent shell directory')
    for name in ('source_prefix','staging_prefix'):
        if name=='staging_prefix' and value[name] is None:continue
        if not isinstance(value[name],str) or not value[name] or len(value[name])>4096 or '\0' in value[name]:raise ValueError('Invalid Agent storage binding')
    return value


def command(selected,parameters):
    profile(selected)
    if not isinstance(parameters,dict) or set(parameters)!={'command','distro'} or parameters['distro']!=selected['distro']:raise ValueError('Agent shell parameters differ')
    text=parameters['command']
    if not isinstance(text,str) or sha(text)!=selected['command_sha256'] or command_block_reason(text):raise ValueError('Agent command differs from its authorized tool call')
    if selected['runtime']=='wsl':
        argv=['wsl.exe','-d',selected['distro'],'--user','nobody','--exec','bash','-lc',
              f"cd {shlex.quote(wsl_path(selected['cwd']))} && {text}"]
    elif selected['runtime']=='powershell':
        executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        script=('$ProgressPreference = "SilentlyContinue"; '
                '[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); '
                '$OutputEncoding = [Console]::OutputEncoding; '+text)
        argv=[str(executable),'-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16-le')).decode('ascii')]
    elif os.name=='nt':
        if hashlib.sha256(WINDOWS_LAUNCHER.read_bytes()).hexdigest()!=selected['launcher_sha256']:raise ValueError('Agent Windows launcher changed')
        argv=[str(Path(getattr(sys,'_base_executable',sys.executable)).resolve()),'-I','-S','-u',str(WINDOWS_LAUNCHER),'--command',base64.b64encode(text.encode('utf8')).decode('ascii')]
    else:
        if hashlib.sha256(LINUX_LAUNCHER.read_bytes()).hexdigest()!=selected.get('linux_launcher_sha256'):
            raise ValueError('Agent Linux workspace launcher changed')
        python = str(Path(getattr(sys,'_base_executable',sys.executable)).resolve())
        argv = ['/usr/bin/unshare','--user','--map-root-user','--mount','--pid','--fork','--kill-child=SIGKILL',
                python,'-I','-S','-u',str(LINUX_LAUNCHER),'--workspace',selected['cwd'],'--',text]
    return argv,selected['timeout_seconds'],MAX_OUTPUT


async def authorize(db,case_id,user_id,tid,authorization,*,expected=None):
    from ..routers.workspaces import get_owned_case
    user=await db.get(User,user_id)
    if user is None or user.status!='active':raise HTTPException(403,'Agent 用户已不可用')
    case=await get_owned_case(case_id,user,db)
    tc=await db.get(ToolCall,tid,populate_existing=True)
    if tc is None or tc.tool!='run_shell_command' or tc.status!='running':raise CaseWriteConflict('state_conflict','Agent 工具调用未被领取或已结束')
    conv=await db.get(Conversation,tc.conversation_id,populate_existing=True)
    if conv is None or conv.case_id!=case_id:raise HTTPException(404,'Agent 工具调用不属于该算例')
    if conv.permission_mode in {'ask','plan'} or (authorization=='full_access' and conv.permission_mode!='full_access'):
        raise HTTPException(403,'Agent 权限模式已不允许执行命令')
    args=json.loads(tc.args)
    if expected is not None and (case.s3_prefix!=expected['source_prefix'] or conv.id!=expected['conversation_id']
            or sha(json_text(args))!=expected['args_sha256']):raise CaseWriteConflict('input_stale','Agent 命令、会话或来源已变化')
    return case,conv,tc,args


class Execution:
    def __init__(self,case_id,user_id,tid,authorization,args):
        self.case_id=case_id;self.user_id=user_id;self.tid=tid;self.authorization=authorization;self.args=args
        self.owner=None;self.selected=None;self.stage=None;self.task=None;self.cancel=asyncio.Event()
        self.scope_started=False
        self.conversation_id=None
        self.executor=None
        from .conversation_execution import active_owner
        self.conversation_owner = active_owner.get()

    async def stop(self):
        self.cancel.set()
        if self.task is not None and self.task is not asyncio.current_task():await finish_before_cancel(asyncio.gather(self.task,return_exceptions=True))

    async def prepare(self):
        from .runtime_config import get_runtime_setting
        runtime = self.args.get('runtime','windows')
        if sys.platform == 'linux' and runtime != 'windows':
            raise ValueError('Linux Agent 命令使用本机 POSIX Shell')
        compute_policy = await linux_compute_policy(runtime)
        async with SessionLocal() as db:
            case,conv,tc,args=await authorize(db,self.case_id,self.user_id,self.tid,self.authorization)
            async with coordinate(db,case):
                if sha(json_text(args))!=sha(json_text(self.args)):raise CaseWriteConflict('input_stale','Agent 工具参数已变化')
                text=args.get('command');seconds=args.get('timeout_seconds',120)
                if not isinstance(text,str) or not text or len(text)>4000 or '\0' in text:raise ValueError('Agent 命令必须为1至4000字符')
                if command_block_reason(text):raise ValueError('Agent 命令命中高危模式')
                if type(seconds) is not int:raise ValueError('Agent 命令时限必须为整数')
                def stage():
                    if storage._use_s3():self.stage=storage.prepare_job_staging(case.s3_prefix,uuid.uuid4().int)
                    return str(storage._fs_path(self.stage or case.s3_prefix,'.').resolve())
                cwd=await finish_before_cancel(asyncio.to_thread(stage))
                self.selected={'conversation_id':conv.id,'tool_call_id':tc.id,'args_sha256':sha(json_text(args)),'command_sha256':sha(text),
                    'runtime':runtime,'distro':get_runtime_setting('wsl_distro','vibeflow-linux') if runtime=='wsl' else None,
                    'cwd':cwd,'source_prefix':case.s3_prefix,'staging_prefix':self.stage,'timeout_seconds':max(1,min(seconds,600)),'authorization':self.authorization}
                self.selected['launcher_sha256']=hashlib.sha256(WINDOWS_LAUNCHER.read_bytes()).hexdigest() if runtime=='windows' else None
                if compute_policy is not None:
                    self.selected['linux_compute_policy'] = compute_policy
                if sys.platform == 'linux' and runtime == 'windows':
                    self.selected['linux_network'] = 'private'
                    self.selected['linux_launcher_sha256'] = hashlib.sha256(LINUX_LAUNCHER.read_bytes()).hexdigest()
                async def reserve():self.owner=await records.create(case.id,self.user_id,'agent.shell',self.selected,db=db)
                await finish_before_cancel(reserve())
                from . import audit
                await audit.case_event(db, case, self.user_id, 'agent.command.admitted', 'native_request', self.owner.id,
                    {'runtime': runtime, 'authorization': self.authorization, 'command_sha256': self.selected['command_sha256']})
            await finish_before_cancel(db.commit())

    async def reauthorize(self):
        if self.cancel.is_set():raise asyncio.CancelledError
        if self.selected['runtime']=='windows' and hashlib.sha256(WINDOWS_LAUNCHER.read_bytes()).hexdigest()!=self.selected['launcher_sha256']:raise ValueError('Agent Windows launcher changed before release')
        if 'linux_launcher_sha256' in self.selected and hashlib.sha256(LINUX_LAUNCHER.read_bytes()).hexdigest()!=self.selected['linux_launcher_sha256']:
            raise ValueError('Agent Linux workspace launcher changed before release')
        async with SessionLocal() as db:
            case,_,_,_=await authorize(db,self.case_id,self.user_id,self.tid,self.authorization,expected=self.selected)
            async with coordinate(db,case):
                if self.conversation_owner is not None:
                    from .conversation_execution import current
                    await current(db, self.conversation_owner)
                if str(storage._fs_path(self.stage or case.s3_prefix,'.').resolve())!=self.selected['cwd']:raise CaseWriteConflict('input_stale','Agent 工作目录已变化')
            await db.commit()
        await records.release_check(self.owner,{key:entry.owned.record for key,entry in self.executor.entries.items()})
        if self.cancel.is_set():raise asyncio.CancelledError

    async def run(self):
        try:
            await self.prepare()
            if self.cancel.is_set():raise asyncio.CancelledError
            self.scope_started=True
            async with scope(self.case_id,self.user_id,self.selected['cwd'],'agent.shell',self.selected,owner=self.owner,before_release=self.reauthorize) as executor:
                self.executor=executor
                invoke=partial(run_native,'agent_shell',{'command':self.args['command'],'distro':self.selected['distro']},
                    timeout=self.selected['timeout_seconds'],capture=True)
                operation=asyncio.create_task(executor.run_sync(invoke,timeout=self.selected['timeout_seconds']+20))
                stopping=asyncio.create_task(self.cancel.wait())
                try:
                    done,_=await asyncio.wait((operation,stopping),return_when=asyncio.FIRST_COMPLETED)
                    if stopping in done:
                        operation.cancel();await finish_before_cancel(asyncio.gather(operation,return_exceptions=True));raise asyncio.CancelledError
                    reply=await operation
                finally:
                    if not operation.done():operation.cancel()
                    stopping.cancel();await finish_before_cancel(asyncio.gather(operation,stopping,return_exceptions=True))
                # PostgreSQL TEXT cannot store NUL. Keep an explicit visible
                # escape so binary/native output cannot lose the tool receipt.
                output=reply.stdout.decode('utf8','replace').replace('\x00','\\0')
                if len(output)>20000:output=output[:20000]+'\n...[输出截断]'
                if self.stage is not None:
                    async def authorized(db):
                        if self.cancel.is_set():raise asyncio.CancelledError
                        case,_,_,_=await authorize(db,self.case_id,self.user_id,self.tid,self.authorization,expected=self.selected)
                        await records._current(db,self.owner,self.owner.nonce)
                        if self.conversation_owner is not None:
                            from .conversation_execution import current
                            await current(db,self.conversation_owner)
                        if str(storage._fs_path(self.stage,'.').resolve())!=self.selected['cwd']:
                            raise CaseWriteConflict('input_stale','Agent 工作目录已变化')
                        return case
                    from .staging_sync import sync_authorized
                    await sync_authorized(self.stage,self.selected['source_prefix'],self.case_id,authorized)
                    await self.cleanup(synchronized=True)
                return {'ok':reply.returncode==0,'result':f'exit={reply.returncode}\n{output}','execution_id':self.owner.id}
        except asyncio.CancelledError:
            self.cancel.set()
            raise
        except Exception as error:
            result = {'ok':False, 'result':type(error).__name__+': '+str(error),
                      'execution_id':self.owner.id if self.owner else None}
            if isinstance(error, NativeComputeLimit):
                result['code'] = 'agent_resource_limit'
            elif isinstance(error, CaseWriteConflict) and error.code == 'agent_compute_unverified':
                result['code'] = error.code
            return result
        finally:
            if self.owner is not None and not self.scope_started:
                try:await finish_before_cancel(records.complete(self.owner,'CancelledError' if self.cancel.is_set() else 'RuntimeError',cancelled=self.cancel.is_set()))
                except Exception:pass  # A failed/rolled-back reservation remains incomplete; never release a command.
            if not self.scope_started and self.stage is not None:await finish_before_cancel(self.cleanup())

    async def cleanup(self,*,synchronized=False):
        from ..routers.terminal import _cleanup_staging
        await finish_before_cancel(asyncio.to_thread(_cleanup_staging,self.stage,synchronized=synchronized))


async def execute(case_id,user_id,tid,authorization,args,cancel=None):
    if user_id is None or tid is None or authorization not in {'approved','full_access'}:raise ValueError('Owned Agent execution requires a claimed tool call and user')
    entry=Execution(case_id,user_id,tid,authorization,args);key=uuid.uuid4().hex
    async with SessionLocal() as db:
        _,conv,_,_=await authorize(db,case_id,user_id,tid,authorization)
        entry.conversation_id=conv.id
    entry.task=asyncio.create_task(entry.run());_active[key]=entry
    register('agent-tool',key,case_id,entry.stop)
    monitor=asyncio.create_task(cancel.wait()) if cancel is not None else None
    try:
        if monitor is not None:
            done,_=await asyncio.wait((entry.task,monitor),return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:await entry.stop();return {'ok':False,'status':'cancelled','result':'Agent 命令已取消','execution_id':entry.owner.id if entry.owner else None}
        return await entry.task
    except asyncio.CancelledError:
        if entry.cancel.is_set() and not asyncio.current_task().cancelling():
            return {'ok':False,'status':'cancelled','result':'Agent 命令已取消','execution_id':entry.owner.id if entry.owner else None}
        raise
    finally:
        if not entry.task.done():entry.task.cancel()
        if monitor is not None:monitor.cancel()
        await finish_before_cancel(asyncio.gather(*(task for task in (entry.task,monitor) if task is not None),return_exceptions=True))
        _active.pop(key,None);unregister('agent-tool:'+key)


async def cancel_conversation(cid):
    entries=[entry for entry in tuple(_active.values()) if entry.conversation_id==cid]
    await finish_before_cancel(asyncio.gather(*(entry.stop() for entry in entries)))
    return bool(entries)
