"""Route deadlines include body parsing and dependencies, before sending a response."""
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse

from .services import request_deadline


class _DeadlineRoute(APIRoute):
    def get_route_handler(self):
        handler=super().get_route_handler()

        async def bounded(request):
            try:
                async with request_deadline.scope(self.seconds) as deadline:
                    response=await handler(request)
                    # Protected commits can finish after cancellation. A late
                    # return must not turn the expired request into a success.
                    deadline.check()
                    return response
            except TimeoutError:
                # No response has been sent by get_route_handler. Framework
                # dependency teardown and the single response send remain owned
                # by FastAPI, outside the admission budget.
                return JSONResponse(status_code=self.timeout_status,content={'detail':self.timeout_detail})

        return bounded


class ImageCleanupDeadlineRoute(_DeadlineRoute):
    seconds=request_deadline.IMAGE_CLEANUP_SECONDS
    timeout_status=409
    timeout_detail={'code':'image_cleanup_incomplete',
        'message':'镜像清理请求已超时，请查询登记记录确认结果后重试；恢复快照和原始元数据保留'}


class SandboxRecoveryDeadlineRoute(_DeadlineRoute):
    seconds=request_deadline.SANDBOX_RECOVERY_SECONDS
    timeout_status=502
    timeout_detail={'code':'sandbox_recovery_incomplete',
        'message':'恢复请求已超时，请查询恢复记录确认结果；原镜像和操作证据保留'}


class StagingCleanupDeadlineRoute(_DeadlineRoute):
    seconds=request_deadline.STAGING_CLEANUP_SECONDS
    timeout_status=409
    timeout_detail={'code':'staging_cleanup_incomplete',
        'message':'暂存清理请求已超时，请查询登记记录确认结果后重试；恢复快照和原始元数据保留'}


class ManualFileRecoveryDeadlineRoute(_DeadlineRoute):
    seconds=300
    timeout_status=409
    timeout_detail={'code':'manual_file_recovery_incomplete',
        'message':'文件版本核对已超时，操作结果仍待确认；请保留原文件和同一次确认，查询操作记录后重试'}
