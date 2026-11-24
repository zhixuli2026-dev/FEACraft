"""Export a real model conversation through two workers and the shipped browser UI."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import httpx

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main
from live_llm_outbox_restart_acceptance import restart
from live_llm_publication_restart_acceptance import worker_peer


FIXTURE = '''
import asyncio, time
from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.models import Conversation
from app.routers.agent import _append_event
from app.routers.workspaces import get_owned_case
from app.services import conversation_history
from app.services.backup_download import SpooledDownload

async def barrier(name):
    flag = root/name
    if flag.exists():
        (root/(name+'.observed')).touch()
        deadline = time.monotonic()+90
        while flag.exists():
            if time.monotonic()>deadline: raise TimeoutError(name)
            await asyncio.sleep(.05)

upstream_send = httpx.AsyncClient.send
async def observed_send(client, request, *args, **kwargs):
    response = await upstream_send(client, request, *args, **kwargs)
    if request.url.host == 'api.deepseek.com' and response.status_code==200:
        await barrier('hold-upstream')
    return response
httpx.AsyncClient.send = observed_send

archive_parts = conversation_history.archive_parts
async def observed_parts(*args):
    from contextlib import aclosing
    async with aclosing(archive_parts(*args)) as parts:
        async for part in parts:
            yield part
            await barrier('hold-snapshot')
conversation_history.archive_parts = observed_parts

write_archive = conversation_history.write_archive
async def measured_archive(*args):
    import tracemalloc
    from app.config import get_settings
    settings=get_settings()
    previous=settings.node_storage_limit_mb
    constrained=(root/'limit-staging').exists()
    measured=(root/'measure-staging').exists()
    if constrained:settings.node_storage_limit_mb=1
    if measured:tracemalloc.start()
    try:
        return await write_archive(*args)
    finally:
        if measured:
            (root/'peak-bytes').write_text(str(tracemalloc.get_traced_memory()[1]))
            tracemalloc.stop()
        if constrained:settings.node_storage_limit_mb=previous
conversation_history.write_archive=measured_archive

chunks = SpooledDownload.chunks
async def observed_chunks(self):
    first=True
    async for chunk in chunks(self):
        yield chunk
        if first:
            first=False
            (root/'spool-bytes').write_text(str(self._reserved))
            await barrier('hold-send')
SpooledDownload.chunks = observed_chunks

@app.post('/api/v1/testing/history/{cid}/note')
async def append_note(cid:int, user=Depends(get_current_user), db=Depends(get_db)):
    conv=await db.get(Conversation,cid)
    await get_owned_case(conv.case_id,user,db)
    await _append_event(db,cid,'system',{'kind':'note','text':'later note'})
    await db.commit()
    return {'ok':True}
'''


BROWSER = '''
import hashlib,json,sys
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
d=json.load(sys.stdin)
with sync_playwright() as driver:
    browser=driver.chromium.launch()
    try:
        ctx=browser.new_context(viewport={'width':1660,'height':1000},accept_downloads=True)
        ctx.add_init_script("if(location.origin==="+json.dumps(d['base'])+")localStorage.setItem('vf_token',"+json.dumps(d['token'])+");")
        page=ctx.new_page()
        page.set_default_timeout(45000)
        page.goto(d['base'],wait_until='domcontentloaded')
        page.locator('[data-case-name="'+d['case']+'"]').click()
        selector=page.get_by_test_id('agent-conversation')
        page.locator('.dv-tab:visible').filter(has_text='Agent').first.click()
        selector.wait_for()
        selector.select_option(str(d['cid']))
        page.locator('.agent-settings summary').click()
        with page.expect_download() as pending:
            page.get_by_test_id('agent-export').click()
        download=pending.value
        assert download.suggested_filename=='conversation-'+str(d['cid'])+'.jsonl'
        file=Path(d['root'])/download.suggested_filename
        download.save_as(file)
        assert hashlib.sha256(file.read_bytes()).hexdigest()==d['sha256']
        with page.expect_response(lambda res: '/conversations:import?' in res.url and res.request.method=='POST') as imported:
            page.get_by_test_id('agent-import-file').set_input_files(str(file))
        assert imported.value.ok
        result=imported.value.json()
        expect(selector).to_have_value(str(result['id']),timeout=30000)
        (file.parent/'conversation-browser.json').write_text(json.dumps({'chromium':browser.version,
            'download':download.suggested_filename,'sha256':d['sha256'],'imported':result['id'],
            'events':result['imported_events']}),encoding='utf8')
    finally:
        browser.close()
'''


def scenario(configuration, api, *, store=None, **_):
    name = 'Conversation export '+uuid.uuid4().hex[:8]
    api.create_case(name)
    profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Export acceptance',
                       'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
    def new(mode):
        return api.call('POST','/conversations',{'case_id':api.case_id,'title':'Export '+mode,
                        'permission_mode':mode,'model':configuration['model'],'provider_profile_id':profile['id']})['id']
    def url(cid):return f'/conversations/{cid}/export'
    def fetch(cid, expected=200, headers=None, worker=None):
        worker = worker or api
        deadline = time.monotonic()+15
        with httpx.Client(base_url=worker.base+'/api/v1',trust_env=False,timeout=120) as client:
            while True:
                response = client.get(url(cid),headers={'Authorization':'Bearer '+api.token,**(headers or {})})
                if response.status_code != 429 or response.json()['detail']['code']!='archive_busy':
                    assert response.status_code==expected, (cid, response.status_code, expected)
                    return response
                if time.monotonic()>=deadline:raise TimeoutError('Archive slot remained occupied')
                time.sleep(.1)
    def raw(cid):return fetch(cid).content
    def parse(data):
        assert configuration['key'].encode() not in data and api.token.encode() not in data
        return [json.loads(line) for line in data.splitlines()]
    def turn(cid,prompt):
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
            response=client.post(f'/conversations/{cid}/messages',json={'content':prompt,'model':configuration['model'],'provider_profile_id':profile['id']})
            response.raise_for_status()
            events=[json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
            assert not any(e['type'] in {'stream.error','request.failed','request.timed_out'} for e in events)
            return events
    first = 'EXPORT_'+uuid.uuid4().hex
    api.put('Ref/export-proof.txt',first)
    cid=new('plan')
    prefix=parse(raw(cid))[0]['session']['workingDirectory'].strip('/')
    cache_key=prefix+'/.vibeflow/codex-sessions/legacy.json'
    legacy_id='OLD_CACHE_'+uuid.uuid4().hex
    cache=json.dumps({'format':'legacy-codex','session':legacy_id}).encode()
    if store:
        store.raw_client.put_object(Bucket=store.bucket,Key=cache_key,Body=cache,IfNoneMatch='*')
        def read_cache():
            obj=store.raw_client.get_object(Bucket=store.bucket,Key=cache_key)
            with obj['Body'] as stream:return stream.read()
    else:
        path=api.root/'files'/cache_key
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as output:output.write(cache)
        read_cache=path.read_bytes
    api.proc.terminate();api.proc.wait(timeout=15);restart(api)
    assert read_cache()==cache and len(parse(raw(cid)))==1
    api.call('PATCH',f'/conversations/{cid}',{'title':'Archive and legacy-cache acceptance'})
    assert read_cache()==cache
    with worker_peer(api,store) as peer, ThreadPoolExecutor(max_workers=1) as pool:
        hold=api.root/'hold-upstream'
        hold.touch()
        run=pool.submit(turn,cid,'请只调用一次 read_text_file 读取 Ref/export-proof.txt，准确回复文件里的完整标识。')
        try:
            wait_for((api.root/'hold-upstream.observed').exists,'Actual provider response did not reach worker')
            busy=fetch(cid,409,worker=peer).json()
            assert busy['detail']['code']=='state_conflict'
        finally:
            hold.unlink()
        events=run.result()
        assert any(e['type']=='tool.result' and first in str(e.get('result')) for e in events)
        assert any(e['type']=='run.completed' for e in events)
        original=parse(raw(cid))
        first_answer=next(e['id'] for e in original[1:] if e['payload'].get('role')=='assistant' and first in e['payload'].get('content',''))
        api.call('PATCH',f'/conversations/{cid}/permission',{'permission_mode':'ask'})
        second='SECOND_'+uuid.uuid4().hex
        assert any(e['type']=='run.completed' for e in turn(cid,'请只回复 '+second))
        fork=api.call('POST',f'/conversations/{cid}/fork')['id']
        nested=api.call('POST',f'/conversations/{fork}/fork',{'head_event_id':first_answer})['id']
        cut=parse(raw(nested))[1:]
        assert cut and cut[-1]['id']==first_answer and all(e['id']<=first_answer for e in cut)
        assert second.encode() not in raw(nested)
        empty=new('ask')
        empty_branch=api.call('POST',f'/conversations/{empty}/fork')['id']
        api.call('POST',f'/testing/history/{empty}/note')
        assert len(parse(raw(empty_branch)))==1
        archive=raw(cid)
        lines=parse(archive)
        assert any(tc['tool']=='read_text_file' and tc['status']=='executed' for tc in lines[0]['tool_calls'])
        users=[e for e in lines[1:] if e['payload'].get('role')=='user']
        assert [e['mode'] for e in users]==['plan','ask'] and all(e['workingDirectory'] and e['model']==configuration['model'] for e in users)
        assert all(e['seq']==i and e['event_id']==e['eventId'] and e['created_at']==e['time'] for i,e in enumerate(lines[1:],1))
        denied=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other','password':'isolated-test-123'})
        fetch(cid,404,{'Authorization':'Bearer '+denied['token']})
        fetch(cid,401,{'Authorization':''})
        pending_id=new('approve_writes')
        proposal=turn(pending_id,'请仅调用一次 propose_file_update，在 Ref/approval-note.txt 新建文本 approved，等待用户批准。')
        proposed=next(e['tool_call']['id'] for e in proposal if e['type']=='approval.requested')
        assert '工具' in fetch(pending_id,409,worker=peer).json()['detail']
        assert api.call('POST',f'/conversations/{pending_id}/tool-calls/{proposed}:reject')['status']=='rejected'
        assert parse(raw(pending_id))[0]['tool_calls'][0]['status']=='rejected'
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=120,trust_env=False) as client:
            # Plain source notes exercise archive size without generating model history.
            notes=[{'type':'system','id':i+1,'payload':{'kind':'note','text':str(i)+'x'*16384}} for i in range(360)]
            notes.append({'type':'system','id':361,'payload':{'kind':'summary','covers_max_id':350,'text':'Review notes'}})
            response=client.post(f'/conversations:import?case_id={api.case_id}',content=b'\n'.join(json.dumps(n).encode() for n in notes))
            response.raise_for_status()
            bulk=response.json()['id']
            hold=api.root/'hold-snapshot';hold.touch()
            pending=pool.submit(raw,bulk)
            try:
                wait_for((api.root/'hold-snapshot.observed').exists,'Export snapshot did not open')
                api.call('POST',f'/testing/history/{bulk}/note')
            finally:
                hold.unlink()
            snapshot=parse(pending.result())
            assert len(snapshot)==362 and snapshot[-1]['payload']['covers_max_id']==snapshot[350]['id']
            limited=api.root/'limit-staging';limited.touch()
            try:
                assert fetch(bulk,409).json()['detail']['code']=='node_storage_full'
                assert raw(cid)==archive
            finally:limited.unlink()
            measured=api.root/'measure-staging';measured.touch()
            try:answer=fetch(bulk)
            finally:measured.unlink()
            assert answer.headers['content-type'].startswith('application/x-ndjson') and int(answer.headers['content-length'])==len(answer.content)
            assert len(parse(answer.content))==363 and parse(answer.content)[-1]['payload']['text']=='later note'
            size=len(answer.content)
            peak=int((api.root/'peak-bytes').read_text())
            assert 0<peak<size,(peak,size)
            imported_empty=client.post(f'/conversations:import?case_id={api.case_id}',content=raw(empty_branch))
            imported_empty.raise_for_status()
            assert imported_empty.json()['imported_events']==0
            legacy=[{'type':'message','payload':users[0]['payload']},
                    {'type':'tool','payload':next(e['payload'] for e in lines[1:] if e['type']=='tool_result')},
                    {'type':'request','payload':next(e['payload'] for e in lines[1:] if e['type']=='request_state')}]
            old=b'\n'.join(json.dumps(e).encode() for e in legacy)
            restored=client.post(f'/conversations:import?case_id={api.case_id}',content=old)
            restored.raise_for_status()
            converted=parse(raw(restored.json()['id']))
            assert [e['type'] for e in converted[1:]]==['message','tool_result','request']
            before=api.call('GET',f'/conversations?caseId={api.case_id}')
            assert client.post(f'/conversations:import?case_id={api.case_id}',content=old+b'\n{').status_code==400
            assert api.call('GET',f'/conversations?caseId={api.case_id}')==before
            wait_for(lambda:not list((api.root/'files'/'.node-storage-budget').glob('*.json')),'Finished export retained disk claims')
            hold=api.root/'hold-send';hold.touch()
            try:
                with client.stream('GET',url(bulk)) as resp:
                    body=resp.iter_bytes()
                    assert resp.status_code==200 and next(body)
                    wait_for((api.root/'hold-send.observed').exists,'Staged response did not reach client')
                    reserved=int((api.root/'spool-bytes').read_text())
                    assert reserved>=size
                    assert peer.call('GET',url(cid),expect=429)['detail']['code']=='archive_busy'
            finally:hold.unlink()
            wait_for(lambda:not list((api.root/'files'/'.node-storage-budget').glob('*.json')),'Disconnected export retained disk claims')
    api.proc.terminate();api.proc.wait(timeout=15);restart(api)
    assert raw(cid)==archive
    assert read_cache()==cache and legacy_id.encode() not in archive
    browser={'base':api.base,'token':api.token,'case':name,'cid':cid,'root':str(api.root),'sha256':hashlib.sha256(archive).hexdigest()}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],'-c',BROWSER],input=json.dumps(browser),text=True,timeout=180,check=True)
    ui=json.loads((api.root/'conversation-browser.json').read_text(encoding='utf8'))
    imported=parse(raw(ui['imported']))
    assert ui['events']==len(lines)-1 and imported[0]['session']['permission']=='approve_writes' and imported[0]['session']['provider_profile_id'] is None
    assert not imported[0]['tool_calls']
    for old, restored_event in zip(lines[1:],imported[1:],strict=True):
        payload=old['payload']
        if old['type'] in {'tool_state','tool_result'} and payload.get('status') in {'pending','queued','running','awaiting_approval'}:
            payload={**payload,'imported_status':payload['status'],'status':'interrupted'}
        assert old['type']==restored_event['type'] and payload==restored_event['payload'],old['type']
    before={item['id'] for item in api.call('GET',f'/conversations?caseId={api.case_id}')}
    api.call('DELETE',f'/conversations/{cid}',expect=409)
    for target in (nested,fork,cid):
        assert api.call('DELETE',f'/conversations/{target}')['ok']
        api.call('GET',f'/conversations/{target}',expect=404)
        assert read_cache()==cache
    after={item['id'] for item in api.call('GET',f'/conversations?caseId={api.case_id}')}
    assert after==before-{nested,fork,cid}
    assert legacy_id.encode() not in raw(ui['imported'])
    report={'passed':True,'database':'postgresql' if api.database_url else 'sqlite','storage':'s3' if store else 'fs',
            'active_peer_status':409,'nested_branch_events':len(cut),'empty_branch_events':0,
            'model_modes':[e['mode'] for e in users],'real_tool':lines[0]['tool_calls'][0]['tool'],
            'pending_tool_status':409,'after_rejection':'rejected','legacy_roundtrip':True,
            'empty_roundtrip':True,'invalid_import_atomic':True,
            'old_cache_sha256':hashlib.sha256(cache).hexdigest(),'old_cache_preserved_after_rename_restart_delete':True,
            'legacy_cache_auto_imports':0,'removed_sql_conversations':3,
            'snapshot_events':len(snapshot)-1,'later_snapshot_events':len(snapshot),'download_bytes':size,'spool_reserved':reserved,
            'traced_peak_bytes':peak,'limited_node_status':409,
            'busy_peer_status':429,'after_disconnect_reservations':0,'restart_identical':True,'browser':ui}
    (api.root/'conversation-export.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__=='__main__':
    bundle=Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (bundle/'index.html').is_file()
    mount="\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(bundle))+",html=True))\n"
    asyncio.run(main(selected_factory=lambda config:partial(scenario,config),fixture_override=FIXTURE+mount))
