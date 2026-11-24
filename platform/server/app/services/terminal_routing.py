"""Forward an authorized terminal reconnect or close to its registered API."""

import asyncio
from urllib.parse import quote

import aiohttp
import httpx
from fastapi import HTTPException, WebSocketDisconnect
from fastapi.responses import Response

from ..config import get_settings
from .connection_tickets import issue, terminal_target
from .session_owner_routing import configured_origin
from .terminal_output import MAX_OUTPUT_BYTES, SEND_TIMEOUT

TICKET_HEADER = 'X-VibeFlow-Terminal-Relay'


def endpoint(row):
    return configured_origin(row, get_settings().terminal_owner_routes)


async def close(origin, sid, user_id, wait):
    headers = {TICKET_HEADER: issue(user_id, 'terminal.close', terminal_target(0, sid))}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=4), trust_env=False) as client:
            reply = await client.delete(f'{origin}/api/v1/terminal/sessions/{quote(sid, safe="")}',
                                        params={'wait': str(wait).lower()}, headers=headers)
    except httpx.RequestError as error:
        raise HTTPException(503, {'code': 'terminal_owner_unavailable',
                                  'message': '暂时无法连接终端所属实例'}) from error
    return Response(reply.content, status_code=reply.status_code, media_type='application/json')


async def reconnect(ws, origin, sid, case, shell, user_id):
    target = f'ws{origin.removeprefix("http")}/ws/terminal'
    headers = {TICKET_HEADER: issue(user_id, 'terminal.reconnect', terminal_target(case, sid))}
    params = {'session': sid, 'case': case, 'shell': shell}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8, connect=4), trust_env=False) as client:
            async with client.ws_connect(target, params=params, headers=headers, max_msg_size=MAX_OUTPUT_BYTES) as peer:
                await _bridge(ws, peer)
    except (aiohttp.ClientError, OSError, TimeoutError):
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                await ws.send_json({'type': 'error', 'code': 'terminal_owner_unavailable',
                                    'message': '暂时无法连接终端所属实例', 'status': 503})
                await ws.close(code=1013)
        except (TimeoutError, WebSocketDisconnect, RuntimeError):
            pass


async def _bridge(ws, peer):
    async def to_owner():
        while True:
            frame = await ws.receive()
            if frame['type'] == 'websocket.disconnect':
                return
            async with asyncio.timeout(SEND_TIMEOUT):
                if frame.get('bytes') is not None:
                    await peer.send_bytes(frame['bytes'])
                elif frame.get('text') is not None:
                    await peer.send_str(frame['text'])

    async def to_client():
        async for frame in peer:
            async with asyncio.timeout(SEND_TIMEOUT):
                if frame.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_text(frame.data)
                elif frame.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(frame.data)
                elif frame.type == aiohttp.WSMsgType.ERROR:
                    raise aiohttp.ClientConnectionError('Terminal relay disconnected')
        code = peer.close_code or 1011
        await ws.close(code=1011 if code in (1005, 1006, 1015) else code)

    tasks = [asyncio.create_task(to_owner()), asyncio.create_task(to_client())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
