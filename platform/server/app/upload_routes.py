"""Own every multipart spool, including incomplete files absent from FormData."""
import asyncio
from fastapi import HTTPException
from fastapi.params import Form
from fastapi.routing import APIRoute
from starlette.formparsers import MultiPartParser,MultiPartException
from starlette.requests import ClientDisconnect
from .services.async_ops import finish_before_cancel


class CompleteMultipartParser(MultiPartParser):
    completed=False

    def on_end(self):
        super().on_end()
        self.completed=True


class UploadCleanupRoute(APIRoute):
    def get_route_handler(self):
        handler=super().get_route_handler()
        has_form=self.body_field is not None and isinstance(self.body_field.field_info,Form)

        async def owned(request):
            if not has_form or request.headers.get('content-type','').split(';',1)[0].strip().lower()!='multipart/form-data':
                return await handler(request)
            parser=CompleteMultipartParser(request.headers,request.stream())
            def close_spools():
                for stream in parser._files_to_close_on_error:
                    if not stream.closed:stream.close()
            try:
                try:form=await parser.parse()
                except MultiPartException as error:raise HTTPException(400,detail=error.message) from error
                except ClientDisconnect as error:raise HTTPException(400,detail='上传连接已中断') from error
                except ExceptionGroup as error:
                    # BaseHTTPMiddleware may wrap a receive-side HTTP error in
                    # an AnyIO task group. Preserve the intended 413 response.
                    pending=list(error.exceptions);body_error=None
                    while pending:
                        item=pending.pop()
                        if isinstance(item,ExceptionGroup):pending.extend(item.exceptions)
                        elif (isinstance(item,HTTPException) and item.status_code==413
                              and isinstance(item.detail,dict) and item.detail.get('code')=='upload_body_too_large'):
                            body_error=item
                        else:raise error
                    if body_error is None:raise
                    raise body_error from error
                if not parser.completed:
                    raise HTTPException(400,detail={'code':'multipart_incomplete','message':'上传正文未完整结束，请重新上传'})
                request._form=form
                return await handler(request)
            finally:
                # FormData contains completed parts only. An EOF without the
                # final boundary can otherwise leave its last file unowned.
                await finish_before_cancel(asyncio.to_thread(close_spools))
        return owned
