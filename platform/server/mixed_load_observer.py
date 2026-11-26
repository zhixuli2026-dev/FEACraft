"""Record metadata only from actual DeepSeek requests in the disposable stack."""

import fcntl
import json
from pathlib import Path
from socket import gethostname
import time
from uuid import uuid4

import httpx

from app.main import app
from app.routers import agent
from app.services import conversation_execution


STATE = Path('/work/data')
RECEIPT = STATE / f'upstream-{gethostname()}.jsonl'
send = httpx.AsyncClient.send
lines = httpx.Response.aiter_lines
read_case = agent.read_case
renew = conversation_execution.renew


def write(row):
    with RECEIPT.open('a', encoding='utf-8') as output:
        output.write(json.dumps(row) + '\n')


def official(url):
    return url.scheme == 'https' and url.host == 'api.deepseek.com' and url.path.endswith('/chat/completions')


async def observed_send(self, request, *args, **kwargs):
    if not official(request.url):
        return await send(self, request, *args, **kwargs)
    with (STATE / 'llm-allowance').open('r+') as allowance:
        fcntl.flock(allowance, fcntl.LOCK_EX)
        remaining = int(allowance.read())
        if remaining <= 0:
            raise RuntimeError('Mixed load reached its authorized request count')
        allowance.seek(0)
        allowance.write(str(remaining - 1))
        allowance.truncate()
    started = time.time_ns() // 1_000_000
    try:
        response = await send(self, request, *args, **kwargs)
    except httpx.TransportError as error:
        write({'kind':'transport_error','error':type(error).__name__,'started_ms':started})
        raise
    trace = uuid4().hex
    response.extensions['mixed_trace'] = trace
    write({'kind':'http','status':response.status_code,'started_ms':started,
           'headers_ms':time.time_ns() // 1_000_000,'request_id':response.headers.get('x-request-id'),
           'requested_model':json.loads(request.content).get('model'),'trace':trace})
    return response


async def observed_lines(self, *args, **kwargs):
    identity = None
    first_delta = None
    try:
        async for line in lines(self, *args, **kwargs):
            if official(self.request.url) and line.startswith('data:') and line[5:].strip() != '[DONE]':
                try:
                    payload = json.loads(line[5:])
                    if payload.get('id'):
                        identity = (payload['id'], payload.get('model'))
                    if first_delta is None and any(choice.get('delta', {}).get('content') for choice in payload.get('choices', ())):
                        first_delta = time.time_ns() // 1_000_000
                except (ValueError, AttributeError, TypeError):
                    pass
            yield line
    finally:
        if identity:
            write({'kind':'model','completion_id':identity[0],'model':identity[1],
                   'first_delta_ms':first_delta,'ended_ms':time.time_ns() // 1_000_000,
                   'trace':self.extensions.get('mixed_trace')})


async def observed_case_read(case_id, prefix, user_id, function, *args, **kwargs):
    if function is not agent.read_prompt_memory:
        return await read_case(case_id, prefix, user_id, function, *args, **kwargs)
    started = time.perf_counter()
    try:
        return await read_case(case_id, prefix, user_id, function, *args, **kwargs)
    finally:
        write({'kind': 'prompt_memory', 'elapsed_ms': round((time.perf_counter() - started) * 1000, 2),
               'single_file': kwargs.get('file_path') is not None})


async def observed_renew(db, owner):
    try:
        return await renew(db, owner)
    except Exception as error:
        write({'kind': 'conversation_renew_failed', 'conversation_id': owner.conversation_id,
               'reason': getattr(error, 'code', None) or type(error).__name__})
        raise


httpx.AsyncClient.send = observed_send
conversation_execution.renew = observed_renew
httpx.Response.aiter_lines = observed_lines
agent.read_case = observed_case_read
