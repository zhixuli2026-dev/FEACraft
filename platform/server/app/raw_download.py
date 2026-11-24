"""Track a verified file spool through its last ASGI send or disconnect."""
import asyncio
import re
from threading import BoundedSemaphore

from fastapi import HTTPException
from starlette.responses import JSONResponse

from .services.async_ops import finish_before_cancel
from .services.backup_download import SpooledDownload
from .services.request_deadline import scope as deadline_scope


_slots = BoundedSemaphore(4)
_PATH = re.compile(r'/api/v1/cases/[^/]+/(?:raw|latex/source|(?:latex|geometry|report|postprocess)/artifact|'
                   r'report/(?:compose|derivative)-artifact|provenance/artifacts/[^/]+/file|solve/package)/?')
_STATE_KEY = 'raw_file_download'


def begin(request):
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, {'code': 'raw_download_busy', 'message': '文件下载繁忙，请稍后重试'},
                            headers={'Retry-After': '1'})
    try:
        download = SpooledDownload()
        request.scope.setdefault('state', {})[_STATE_KEY] = download
        return download
    except BaseException:
        _slots.release()
        raise


class RawDownloadLifetime:
    seconds = 300

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path, root = scope.get('path', ''), scope.get('root_path', '').rstrip('/')
        if root and path.startswith(root+'/'):
            path = path[len(root):]
        if scope['type'] != 'http' or scope.get('method') != 'GET' or not _PATH.fullmatch(path):
            return await self.app(scope, receive, send)

        state = scope.setdefault('state', {})
        started = False

        async def observed_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            async with deadline_scope(self.seconds):
                await self.app(scope, receive, observed_send)
        except TimeoutError:
            if started:
                raise
            response = JSONResponse({'detail': {'code': 'raw_download_timeout',
                'message': '文件下载准备超时，请稍后重试'}}, status_code=504)
            await response(scope, receive, send)
        finally:
            download = state.pop(_STATE_KEY, None)
            if download is not None:
                try:
                    await finish_before_cancel(asyncio.to_thread(download.close))
                finally:
                    _slots.release()
