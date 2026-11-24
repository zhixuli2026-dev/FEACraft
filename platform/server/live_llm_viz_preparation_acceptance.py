"""Observe staging and native rendering on an isolated S3 deployment."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time

import httpx

from live_llm_job_repeat_acceptance import main, put
from viz_execution_smoke import STL


FIXTURE = r'''
from pathlib import Path
from threading import Event, Lock
from fastapi import Depends
from sqlalchemy import func, select
from app.config import get_settings
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import NativeVizSession
from app.routers import viz
from app.services import request_deadline, staging_download
from app.services.node_storage_budget import DIRECTORY
from app.storage import _fs_root

viz_pause = None
viz_pause_lock = Lock()
viz_prepare_original = viz.prepare_job_staging
viz_download_original = staging_download.download


def pause_viz(mode):
    with viz_pause_lock:
        current = viz_pause
        active = request_deadline.current()
        if current is None or current['mode'] != mode or active is None or current['gate'].is_set():
            return
        current['deadlines'].append(active)
    if not current['gate'].wait(35):
        raise TimeoutError('The actual S3 fault barrier was not released')


def prepare_for_test(prefix, stamp):
    stage = viz_prepare_original(prefix, stamp)
    pause_viz('snapshot')
    return stage


def download_for_test(prefix, path, record, target):
    digest = viz_download_original(prefix, path, record, target)
    if path == 'D-post/surface.stl':
        pause_viz('download')
    return digest


viz.prepare_job_staging = prepare_for_test
staging_download.download = download_for_test


@app.post('/api/v1/testing/viz-preparation')
async def update_viz_preparation(body: dict, user=Depends(get_current_user)):
    global viz_pause
    with viz_pause_lock:
        if viz_pause is not None:
            viz_pause['gate'].set()
        mode = body['mode']
        assert mode in {'snapshot', 'download', 'off'}
        viz_pause = {'mode': mode, 'deadlines': [], 'gate': Event()} if mode != 'off' else None
        get_settings().viz_prepare_timeout_seconds = body['seconds']
    return {'mode': mode}


@app.get('/api/v1/testing/viz-preparation')
async def inspect_viz_preparation(user=Depends(get_current_user)):
    with viz_pause_lock:
        deadlines = list(viz_pause['deadlines']) if viz_pause else []
    async with SessionLocal() as db:
        rows = await db.scalar(select(func.count()).select_from(NativeVizSession))
    root = _fs_root()
    return {'blocked': len(deadlines), 'expired': bool(deadlines) and all(d.expired for d in deadlines),
        'stage_entries': len(list((root / '.staging').glob('*'))),
        'node_claims': len(list((root / DIRECTORY).glob('*.json'))), 'session_rows': rows}
'''


def scenario(api, *, store=None, **_):
    assert store is not None, 'This acceptance needs an actual S3 service'
    endpoint = api.prefix + '/viz:sessions'
    control = '/testing/viz-preparation'
    put(api, 'D-post/surface.stl', STL)
    put(api, 'Ref/source.txt', 'ORIGINAL STAGING SOURCE')

    def start():
        with httpx.Client(base_url=api.base+'/api/v1', trust_env=False, timeout=55,
                          headers={'Authorization': 'Bearer '+api.token}) as client:
            return client.post(endpoint, json={'file_path': 'D-post/surface.stl'})

    def arm(mode, seconds=100):
        assert api.call('POST', control, {'mode': mode, 'seconds': seconds})['mode'] == mode

    def wait_until(predicate, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = api.call('GET', control)
            if predicate(state):
                return state
            time.sleep(.08)
        raise AssertionError(f'Staging state did not arrive: {state}')

    with ThreadPoolExecutor(max_workers=3) as pool:
        arm('snapshot')
        stale = pool.submit(start)
        try:
            prepared = wait_until(lambda state: state['blocked'] == 1)
            assert prepared['stage_entries'] >= 2 and prepared['session_rows'] == 0, prepared
            # The write must complete before the old snapshot is released.
            write = pool.submit(put, api, 'Ref/source.txt', 'NEW STAGING SOURCE')
            write.result(timeout=10)
            assert not stale.done()
        finally:
            arm('off')
        response = stale.result(timeout=20)
        assert response.status_code == 409 and response.json()['detail']['code'] == 'input_stale', response.text
        assert api.call('GET', api.prefix+'/file?path=Ref/source.txt')['content'] == 'NEW STAGING SOURCE'
        state = api.call('GET', control)
        assert state['session_rows'] == state['stage_entries'] == state['node_claims'] == 0, state

        arm('download', seconds=14)
        slow = [pool.submit(start) for _ in range(2)]
        try:
            downloading = wait_until(lambda state: state['blocked'] == 2)
            assert downloading['session_rows'] == 0 and downloading['node_claims'] >= 2, downloading
            wait_until(lambda state: state['expired'])
            assert not any(request.done() for request in slow)
            refused = pool.submit(start).result(timeout=8)
            assert refused.status_code == 429 and refused.json()['detail']['code'] == 'viz_preparation_busy', refused.text
        finally:
            arm('off')
        for request in slow:
            response = request.result(timeout=20)
            assert response.status_code == 504 and response.json()['detail']['code'] == 'viz_preparation_timeout', response.text
        state = api.call('GET', control)
        assert state['session_rows'] == state['stage_entries'] == state['node_claims'] == 0, state

    response = start()
    assert response.status_code == 200, response.text
    kernel = response.json()
    assert kernel['phase'] == 'active' and kernel['alive'] is True
    sid = kernel['id']
    try:
        with httpx.Client(trust_env=False, timeout=20) as client:
            page = client.get(api.base+f'/viz-proxy/{sid}/index.html', headers={'Authorization': 'Bearer ' + api.token})
            assert page.status_code == 200 and 'trame' in page.text.lower()
    finally:
        api.call('DELETE', f'/viz:sessions/{sid}')
    receipt = api.call('GET', f'/viz:sessions/{sid}')
    assert receipt['phase'] == 'closed' and receipt['cleanup_verified'] is True and receipt['stop']['verified'] is True
    state = api.call('GET', control)
    assert state['session_rows'] == 1 and state['stage_entries'] == state['node_claims'] == 0, state
    report = {'concurrent_write': True, 'stale_snapshot_refused': True, 'blocked_downloads': 2,
        'deadline_waited_for_io': True, 'overload_status': 429, 'timeout_status': 504,
        'renderer_id': sid, 'real_trame': True, 'verified_stop': True, 'final_state': state}
    (api.root/'viz-preparation.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    return report


if __name__ == '__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE))
