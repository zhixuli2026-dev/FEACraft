"""Real public inspection and recovery of a bounded page of sealed input history."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import httpx

from case_copy_source_lock_smoke import RUNTIME, read_observation
from seal_maintenance_page_smoke import (INPUT, PREFIX, SENTINEL, UNKNOWN, UNKNOWN_ID, boot, create,
                                         database, get, peer, process_exited, seed_writing)
from s3testsupport import ObjectStore
from testsupport import SERVER


LEGACY = '.vibeflow/attempts/manual-no-proof'


def endpoint(case_id, apply=False):
    return f'/cases/{case_id}/attempt-preparations'+(':recover' if apply else '')


def use_default_page(module):
    with module.open('a',encoding='utf8') as stream:
        stream.write('\nstorage_maintenance.SEAL_PAGE_SIZE = 32\n')


def request(client, case_id, *, apply=False, cursor=None, limit=None, status=200, headers=None):
    params = {key:value for key,value in {'cursor':cursor,'limit':limit}.items() if value is not None}
    response = client.request('POST' if apply else 'GET',endpoint(case_id,apply),params=params,headers=headers)
    assert response.status_code==status, (response.status_code,response.text[:700])
    return response.json()


def pages(client, case_id, *, apply=False, first=None):
    page = request(client,case_id,apply=apply,limit=1) if first is None else first
    seen, visited = set(), [page]
    while page['next_cursor'] is not None:
        cursor = page['next_cursor']
        assert cursor not in seen and len(seen)<20, visited
        seen.add(cursor)
        page = request(client,case_id,apply=apply,cursor=cursor,limit=1)
        visited.append(page)
    assert all(len(value['results'])+len(value.get('untracked',()))<=1 for value in visited), visited
    return visited


def client_for(api, token):
    return httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+token},
                        trust_env=False,timeout=75)


def from_abrupt_setup(settings, store, url, report):
    from app.services.process_identity_native import probe

    source, module = boot('manual-origin',settings,url)
    use_default_page(module)
    print('SEAL_MANUAL_ORIGIN',source.root,flush=True)
    try:
        with source:
            case = source.create_case('Manual seal diagnostics')
            case_id = case['id']
            prefix = source.call('GET',source.prefix)['storage_prefix']
            workspace = source.call('GET','/workspaces')[0]['id']
            other_case = source.call('POST',f'/workspaces/{workspace}/cases',{'name':'Unrelated seal case'})['id']
            root = source.env['VIBEFLOW_STORAGE_FS_ROOT']
            source.put('Ref/input.txt',INPUT.decode('utf8'))
            completed = source.call('POST',source.prefix+'/jobs',{
                'command':'echo SEAL_MANUAL_COMMITTED','input_paths':['Ref/input.txt']})
            terminal = source.wait_job(completed['id'],timeout=90)
            committed = terminal['attempt']['id']
            committed_input = '.vibeflow/attempts/'+committed+'/inputs/Ref/input.txt'
            assert get(root,store,prefix,committed_input)==INPUT
            native = source.call('GET',f'/jobs/{completed["id"]}/process')
            assert native['attempt_id']==committed and native['state']=='exited', native
            report['committed'] = {'job':completed['id'],'attempt':committed,'status':terminal['status'],
                                   'native_process':native['identity']['host'],'native_exit':native['state']}
            (source.root/'hold-seal').touch()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(source.call,'POST',source.prefix+'/jobs',{
                    'command':'echo SEAL_MANUAL_ABANDONED','input_paths':['Ref/input.txt']})
                held = read_observation(source,'seal-ready.json')
                attempt = held['attempt_id']
                assert attempt>UNKNOWN_ID and not pending.done() and probe(held['identity'])['state']=='match'
                record = PREFIX+'/pending/'+attempt+'.json'
                original = get(root,store,prefix,record)
                plan = json.loads(original)
                abandoned_input = plan['manifest']['inputs_path']+'/Ref/input.txt'
                assert get(root,store,prefix,abandoned_input)==INPUT
                source.proc.terminate()
                source.proc.wait(timeout=15)
                try:
                    pending.result(timeout=15)
                except (OSError,httpx.TransportError):
                    pass
                else:
                    raise AssertionError('Interrupted input preparation returned an HTTP result')
                report['origin'] = {'process':held['identity'],'port':source.port,
                                    'exit':process_exited(held['identity']),'attempt':attempt}
    finally:
        module.unlink(missing_ok=True)
    return source, case_id, other_case, prefix, root, completed['id'], committed_input, attempt, record, original, abandoned_input


def exercise(settings, store, url, baseline, report):
    (source,case_id,other_case,prefix,root,committed_job,committed_input,attempt,
     pending,original,abandoned_input) = from_abrupt_setup(settings,store,url,report)
    plan = json.loads(original)
    create(root,store,prefix,UNKNOWN,SENTINEL)
    create(root,store,prefix,LEGACY+'/unknown.bin',SENTINEL)
    if store is not None:
        create(root,None,prefix,LEGACY+'/unknown.bin',SENTINEL)
    else:
        seed_writing(root,prefix,plan)
    first, module = peer('manual-first',settings,url,source,cache_root=root)
    use_default_page(module)
    try:
        with first, client_for(first,source.token) as client:
            first_id = first.call('GET','/fixture/seal-process',api_prefix=False)
            assert request(client,other_case)=={'results':[],'untracked':[]}
            assert request(client,other_case,apply=True)=={'results':[]}
            view = report['first_view'] = request(client,case_id,limit=1)
            if baseline:
                assert len(view['results'])>1 and 'next_cursor' not in view, view
                old = report['old_one_request'] = request(client,case_id,apply=True,limit=1)
                assert any(row.get('attempt_id')==attempt and row['status']=='rolled_back' for row in old['results']), old
                assert get(root,store,prefix,pending) is None
            else:
                assert len(view['results'])+len(view['untracked'])<=1 and view['next_cursor'], view
                outsider = first.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
                    'password':'isolated-test-123','name':'Manual seal outsider'})['token']
                request(client,case_id,cursor=view['next_cursor'],status=404,headers={'Authorization':'Bearer '+outsider})
                request(client,other_case,cursor=view['next_cursor'],status=422)
                request(client,case_id,apply=True,cursor=view['next_cursor'],status=422)
                report['invalid_cursor_rejections'] = [404,422,422]
                assert get(root,store,prefix,pending)==original and get(root,store,prefix,abandoned_input)==INPUT
    finally:
        module.unlink(missing_ok=True)
    report['first'] = {'process':first_id,'port':first.port,'exit':process_exited(first_id)}

    if not baseline:
        resumed, module = peer('manual-resumed',settings,url,source,cache_root=root)
        use_default_page(module)
        try:
            with resumed, client_for(resumed,source.token) as client:
                resumed_id = resumed.call('GET','/fixture/seal-process',api_prefix=False)
                scanned = report['read_pages'] = pages(client,case_id,first=view)
                records = [row for value in scanned for row in value['results']]
                orphans = [row for value in scanned for row in value['untracked']]
                assert any(row.get('attempt_id')==attempt and row['status']=='uncommitted' for row in records), records
                assert any(row.get('record')==UNKNOWN and row['status']=='recovery_required' for row in records), records
                assert [row for row in orphans if row['path']==LEGACY]==[{'path':LEGACY,'status':'untracked_retained'}], orphans
                assert get(root,store,prefix,pending)==original and get(root,store,prefix,abandoned_input)==INPUT
                actions = report['recovery_pages'] = pages(client,case_id,apply=True)
                outcomes = [row for value in actions for row in value['results']]
                assert [row for row in outcomes if row.get('attempt_id')==attempt]==[
                    {'attempt_id':attempt,'status':'rolled_back'}], outcomes
                assert json.loads(get(root,store,prefix,PREFIX+'/history/'+attempt+'.json'))=={
                    'plan':plan,'outcome':'rolled_back'}
                assert get(root,store,prefix,pending) is None and get(root,store,prefix,abandoned_input) is None
                assert get(root,store,prefix,UNKNOWN)==SENTINEL and get(root,store,prefix,LEGACY+'/unknown.bin')==SENTINEL
                if store is not None:
                    assert get(root,None,prefix,LEGACY+'/unknown.bin')==SENTINEL
                assert get(root,store,prefix,'Ref/input.txt')==INPUT and get(root,store,prefix,committed_input)==INPUT
                jobs = resumed.call('GET',f'/cases/{case_id}/jobs')
                assert [item['id'] for item in jobs]==[committed_job], jobs
        finally:
            module.unlink(missing_ok=True)
        report['resumed'] = {'process':resumed_id,'port':resumed.port,'exit':process_exited(resumed_id)}


async def run(backend,kind,baseline):
    root = Path(tempfile.mkdtemp(prefix=f'seal-manual-{kind}-{backend}-',dir=SERVER.parents[1]/'tmp'))
    report = {'passed':False,'backend':backend,'database':kind,'baseline':baseline,'model_calls':0,'mock_llm_calls':0}
    print('SEAL_MANUAL_EVIDENCE',root,flush=True)
    context = ObjectStore(executable=RUNTIME,volume_max=24,authenticated=True) if backend=='s3' else nullcontext()
    try:
        async with database(kind,root) as url:
            with context as store:
                if store is not None:
                    report['store_receipt'] = str(store.root/'store.json')
                await asyncio.to_thread(exercise,store.settings() if store else {},store,url,baseline,report)
            if store is not None:
                report['store_exit'] = store.proc.poll()
                assert report['store_exit']==0
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__,'message':str(error)[:1600]}
        raise
    finally:
        (root/'seal-manual-page.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('SEAL_MANUAL_PASS',root,flush=True)


async def main():
    baseline = os.environ.get('VIBEFLOW_SEAL_MANUAL_PAGE_BASELINE')=='1'
    await run('fs','sqlite',baseline)
    if not baseline:
        await run('s3','sqlite',False)
        await run('fs','postgresql',False)
        await run('s3','postgresql',False)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    asyncio.run(main())
