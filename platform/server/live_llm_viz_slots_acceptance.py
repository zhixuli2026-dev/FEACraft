"""Cross-API renderer quota with real VTK kernels and a damaged historical receipt."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time
import httpx

from live_llm_job_repeat_acceptance import main
from live_llm_publication_restart_acceptance import worker_peer
from viz_execution_smoke import STL


BROWSER = """
import json,sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
data=json.load(sys.stdin)
with sync_playwright() as play:
    browser=play.chromium.launch()
    try:
        page=browser.new_page(viewport={'width':1600,'height':1000},locale='zh-CN')
        page.add_init_script("localStorage.setItem('vf_token',"+json.dumps(data['token'])+");")
        page.goto(data['base'],wait_until='domcontentloaded')
        page.locator('.dv-tab').filter(has_text='设置').first.click(timeout=30000)
        viz=page.get_by_text('并发可视化会话',exact=True).locator('..')
        job=page.get_by_text('并发作业',exact=True).locator('..')
        expect(viz).to_contain_text('1 / 1',timeout=30000)
        expect(job).to_contain_text('0 / 4')
        viz.locator('..').screenshot(path=str(Path(data['root'])/'renderer-quota.png'))
    finally:
        browser.close()
"""


FIXTURE = '''
from fastapi import Depends
from sqlalchemy import select
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Plan, NativeVizSession
saved_stops = {}
@app.post('/api/v1/testing/renderer-limit')
async def render_limit(user=Depends(get_current_user)):
    async with SessionLocal() as db:
        plan=(await db.scalars(select(Plan).where(Plan.code=='free'))).one()
        quotas=json.loads(plan.quotas);quotas['viz_concurrent']=1
        plan.quotas=json.dumps(quotas);await db.commit()
        return {'limit':1}

@app.post('/api/v1/testing/renderer-history/{sid}/{action}')
async def renderer_history(sid:int,action:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeVizSession,sid)
        assert row.created_by==user.id and row.phase=='closed'
        if action=='damage':
            assert json.loads(row.stop_json)['verified'] and sid not in saved_stops
            saved_stops[sid]=row.stop_json;row.stop_json='{}'
        else:
            assert action=='restore' and row.stop_json=='{}'
            row.stop_json=saved_stops.pop(sid)
        await db.commit()
        return {'action':action}
'''


def scenario(api, *, store=None, **_):
    workspace=api.call('GET','/workspaces')[0]['id']
    paths=[api.prefix,'/cases/'+str(api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Second renderer'})['id'])]
    for path in paths:
        api.call('PUT',path+'/file',{'path':'D-post/surface.stl','content':STL})
    api.call('POST','/testing/renderer-limit',{})
    def slots(server):
        usage=server.call('GET','/org/mine')['usage']
        assert usage['quotas']['viz_concurrent']==1
        return usage['occupied_viz_slots']
    def start(server,path):
        deadline=time.monotonic()+35
        while True:
            result=server.call('POST',path+'/viz:sessions',{'file_path':'D-post/surface.stl'},expect=(200,409,429))
            if not isinstance(result.get('detail'),dict):return result
            assert result['detail']['code'] in {'storage_quota_busy','validation_busy'} and time.monotonic()<deadline
            time.sleep(.1)
    def close(server,sid):
        server.call('DELETE',f'/viz:sessions/{sid}')
        row=server.call('GET',f'/viz:sessions/{sid}')
        assert row['phase']=='closed' and row['cleanup_verified']
        return row['stop']['verified']
    with worker_peer(api,store) as peer:
        servers=[api,peer]
        assert slots(api)==slots(peer)==0
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests=[pool.submit(start,server,path) for server,path in zip(servers,paths)]
            outcomes=[request.result() for request in requests]
        winners=[i for i,outcome in enumerate(outcomes) if outcome.get('alive')]
        assert len(winners)==1,outcomes
        win=winners[0];other=1-win
        assert '并发可视化会话' in outcomes[other]['detail']
        sid=outcomes[win]['id']
        try:
            assert slots(api)==slots(peer)==1
            if os.environ.get('VIBEFLOW_TEST_WEB_DIST'):
                data={'token':api.token,'base':api.base,'root':str(api.root)}
                subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],'-c',BROWSER],
                               input=json.dumps(data),text=True,timeout=65,check=True)
            with httpx.Client(trust_env=False,timeout=20) as client:
                page=client.get(servers[win].base+f'/viz-proxy/{sid}/index.html', headers={'Authorization': 'Bearer ' + api.token})
                assert page.status_code==200 and b'trame' in page.content.lower()
        finally:
            close(servers[win],sid)
        assert slots(api)==slots(peer)==0
        # Retain a terminal row whose previously observed renderer has lost its
        # stop receipt. Only this isolated fixture restores the exact old bytes.
        api.call('POST',f'/testing/renderer-history/{sid}/damage',{})
        try:
            assert peer.call('GET',f'/viz:sessions/{sid}')['phase']=='evidence_invalid'
            assert slots(peer)==1
            refusal=start(servers[other],paths[other])
            assert '并发可视化会话' in refusal['detail']
        finally:
            api.call('POST',f'/testing/renderer-history/{sid}/restore',{})
        assert slots(peer)==0
        successor=start(servers[other],paths[other])
        assert successor['alive'] and successor['id']!=sid
        assert close(servers[other],successor['id']) and slots(api)==0
    result={'storage':'s3' if store else 'fs','concurrent_requests':2,'kernels_created_during_race':1,
            'first_renderer':sid,'successor':successor['id'],'proxy_trame_verified':True,
            'occupied_with_damaged_history':1,'second_admission_refused':True,'both_stops_verified':True,'final_slots':0}
    result['browser_verified']=(api.root/'renderer-quota.png').is_file()
    (api.root/'viz-slots.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    fixture=FIXTURE
    if os.environ.get('VIBEFLOW_TEST_WEB_DIST'):
        bundle=Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
        assert (bundle/'index.html').is_file()
        fixture+="\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(bundle))+",html=True))\n"
    asyncio.run(main(selected_override=scenario,fixture_override=fixture))
