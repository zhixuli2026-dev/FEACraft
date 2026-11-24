"""Actual retired S3 sandbox with production frontend for cleanup UI checks."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import aiohttp
from sandbox_retirement_checks import verify
from s3testsupport import ObjectStore
from testsupport import SERVER


async def browser(api, sid, rid, proof):
    web = SERVER.parent / 'web'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    log = (api.root / 'web.log').open('w', encoding='utf8')
    process = subprocess.Popen([shutil.which('node'), str(web / 'node_modules/vite/bin/vite.js'),
        'preview', '--host', '127.0.0.1', '--port', str(port), '--strictPort'], cwd=web,
        env=dict(os.environ, VITE_API_TARGET=api.base), stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        base = 'http://127.0.0.1:' + str(port)
        async with aiohttp.ClientSession() as client:
            async with asyncio.timeout(30):
                while True:
                    assert process.poll() is None
                    try:
                        async with client.get(base) as response:
                            if response.status==200:
                                break
                    except aiohttp.ClientError:
                        pass
                    await asyncio.sleep(.1)
        user = await asyncio.to_thread(api.call, 'GET', '/auth/me')
        ready = {'web':base, 'api':api.base, 'case_id':api.case_id, 'case_name':'Retire actual dead sandbox',
            'stopped_session':sid, 'recovery_id':rid, 'root':str(api.root), 'email':user['email']}
        (api.root / 'ready.json').write_text(json.dumps(ready, indent=2))
        print('STAGING_UI_READY ' + str(api.root / 'ready.json'), flush=True)
        async with asyncio.timeout(1800):
            while True:
                assert process.poll() is None and api.proc.poll() is None
                control = api.root / 'control.json'
                if control.exists() and json.loads(control.read_text())['action']=='stop':
                    break
                await asyncio.sleep(.2)
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
        log.close()
    return 0


if __name__=='__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed') as store:
        asyncio.run(verify(store, after_retirement=browser))
