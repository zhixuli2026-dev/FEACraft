"""One large archive/upload request per shared node root, through response send.

The nonblocking OS lock has no wait queue and survives API task cancellation
until its owned work drains. Process exit releases the lock; the lock inode is
never deleted. This bounds concurrent v1 backup memory without a format change.
"""
import asyncio
from contextlib import contextmanager
import os,re
from fastapi import HTTPException
from starlette.responses import JSONResponse
from .services import request_deadline
from .services.async_ops import finish_before_cancel


RECOVERY_DOWNLOAD_STREAM = 'recovery_download_stream'


class ArchiveBusy(Exception):pass


@contextmanager
def slot():
    from .storage import _fs_root
    from .services.file_transactions import _path
    root=_fs_root();directory=_path(root,'.archive-admission');directory.mkdir(exist_ok=True)
    path=_path(root,'.archive-admission/slot')
    with path.open('a+b') as stream:
        stream.seek(0,os.SEEK_END)
        if not stream.tell():stream.write(b'\0');stream.flush()
        stream.seek(0);acquired=False
        try:
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                acquired=True
            except (BlockingIOError,PermissionError) as error:raise ArchiveBusy from error
            yield
        finally:
            if acquired:
                stream.seek(0)
                if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


class ArchiveAdmission:
    seconds=300
    # File limits match the business endpoints. The extra MiB is for multipart
    # headers/fields; it does not raise the endpoint's file allowance.
    multipart_limits=(
        (r'/api/v1/workspaces:restore',100*1024*1024),
        (r'/api/v1/workspaces/[^/]+/cases:import',50*1024*1024),
        (r'/api/v1/cases/[^/]+/upload',16*1024*1024),
        (r'/api/v1/skills/install',2*1024*1024),
    )
    framing_bytes=1024*1024

    def __init__(self,app):self.app=app

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        path=scope.get('path','');root_path=scope.get('root_path','').rstrip('/')
        if root_path and path.startswith(root_path+'/'):path=path[len(root_path):]
        path=path.rstrip('/')
        maximum=next((size+self.framing_bytes for pattern,size in self.multipart_limits
            if scope.get('method')=='POST' and re.fullmatch(pattern,path)),None)
        exporting = scope.get('method') == 'GET' and re.fullmatch(r'/api/v1/conversations/[^/]+/export', path)
        remote_download = scope.get('method') == 'POST' and path == '/api/v1/scnet/files:download'
        if remote_download:maximum=64*1024
        recovery_download = scope.get('method') == 'GET' and re.fullmatch(
            r'/api/v1/terminal/sessions/[^/]+/recoveries/[^/]+/download', path)
        selected=maximum is not None or exporting or recovery_download or (scope.get('method')=='GET' and
            re.fullmatch(r'/api/v1/workspaces/[^/]+/backup',path))
        if not selected:return await self.app(scope,receive,send)
        too_large={'code':'upload_body_too_large','message':'请求正文超过此入口的整包大小上限',
            'max_body_bytes':maximum}
        if maximum is not None:
            declared=dict(scope.get('headers',[])).get(b'content-length')
            if declared is not None:
                try:
                    if not declared.isdigit():raise ValueError
                    length=int(declared)
                except ValueError:
                    return await JSONResponse({'detail':'Content-Length必须为非负整数'},status_code=400)(scope,receive,send)
                if length>maximum:
                    return await JSONResponse({'detail':too_large},status_code=413)(scope,receive,send)
        started=False
        received=0
        async def bounded_receive():
            nonlocal received
            message=await receive()
            if maximum is not None and message['type']=='http.request':
                received+=len(message.get('body',b''))
                if received>maximum:
                    # UploadCleanupRoute owns all spools, including on this
                    # HTTPException, disconnect, incomplete EOF or cancellation.
                    raise HTTPException(413,detail=too_large)
            return message
        async def forward(message):
            nonlocal started
            if message['type']=='http.response.start':started=True
            await send(message)
        try:
            with slot():
                async with request_deadline.scope(self.seconds):
                    if recovery_download:
                        state = scope.setdefault('state', {})
                        try:
                            await self.app(scope, bounded_receive, forward)
                        finally:
                            stream = state.pop(RECOVERY_DOWNLOAD_STREAM, None)
                            if stream is not None:
                                await finish_before_cancel(asyncio.to_thread(stream.close))
                    elif scope['method'] == 'GET' or remote_download:
                        from .services.backup_download import BackupDownload, SpooledDownload
                        if remote_download:
                            from .services.scnet_download import DownloadSpool
                            download,key = DownloadSpool(),'scnet_download'
                        else:
                            download = SpooledDownload() if exporting else BackupDownload()
                            key = 'conversation_download' if exporting else 'backup_download'
                        scope.setdefault('state', {})[key] = download
                        try:
                            await self.app(scope,bounded_receive,forward)
                        finally:
                            await finish_before_cancel(asyncio.to_thread(download.close))
                    else:
                        await self.app(scope,bounded_receive,forward)
        except ArchiveBusy:
            await JSONResponse({'detail':{'code':'archive_busy','message':'当前节点正在处理上传、备份或恢复，请稍后重试'}},
                status_code=429,headers={'Retry-After':'2'})(scope,receive,send)
        except TimeoutError:
            # After response start, terminate the incomplete transfer. A second
            # JSON response could otherwise look like a successful ZIP download.
            if started:raise
            await JSONResponse({'detail':{'code':'archive_timeout','message':'上传、备份或恢复超时，请核对处理结果后重试'}},
                status_code=504)(scope,receive,send)
