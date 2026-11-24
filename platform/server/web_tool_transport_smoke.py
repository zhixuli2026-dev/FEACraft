"""HTTP transport and task lifecycle checks. This module makes no LLM requests."""

import asyncio
import json
import socket

import aiohttp
from aiohttp import web

from app.services.webfetch import MAX_FETCH_BYTES, PublicOnlyResolver, cancellable_web_fetch, safe_web_fetch
from app.services.websearch import WebSearchManager


class FixtureResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=0):
        assert host == 'test-server.invalid'
        return [{'hostname': host, 'host': '127.0.0.1', 'port': port, 'family': socket.AF_INET,
                 'proto': socket.IPPROTO_TCP, 'flags': socket.AI_NUMERICHOST}]

    async def close(self):
        pass


async def main():
    saw_start = asyncio.Event()
    closed = asyncio.Event()

    async def route(request):
        key = request.match_info['sample']
        if key == 'redirect':
            raise web.HTTPFound(f'http://127.0.0.1:{port}/pages')
        if key == 'compressed':
            return web.Response(text='blocked', headers={'Content-Encoding': 'gzip'})
        stream = web.StreamResponse(headers={'Content-Type': 'text/plain; charset=utf-8'})
        await stream.prepare(request)
        if key == 'pages':
            await stream.write(b'first block\n')
            await asyncio.sleep(.06)
            await stream.write(b'last block')
        elif key == 'huge':
            chunk = b'x' * (64 * 1024)
            try:
                for _ in range(MAX_FETCH_BYTES // len(chunk)):
                    await stream.write(chunk)
                await stream.write(b'overflow')
            except ConnectionError:
                return stream
        return stream

    app = web.Application()
    app.router.add_get('/{sample}', route)
    runner = web.AppRunner(app)
    await runner.setup()
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    port = server.getsockname()[1]
    await web.SockSite(runner, server).start()
    base = f'http://test-server.invalid:{port}/'
    try:
        options = {'resolver': FixtureResolver()}
        complete = await safe_web_fetch(base + 'pages', **options)
        assert complete['ok'] and complete['result'].endswith('first block\nlast block'), complete
        over = await safe_web_fetch(base + 'huge', **options)
        assert not over['ok'] and '8 MiB' in over['result'], over
        compressed = await safe_web_fetch(base + 'compressed', **options)
        assert not compressed['ok'] and '压缩响应' in compressed['result'], compressed
        direct = await safe_web_fetch(f'http://127.0.0.1:{port}/pages')
        redirected = await safe_web_fetch(base + 'redirect', **options)
        assert all(not result['ok'] and '非公网' in result['result'] for result in (direct, redirected))
        try:
            await PublicOnlyResolver().resolve('localhost', port)
        except ValueError:
            pass
        else:
            raise AssertionError('DNS returned an accepted loopback address')

        async def wait_until_cancelled(*args, **kwargs):
            try:
                saw_start.set()
                await asyncio.Future()
            finally:
                closed.set()

        async def check_cancel(start):
            saw_start.clear()
            closed.clear()
            job = asyncio.create_task(start())
            await asyncio.wait_for(saw_start.wait(), 3)
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass
            assert closed.is_set()

        class BlockedSearch:
            name = 'cancellation-boundary'
            search = wait_until_cancelled

        await check_cancel(lambda: cancellable_web_fetch(base, asyncio.Event(), fetcher=wait_until_cancelled))
        manager = WebSearchManager(BlockedSearch(), budget=1)
        await check_cancel(lambda: manager.execute('first-run', 1, 'test query', asyncio.Event()))
        assert not (await manager.execute('first-run', 2, 'foreign', asyncio.Event()))['ok']
        assert not (await manager.execute('first-run', 1, 'over budget', asyncio.Event()))['ok']
        await check_cancel(lambda: manager.execute('independent-run', 2, 'other query', asyncio.Event()))
        assert manager.snapshot('independent-run')['queries'] == 1
        manager.finish('first-run')
        assert manager.snapshot('first-run') is None
        print(json.dumps({'passed': True, 'model_calls': 0, 'fragmented_read': True, 'streaming_size_limit': True,
                          'origin_and_redirect_rejected': True, 'decompression_disabled': True,
                          'outer_cancellation_drains_provider': True, 'search_budget_isolated': True}))
    finally:
        await runner.cleanup()


if __name__=='__main__':
    asyncio.run(main())
