"""可视化网关：经主后端鉴权反代 viz kernel（trame 客户端 + wslink WS 隧道）

开发形态问题：kernel 直接暴露 127.0.0.1:PORT 裸端口（iframe 直连无鉴权）。
本路由把 iframe 收编到主后端鉴权内：
  HTTP  GET /viz-proxy/{sid}/{path}（静态资产反代，httpx 流式）
  WS    /viz-proxy/{sid}/ws（wslink 隧道，双向二进制/文本帧）
鉴权：网站登录使用第一方 HttpOnly Cookie；客户端可使用单会话短期票据，
成功后按 sid 路径设置 HttpOnly/SameSite=Strict cookie，
后续静态子资源和 wslink 复用该 cookie，仍逐次校验用户与会话归属；
只对"注册表内活跃会话"的端口转发（闭集，不开任意反代）。
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote, urlencode

import aiohttp
import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse

from ..deps import connection_user_id
from ..services.connection_tickets import TTL_SECONDS, issue, viz_cookie
from ..services.siwc_protocol import SESSION_COOKIE
from .viz import _authorize, _row, _sessions, owner_endpoint

router = APIRouter(tags=["viz-proxy"])

_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length",
               "upgrade", "te", "trailer"}


_PARAVIEW_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>ParaViewWeb Visualizer</title>
</head>
<body>
  <div class="content"></div>
  <script src="Visualizer.js"></script>
  <script>
    const endpoint = new URL("ws", window.location.href);
    endpoint.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    Visualizer.connect({ application: "visualizer", sessionURL: endpoint.href });
  </script>
</body>
</html>"""


async def _resolve(sid: int, token: str, connection):
    user_id = await connection_user_id(connection, token, scope='viz', target=str(sid))
    if user_id is None:
        raise HTTPException(401, "令牌无效")
    sess = _sessions.get(sid)
    if sess is None:
        row = await _row(sid, user_id)
        remote = owner_endpoint(row)
        if remote is None or row.phase != 'active':
            raise HTTPException(404, '可视化会话不存在或已退出')
        return None, remote, user_id
    if (sess.user_id!=user_id or sess.alive is not True or sess._close_task is not None
            or sess.owned.proc.returncode is not None):
        raise HTTPException(404, "可视化会话不存在或已退出")
    from ..db import SessionLocal
    async with SessionLocal() as db:
        await _authorize(db,sess.case_id,user_id)
    try:await sess.verify_ready()
    except (ValueError,OSError,ProcessLookupError):raise HTTPException(404,'可视化会话不存在或已退出')
    sess.touch()
    return sess, None, user_id


@router.get("/viz-proxy/{sid}/{path:path}")
async def http_proxy(sid: int, path: str, request: Request, token: str = ""):
    query_token = token or request.query_params.get("token", "")
    cookie_token = request.cookies.get(viz_cookie(sid), "")
    # A current first-party login takes precedence over an older view ticket.
    auth = query_token or ('' if request.cookies.get(SESSION_COOKIE) else cookie_token)
    sess, remote, user_id = await _resolve(sid, auth, request)
    if any(part in ('.', '..') for part in path.split('/')):
        raise HTTPException(404, '可视化文件不存在')
    if remote:
        target = f'{remote}/viz-proxy/{sid}/{quote(path, safe="/")}'
        cookie = f'{viz_cookie(sid)}={issue(user_id, "viz", str(sid))}'
        response = await _proxy_asset(target, request, viewer_cookie=cookie)
    elif sess.selected.get('engine') == 'paraview' and path in ('', 'index.html'):
        response = HTMLResponse(_PARAVIEW_PAGE, headers={'Cache-Control':'no-store'})
    else:
        response = await _proxy_asset(f'http://127.0.0.1:{sess.port}/{quote(path, safe="/")}', request)
    if query_token:
        # Both viewers authenticate relative requests using the cookie on this session path.
        response.set_cookie(viz_cookie(sid), query_token, path=f"/viz-proxy/{sid}/",
                            max_age=TTL_SECONDS['viz'], httponly=True, samesite="strict",
                            secure=request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https')
    return response


async def _proxy_asset(target: str, request: Request, *, viewer_cookie=''):
    query = [(key, value) for key, value in request.query_params.multi_items() if key != 'token']
    if query:
        target += '?' + urlencode(query)
    excluded = {'host', 'connection', 'authorization', 'proxy-authorization', 'referer', 'cookie'}
    headers = {k: v for k, v in request.headers.items() if k not in excluded}
    if viewer_cookie:
        headers['cookie'] = viewer_cookie
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0,connect=4.0), trust_env=False)
    req = client.build_request('GET', target, headers=headers)
    try:
        resp = await client.send(req, stream=True)
    except BaseException as error:
        await client.aclose()
        if isinstance(error, httpx.RequestError):
            raise HTTPException(503, '暂时无法连接可视化所属实例') from error
        raise

    async def body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP | {'set-cookie'}}
    return StreamingResponse(body(), status_code=resp.status_code, headers=headers)


@router.websocket("/viz-proxy/{sid}/ws")
async def ws_tunnel(ws: WebSocket, sid: int, token: str = ""):
    try:
        sess, remote, user_id = await _resolve(sid, token or ('' if ws.cookies.get(SESSION_COOKIE) else ws.cookies.get(viz_cookie(sid), '')), ws)
    except HTTPException:
        await ws.close(code=4401)
        return
    if remote:
        target = f'ws{remote.removeprefix("http")}/viz-proxy/{sid}/ws'
        headers = {'cookie': f'{viz_cookie(sid)}={issue(user_id, "viz", str(sid))}'}
    else:
        target = f'ws://127.0.0.1:{sess.port}/ws'
        headers = {}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15,connect=4)) as cs:
        try:
            async with cs.ws_connect(target, headers=headers, max_msg_size=64 * 1024 * 1024) as upstream:
                await ws.accept()

                async def browser_to_kernel():
                    try:
                        while True:
                            msg = await ws.receive()
                            if sess is not None:sess.touch()
                            if msg.get("bytes") is not None:
                                await upstream.send_bytes(msg["bytes"])
                            elif msg.get("text") is not None:
                                await upstream.send_str(msg["text"])
                            elif msg.get("type") == "websocket.disconnect":
                                break
                    except (WebSocketDisconnect, Exception):
                        pass

                async def kernel_to_browser():
                    async for msg in upstream:
                        if sess is not None:sess.touch()
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await ws.send_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            await ws.send_text(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

                tasks=[asyncio.create_task(browser_to_kernel()),asyncio.create_task(kernel_to_browser())]
                try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
        except Exception:
            pass
    try:
        await ws.close()
    except Exception:
        pass
