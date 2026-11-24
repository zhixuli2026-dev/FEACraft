"""Verify public StandardCase creation, project selection, permissions and restart."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from live_llm_failure_fallback_acceptance import kill_owned_api
from live_llm_job_repeat_acceptance import main
from live_llm_outbox_restart_acceptance import restart


PATHS = ('0-caseDict/caseDict', '0-caseDict/memory.md', 'A-geo/geometry.py', 'B-mesh/.gitkeep',
         'C-run/.gitkeep','D-post/postprocess.py','D-post/scenes.py','D-post/pvpython.py',
         'D-post/summary.md','E-report/report.md','UDF/README.md','Ref/.gitkeep','0-caseDict/case-events.jsonl')

FIXTURE = '''
import asyncio, os
from pathlib import Path
from fastapi import Depends
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.services.owner_storage_budget import hold
from app.services.storage_admission import lock_owner
root = Path(__file__).parent
(root/'case-create-server-pid').write_text(str(os.getpid()))
@app.post('/api/v1/testing/hold-creation-grant')
async def hold_creation_grant(user=Depends(get_current_user), db=Depends(get_db)):
    async with hold(db, user.id):
        await lock_owner(db, user.id, wait=True)
        settings = get_settings()
        previous = settings.storage_measurement_timeout_seconds
        try:
            if (root/'short-admission').exists():
                settings.storage_measurement_timeout_seconds = .4
            (root/'grant-acquired').touch()
            async with asyncio.timeout(15):
                while not (root/'release-grant').exists():
                    await asyncio.sleep(.02)
        finally:
            settings.storage_measurement_timeout_seconds = previous
            await db.commit()
    return {'released':True}
'''


def verify_admission(api, route):
    acquired, release, short = (api.root/key for key in ('grant-acquired','release-grant','short-admission'))
    elapsed = {}
    for limited in (True,False):
        if limited:
            short.touch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            held = pool.submit(api.call,'POST','/testing/hold-creation-grant')
            try:
                deadline = time.monotonic()+12
                while not acquired.exists():
                    if held.done():
                        held.result()
                    if time.monotonic()>deadline:
                        raise TimeoutError('Isolated grant was not acquired')
                    time.sleep(.02)
                start = time.monotonic()
                create = pool.submit(api.call,'POST',route,{'name':'等候账户写入额度'},expect=409 if limited else 200)
                if limited:
                    assert create.result(timeout=6)['detail']['code']=='storage_quota_busy'
                else:
                    time.sleep(.65)
                    assert not create.done()
                    release.touch()
                    assert create.result(timeout=12)['id']>0
                elapsed['timeout' if limited else 'success'] = round(time.monotonic()-start,3)
            finally:
                release.touch()
                assert held.result(timeout=15)['released']
        acquired.unlink()
        release.unlink()
        short.unlink(missing_ok=True)
        count = sum(c['name']=='等候账户写入额度' for c in api.call('GET',route))
        assert count == int(not limited)
    return elapsed


def inventory(api, cid):
    prefix = f'/cases/{cid}'
    entries = api.call('GET', prefix+'/tree')['entries']
    public = {row['path'] for row in entries}
    assert not set(PATHS)-public, sorted(set(PATHS)-public)
    files = {path:api.call('GET',prefix+'/raw?path='+path,raw=True) for path in PATHS}
    from app.services.casedict import parse_casedict
    parsed = parse_casedict(files['0-caseDict/caseDict'].decode())
    assert parsed['workflowState']=='A' and parsed['caseMeta']=={}
    meta = api.call('GET',prefix)
    assert meta['parse_status']=='ok' and meta['workflow_state']=='A' and meta['dict_revision']==0
    event = json.loads(files['0-caseDict/case-events.jsonl'])
    assert (event['case_id'], event['type'], event['revision'])==(cid,'case.created',0)
    for path, content in files.items():
        if path.endswith('.py'):
            compile(content, path, 'exec')
    assert all(files[path]==b'' for path in PATHS if path.endswith('.gitkeep'))
    return {path:hashlib.sha256(content).hexdigest() for path,content in files.items()}


def scenario(api, *, store=None, **_):
    ws = api.call('GET','/workspaces')[0]['id']
    route = f'/workspaces/{ws}/cases'
    original = api.call('POST',route,{'name':' 原有算例 '})
    assert original['name']=='原有算例' and original['updated_at']
    cid = original['id']
    hashes = inventory(api,cid)
    admission = verify_admission(api, route)
    for url in ('/workspaces',route):
        for name in (' ', 'a/b', 'a\\b', 'a\0b'):
            api.call('POST', url, {'name':name}, expect=400)
        for name in ('', 'x'*201):
            api.call('POST', url, {'name':name}, expect=422)

    def simultaneous(_):
        return api.call('POST',route,{'name':'同名并发'},expect=(200,409))
    with ThreadPoolExecutor(max_workers=2) as pool:
        both = list(pool.map(simultaneous,range(2)))
    assert sum('id' in row for row in both)==1, both
    duplicate = api.call('POST',route,{'name':' 同名并发 '},expect=409)
    assert duplicate['detail']=='同名算例已存在'
    assert sum(case['name']=='同名并发' for case in api.call('GET',route))==1

    email = uuid.uuid4().hex+'@example.com'
    member = api.call('POST','/auth/register',{'email':email,'password':'isolated-case-example','name':'协作者'})
    headers = {'Authorization':'Bearer '+member['token']}
    def other(method, path, body=None, expect=200):
        return api.call(method,path,body,expect=expect,extra_headers=headers)
    for path in (route, f'/cases/{cid}', f'/cases/{cid}/tree'):
        other('GET',path,expect=404)
    other('POST',route,{'name':'外部用户'},expect=404)
    org = api.call('GET','/org/mine')['org']['id']
    api.call('POST',f'/org/{org}/members',{'email':email,'role':'member'},expect=201)
    other('POST',f'/org/{org}/invitation:accept')
    for role in ('member','viewer'):
        api.call('PUT',f'/workspaces/{ws}/sharing',{'org_id':org,'role':role})
        shared = next(w for w in other('GET','/workspaces') if w['id']==ws)
        assert shared['shared'] and shared['access_role']==role
        assert any(c['id']==cid for c in other('GET',route))
        assert other('GET',f'/cases/{cid}/tree')['entries']
        other('POST',route,{'name':'仅所有者可建'},expect=404)
    browser_data = {'base':api.base,'ws':ws,'owner':api.token,'member':member['token'],
                    'output':str(api.root/'creation-browser.json')}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], str(Path(__file__).with_name('case_creation_browser.py'))],
        input=json.dumps(browser_data),text=True,check=True,timeout=210)
    observations = json.loads((api.root/'creation-browser.json').read_text())
    created_hashes = inventory(api,observations['late_case'])
    api.call('PUT',f'/workspaces/{ws}/sharing',{'org_id':org,'role':'viewer','enabled':False})
    other('GET',route,expect=404)
    other('GET',f'/cases/{cid}/tree',expect=404)
    pid = int((api.root/'case-create-server-pid').read_text())
    kill_owned_api(api,pid)
    restart(api)
    assert api.call('POST','/case-provisions:recover')['recovery_required']==0
    assert inventory(api,cid)==hashes and inventory(api,observations['late_case'])==created_hashes
    result = dict(storage='s3' if store else 'fs', invalid_requests=12,
        unique_concurrent_create=True, duplicate_after_trim=409, scaffold=hashes, contention_seconds=admission,
        permissions=['foreign denied','org member reads','org viewer reads','owner-only creation','revocation'],
        browser=observations, restart_preserved_two_inventories=True)
    # The shared real-model runner now reads its verification receipt from the new case.
    api.case_id = observations['late_case']
    api.prefix = f'/cases/{api.case_id}'
    (api.root/'case-creation.json').write_text(json.dumps(result,indent=2))
    return result


if __name__ == '__main__':
    bundle = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (bundle/'index.html').is_file()
    fixture = FIXTURE+"\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/', StaticFiles(directory="+repr(str(bundle))+", html=True))\n"
    asyncio.run(main(selected_override=scenario, fixture_override=fixture))
