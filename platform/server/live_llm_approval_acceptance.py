"""Real DeepSeek write proposals through the production approval API.

Explicit paid acceptance only; never supplies model replies or tool choices.
Run individual modes after a failure to avoid repeating successful live checks.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer, SERVER

RACE_MODES = {'race', 'replace_race', 'dict_race', 'create_race'}
DICT_CAPACITY_MODES = {'dict_capacity', 'dict_capacity_full', 'dict_capacity_update'}
FILE_CAPACITY_MODES = {'file_capacity', 'file_capacity_full', 'file_capacity_overwrite'}
CAPACITY_MODES = DICT_CAPACITY_MODES | FILE_CAPACITY_MODES
CAPACITY_BYTES = 40 * 1024 * 1024
AUTO_MODES = {'full', 'dict_capacity_full', 'file_capacity_full'}
WRITE_MODES = {'approve', 'full', 'quota'} | CAPACITY_MODES
REPLACEMENT_MODES = {'replace_race', 'dict_race', 'dict_capacity', 'dict_capacity_full',
                     'file_capacity', 'file_capacity_full'}
WRITE_BARRIER = '''
import asyncio
from contextlib import asynccontextmanager
from app.services import file_transactions, local_file_edits, s3_staging_transactions

async def pause_before_publish(prefix, changes):
    armed = root / 'pause-approved-path'
    if not armed.exists() or armed.read_text() not in changes:
        return
    armed.unlink()
    (root / 'journal-ready').write_text(prefix)
    async with asyncio.timeout(30):
        while not (root / 'external-saved').exists():
            await asyncio.sleep(.02)

original_write = file_transactions.write
async def pause_approved_write(db, case, changes, **kwargs):
    await pause_before_publish(case.s3_prefix, changes)
    return await original_write(db, case, changes, **kwargs)
file_transactions.write = pause_approved_write

original_local = local_file_edits.prepare_changes
@asynccontextmanager
async def pause_local(source, changes, *args, **kwargs):
    async with original_local(source, changes, *args, **kwargs) as prepared:
        await pause_before_publish(source.s3_prefix, changes)
        yield prepared
local_file_edits.prepare_changes = pause_local

original_remote = s3_staging_transactions.prepare
async def pause_remote(case, changes, *args, **kwargs):
    await pause_before_publish(case.s3_prefix, changes)
    return await original_remote(case, changes, *args, **kwargs)
s3_staging_transactions.prepare = pause_remote
'''

DICT_CAPACITY_OBSERVER = '''
import hashlib
import time
from contextlib import closing
from fastapi import Depends
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.case_events import CASE_EVENTS_PATH
from app.services.file_read import FileRead

history = {}
original_get = storage.get_bytes
original_chunks = FileRead.chunks

def pause_history(size, received, probe='approval-history'):
    witness = root/(probe+'-observed.json')
    if size!=40*1024*1024 or witness.exists():
        return
    partial = witness.with_suffix('.partial')
    partial.write_text(json.dumps({'size':size,'received':received}),encoding='utf8')
    partial.replace(witness)
    limit = time.monotonic()+25
    while not (root/(probe+'-release')).exists():
        if time.monotonic()>=limit:
            raise TimeoutError('Approval history probe was not released')
        time.sleep(.02)

def observe_get(prefix,path):
    data = original_get(prefix,path)
    if prefix==history.get('prefix') and path==CASE_EVENTS_PATH:
        pause_history(len(data),len(data))
    return data

def observe_chunks(reader,*args,**kwargs):
    prefix = history.get('prefix')
    path = CASE_EVENTS_PATH
    chosen = prefix and (reader.location['Key']==storage._s3_key(prefix,path) if reader.remote
                         else reader.path==storage._fs_path(prefix,path))
    with closing(original_chunks(reader,*args,**kwargs)) as chunks:
        for block in chunks:
            if chosen:
                pause_history(reader.size,len(block))
            yield block

storage.get_bytes, FileRead.chunks = observe_get, observe_chunks

@app.post('/api/v1/fixture/approval-history/{case_id}')
async def approval_history(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db)
    head, tail = b'{"data":"', b'"}'+bytes((10,))
    size = 40*1024*1024
    seeded = head+b'a'*(size-len(head)-len(tail))+tail
    storage.put_bytes(case.s3_prefix,CASE_EVENTS_PATH,seeded)
    history['prefix'] = case.s3_prefix
    return {'size':size,'sha256':hashlib.sha256(seeded).hexdigest()}
'''

FILE_CAPACITY_OBSERVER = '''
import os
from app.services import file_transactions, s3_live_store

observed_file = {}
previous_chunks = FileRead.chunks
capture_original = file_transactions._capture
s3_get_original = s3_live_store.Client.get_object

def watching(kind):
    return observed_file.get('kind')==kind and (root/'approval-file-armed').exists()

def observe_file_chunks(reader, *args, **kwargs):
    with closing(previous_chunks(reader, *args, **kwargs)) as blocks:
        for block in blocks:
            if watching('replace') and (
                reader.location['Key']==storage._s3_key(observed_file['prefix'], observed_file['path'])
                if reader.remote else reader.path==storage._fs_path(observed_file['prefix'], observed_file['path'])
            ):
                pause_history(reader.size,len(block),'approval-file')
            yield block
FileRead.chunks = observe_file_chunks

def observe_file_backup(path, destination=None, **options):
    if (watching('overwrite') and destination is not None and '.file-write-staging' in destination.parts
            and path==storage._fs_path(observed_file['prefix'],observed_file['path'])):
        size = path.stat().st_size
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('xb') as target:
            class BodyCopy:
                def write(self, block):
                    count = target.write(block)
                    pause_history(size,count,'approval-file')
                    return count
            value = capture_original(path,stream=BodyCopy(),**options)
            target.flush()
            os.fsync(target.fileno())
            return value
    return capture_original(path,destination,**options)
file_transactions._capture = observe_file_backup

def observe_s3_backup(self, **args):
    result = s3_get_original(self, **args)
    if watching('overwrite') and args['Key']==storage._s3_key(observed_file['prefix'],observed_file['path']):
        wrapped, size = result['Body'], result['ContentLength']
        class Body:
            def read(self, amount=None):
                block = wrapped.read(amount)
                if block:
                    pause_history(size,len(block),'approval-file')
                return block
            def close(self):
                wrapped.close()
        result['Body'] = Body()
    return result
s3_live_store.Client.get_object = observe_s3_backup

@app.post('/api/v1/fixture/approval-file/{case_id}')
async def approval_file_seed(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db)
    path, marker, kind = body['path'], body['marker'].encode(), body['kind']
    assert path in ('Ref/live-file_capacity.txt','Ref/live-file_capacity_full.txt','Ref/live-file_capacity_overwrite.txt')
    assert kind in ('replace','overwrite')
    size = 40*1024*1024
    data = marker+b'x'*(size-len(marker))
    storage.put_bytes(case.s3_prefix,path,data)
    observed_file.update(prefix=case.s3_prefix,path=path,kind=kind)
    return {'size':size,'sha256':hashlib.sha256(data).hexdigest()}
'''


def approval_overlap(api, approve, probe):
    workspace_id = api.call('GET','/workspaces')[0]['id']
    independent = api.call('POST',f'/workspaces/{workspace_id}/cases',
                           {'name':'Independent approval writer '+uuid.uuid4().hex[:8]})
    other = f'/cases/{independent["id"]}'
    witness = api.root/(probe+'-observed.json')
    release = api.root/(probe+'-release')
    armed = api.root/(probe+'-armed')
    witness.unlink(missing_ok=True)
    release.unlink(missing_ok=True)
    armed.touch()
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(approve)
        try:
            limit = time.monotonic()+25
            while not witness.exists():
                if pending.done():
                    raise AssertionError(('Approval did not reach the observed file',pending.result()))
                if time.monotonic()>limit:
                    raise TimeoutError('Approved write did not reach the observed file')
                time.sleep(.025)
            result = json.loads(witness.read_text(encoding='utf8'))
            assert result['size']==CAPACITY_BYTES
            with sqlite3.connect(api.root/'test.sqlite',timeout=.3) as db:
                try:
                    db.execute('BEGIN IMMEDIATE')
                    result['sqlite_writer_available'] = True
                except sqlite3.OperationalError as error:
                    assert 'locked' in str(error).lower(),error
                    result['sqlite_writer_available'] = False
            (api.root/(probe+'-lock.json')).write_text(json.dumps(result),encoding='utf8')
            assert result['sqlite_writer_available'] and 0<result['received']<result['size'], result
            content = {'path':'Ref/other-writer.txt','content':'Concurrent case save during actual proposal input read'}
            api.call('PUT',other+'/file',content)
            assert api.call('GET',other+'/raw?path='+content['path'],raw=True)==content['content'].encode()
            result['another_case_committed'] = True
        finally:
            release.touch()
            armed.unlink(missing_ok=True)
        events = pending.result(timeout=180)
    return events, result


def approval_through_history(api, case_id, approve):
    seed = api.call('POST',f'/fixture/approval-history/{case_id}')
    events, result = approval_overlap(api,approve,'approval-history')
    assert result['size']==seed['size']
    timeline = api.call('GET',api.prefix+'/raw?path=0-caseDict/case-events.jsonl',raw=True)
    assert hashlib.sha256(timeline[:seed['size']]).hexdigest()==seed['sha256']
    added = [json.loads(line) for line in timeline[seed['size']:].splitlines()]
    assert len(added)==1 and added[0]['type']=='caseDict.updated' and added[0]['source']=='agent',added
    provenance = api.call('GET',api.prefix+'/raw?path=0-caseDict/provenance.jsonl',raw=True)
    final = json.loads(provenance.splitlines()[-1])
    dictionary = api.call('GET',api.prefix+'/raw?path=0-caseDict/caseDict',raw=True)
    assert final['source']=='agent' and final['casedict_sha256']==hashlib.sha256(dictionary).hexdigest()
    assert final['revision']==added[0]['revision']
    result['revision'] = final['revision']
    result['history_prefix_preserved'] = True
    return events, result


def edit_after_approval(api, store, path, content, approve):
    ready, release = api.root/'journal-ready', api.root/'external-saved'
    ready.unlink(missing_ok=True)
    release.unlink(missing_ok=True)
    (api.root/'pause-approved-path').write_text(path)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(approve)
        try:
            until = time.monotonic()+40
            while not ready.exists() or not ready.read_text():
                if future.done():
                    raise AssertionError(('Approval never reached the write', future.result()))
                if time.monotonic()>until:
                    raise TimeoutError('Approval did not reach the file journal')
                time.sleep(.03)
            key = ready.read_text().rstrip('/')+'/'+path
            if store:
                store.client.put_object(Bucket=store.bucket, Key=key, Body=content.encode())
            else:
                (api.root/'files'/key).write_bytes(content.encode())
        finally:
            release.touch()
        return future.result(timeout=180)

S3_OBSERVE = '''
from fastapi import Depends
from sqlalchemy import select
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import CaseStorageOperation, OwnerStorageReservation, StorageWriteReservation
from app.services import owner_storage_budget as capacity,quota,remote_file_edits,storage_operations
original_quotas=quota.quotas_for
async def quotas(db,user):
 value=await original_quotas(db,user)
 if (root/'quota-zero').exists():return {**value,'storage_mb':0}
 return value
quota.quotas_for=quotas
original_file_save=remote_file_edits.save
async def observe_file_save(*args,on_commit=None,**kwargs):
 if on_commit is None:return await original_file_save(*args,**kwargs)
 def committed():
  lease=storage_operations.current()
  assert lease is not None and capacity.current() is None,'Agent save lacks isolated case credit'
  on_commit()
  receipt({'kind':'file_capacity','operation_id':lease.id,'case_id':lease.case.id})
 return await original_file_save(*args,on_commit=committed,**kwargs)
remote_file_edits.save=observe_file_save
observed_send=httpx.AsyncClient.send
async def outside_credit(self,request,*args,**kwargs):
 if request.url.host=='api.deepseek.com':
  assert capacity.current() is None and storage_operations.current() is None,'Storage credit leaked into inference'
 return await observed_send(self,request,*args,**kwargs)
httpx.AsyncClient.send=outside_credit
@app.get('/api/v1/fixture/storage-credit')
async def credits(user=Depends(get_current_user)):
 async with SessionLocal() as db:
  rows=list(await db.scalars(select(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user.id)))
  return [{'state':r.state,'receipt':json.loads(r.receipt_json)} for r in rows]

@app.get('/api/v1/fixture/dictionary-credit/{case_id}')
async def dictionary_credits(case_id:int,operation_id:str|None=None,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  from app.routers.workspaces import get_owned_case
  case=await get_owned_case(case_id,user,db,write=False)
  operations=select(CaseStorageOperation).where(CaseStorageOperation.case_id==case.id)
  transfers=select(StorageWriteReservation).where(StorageWriteReservation.case_id==case.id)
  if operation_id is not None:
   operations=operations.where(CaseStorageOperation.id==operation_id)
   transfers=transfers.where(StorageWriteReservation.operation_id==operation_id)
  operations=list(await db.scalars(operations))
  transfers=list(await db.scalars(transfers))
  return {'operations':[row.state for row in operations], 'transfers':[row.state for row in transfers]}
'''


def verify(configuration, modes, store=None, database_url=None, *, browser=False):
    api = IsolatedServer('live-deepseek-approval', app='live_fixture:app', database_url=database_url)
    bootstrap = ''
    if store is not None:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
        bootstrap = 'import os\nos.environ.update(' + repr(store.settings()) + ')\n'
    fixture = bootstrap + FIXTURE + (S3_OBSERVE if store else '')
    if RACE_MODES.intersection(modes):
        fixture += WRITE_BARRIER
    if CAPACITY_MODES.intersection(modes):
        fixture += DICT_CAPACITY_OBSERVER
    if FILE_CAPACITY_MODES.intersection(modes):
        fixture += FILE_CAPACITY_OBSERVER
    (api.root / 'live_fixture.py').write_text(fixture, encoding='utf8')
    result = dict(kind='real_llm_write_approval', simulated_llm=False,
                  source_database_modified=False, provider='deepseek',
                  model=configuration['model'], storage='s3' if store else 'fs',
                  database='postgresql' if database_url else 'sqlite',
                  passed=False, checks=[], scenarios={})

    def save():
        trace = api.root / 'upstream.jsonl'
        result['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        (api.root / 'live-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')

    def stream(client, path, body, label):
        events = []
        started = time.monotonic()
        try:
            with client.stream('POST', path, json=body) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if time.monotonic() - started > 180:
                        raise TimeoutError('Live stream exceeded 180 seconds')
                    if line.startswith('data:'):
                        events.append(json.loads(line[5:]))
                    if len(json.dumps(events)) > 2 * 1024 * 1024:
                        raise ValueError('Live evidence exceeded 2 MiB')
        finally:
            assert configuration['key'] not in json.dumps(events)
            (api.root / (label + '-events.json')).write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding='utf8')
            save()
        failures = [e for e in events if e.get('type') in {'stream.error', 'request.failed', 'request.timed_out'}]
        assert not failures, failures
        return events

    print('LIVE_LLM_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            case = api.create_case('Real DeepSeek write approval')
            profile = api.call('POST', '/provider-profiles', {
                'provider': 'deepseek', 'name': 'Real write acceptance',
                'base_url': configuration['base_url'], 'default_model': configuration['model'],
                'api_key': configuration['key']})
            assert configuration['key'] not in json.dumps(profile)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=180) as client:
                for mode in modes:
                    path = 'Ref/live-' + mode + '.txt'
                    before = 'BEFORE_' + uuid.uuid4().hex
                    after = 'AFTER_' + uuid.uuid4().hex
                    old_text = before
                    dictionary_edit = mode == 'dict_race' or mode in DICT_CAPACITY_MODES
                    large_file = mode in FILE_CAPACITY_MODES
                    automatic = mode in AUTO_MODES
                    source_file = None
                    if dictionary_edit:
                        path = '0-caseDict/caseDict'
                        dictionary = api.call('GET', api.prefix+'/caseDict')
                        before = dictionary['content']+'\n// '+old_text+'\n'
                        seeded = api.call('PUT', api.prefix+'/caseDict',
                            {'content': before, 'expected_revision': dictionary['dict_revision']})
                        revision = seeded['dict_revision']
                        if mode == 'dict_capacity_update':
                            after = before.replace(old_text, after, 1)
                    elif large_file:
                        source_file = api.call('POST',f'/fixture/approval-file/{case["id"]}',
                            {'path':path,'marker':before,'kind':'replace' if mode in REPLACEMENT_MODES else 'overwrite'})
                        assert source_file['size']==CAPACITY_BYTES
                        revision = api.call('GET',api.prefix+'/caseDict')['dict_revision']
                    elif mode == 'create_race':
                        before = None
                    else:
                        api.put(path, before)
                    conversation = api.call('POST', '/conversations', {
                        'case_id': case['id'], 'title': 'Live ' + mode,
                        'provider_profile_id': profile['id'], 'model': configuration['model'],
                        'permission_mode': 'full_access' if automatic else 'approve_writes'})
                    cid = conversation['id']
                    record = result['scenarios'][mode] = dict(conversation_id=cid, path=path, before=before, proposed=after)
                    before_state = source_file['sha256'] if large_file else before
                    if large_file:
                        record['original_file'] = source_file

                    def read():
                        if large_file:
                            body = api.call('GET',api.prefix+'/raw?path='+path,raw=True)
                            return hashlib.sha256(body).hexdigest()
                        response = client.get(api.prefix + '/file', params={'path': path})
                        if response.status_code == 404 and mode == 'create_race':
                            return None
                        response.raise_for_status()
                        return response.json()['content']

                    prompt = (f'这是隔离测试文件 {path}，内容已知为 {before}。请仅调用一次 propose_file_update，'
                              f'将文件完整内容替换为 {after}，不添加换行或其他文字，reason 写“真实审批验收”。'
                              '不要调用读取、搜索、计划、shell 或其他工具。等待用户审批；收到此工具结果后，'
                              '只用一句中文总结成功或失败，不再调用任何工具。若出现文件冲突，不重试、不生成新提案。')
                    tool = 'propose_file_update'
                    if mode == 'create_race':
                        prompt = (f'文件 {path} 尚不存在，请仅调用一次 propose_file_update 创建它，文件内容精确为 {after}。'
                                  '等待审批，收到结果后简述成功或失败，不重试，不调用其他工具。')
                    if mode in REPLACEMENT_MODES:
                        tool = 'propose_text_replacement'
                        prompt = (f'请仅调用一次 {tool}，对 {path} 将唯一的 {old_text} 替换为 {after}。'
                                  + ('' if automatic else '等待批准，')
                                  + '收到工具结果后只简短说明结果，不重试，不调用其他工具。')
                    if mode == 'dict_capacity_update':
                        prompt = (f'这是隔离算例的完整字典导入验收。只调用一次 {tool} 提议导入 {path}，'
                                  '给出的完整内容保留所有原有分区、参数和同一 workflowState A。'
                                  f'工具参数 content 精确采用这个 JSON 字符串的解码文本 {json.dumps(after)}。'
                                  '本项需要使用整文件导入工具。等待批准，收到结果后简短说明，不再调用工具。')
                    if mode == 'file_capacity_overwrite':
                        prompt = (f'这是可覆盖的隔离测试大文件 {path}，现有内容是测试填充。'
                                  f'请只调用一次 propose_file_update，把它完整替换为 {after}，'
                                  '不加换行或其他文字。用户批准前等待，收到工具结果后只简短总结。')
                    message = {
                        'content': prompt, 'provider_profile_id': profile['id'],
                        'model': configuration['model']}
                    def send_message():
                        return stream(client, f'/conversations/{cid}/messages', message, mode + '-proposal')
                    if mode == 'dict_capacity_full':
                        events, record['history_capacity'] = approval_through_history(api,case['id'],send_message)
                    elif mode == 'file_capacity_full':
                        events, record['file_capacity'] = approval_overlap(api,send_message,'approval-file')
                    else:
                        events = send_message()
                    proposals = [e['tool_call'] for e in events if e.get('type') == 'approval.requested']
                    saved = api.call('GET', f'/conversations/{cid}')
                    if automatic:
                        assert not proposals and not saved['pending_tools']
                        proposals = [t for t in saved['tool_calls'] if t['tool'] == tool]
                    assert len(proposals) == 1, [e.get('type') for e in events]
                    proposal = proposals[0]
                    assert proposal['tool'] == tool and proposal['args']['path'] == path, proposal
                    if mode in REPLACEMENT_MODES:
                        assert (proposal['args']['old_text'], proposal['args']['new_text']) == (old_text, after)
                    else:
                        assert proposal['args']['content'] == after, proposal
                    original_digest = (source_file['sha256'] if large_file else
                        hashlib.sha256(before.encode()).hexdigest() if before is not None else None)
                    assert proposal['args']['baseline_sha256'] == original_digest, proposal
                    expected_write = before.replace(old_text,after,1) if mode in REPLACEMENT_MODES else after
                    if large_file:
                        expected_bytes = after.encode()
                        if mode in REPLACEMENT_MODES:
                            expected_bytes += b'x'*(CAPACITY_BYTES-len(before.encode()))
                        expected_write = hashlib.sha256(expected_bytes).hexdigest()
                        record['expected_file'] = {'size':len(expected_bytes),'sha256':expected_write}
                    if automatic:
                        assert read() == expected_write
                        result['checks'].append(mode + ':real_model_tool_executed_without_approval')
                    else:
                        assert read() == before_state, 'File changed before approval'
                        assert len(saved['pending_tools']) == 1 and saved['pending_tools'][0]['id'] == proposal['id']
                        record['pending_file_unchanged'] = True
                        result['checks'].append(mode + ':real_proposal_waits_for_approval')
                    save()
                    print('LIVE_PASS ' + mode + (' automatic_write' if automatic else ' proposal_waits'), flush=True)
                    tool_path = f'/conversations/{cid}/tool-calls/{proposal["id"]}'
                    if mode == 'quota':
                        assert store is not None, 'Quota case requires actual S3 storage'
                        count_before = len(result['upstream'])
                        (api.root / 'quota-zero').touch()
                        try:
                            denied = client.post(tool_path + ':approve', json={})
                        finally:
                            (api.root / 'quota-zero').unlink()
                        assert denied.status_code == 429, (denied.status_code, denied.text)
                        save()
                        assert len(result['upstream']) == count_before, 'Quota rejection triggered inference'
                        assert read() == before
                        pending = api.call('GET', f'/conversations/{cid}')['pending_tools']
                        assert len(pending) == 1 and pending[0]['id'] == proposal['id']
                        record['quota_denied_before_claim'] = True
                        result['checks'].append('quota:no_write_no_model_pending_retained')
                    if mode == 'reject':
                        response = client.post(tool_path + ':reject')
                        response.raise_for_status()
                        assert response.json()['status'] == 'rejected'
                        assert read() == before
                    else:
                        expected = expected_write
                        if mode == 'stale':
                            expected = 'EXTERNAL_' + uuid.uuid4().hex
                            api.put(path, expected)
                            record['external'] = expected
                        if mode == 'legacy':
                            # Model chose the tool and arguments. Reproduce only
                            # the historical storage format, which lacked a digest.
                            args = dict(proposal['args'])
                            del args['baseline_sha256']
                            with sqlite3.connect(api.root/'test.sqlite') as db:
                                changed = db.execute("UPDATE tool_calls SET args=? WHERE id=? AND status='pending'",
                                                     (json.dumps(args), proposal['id']))
                                assert changed.rowcount == 1
                            expected = before
                        if mode == 'dict_race':
                            audit = api.call('GET', api.prefix+'/events')
                        conflict_mode = mode in {'stale','legacy'} or mode in RACE_MODES
                        if conflict_mode:
                            watermark = api.call('GET', api.prefix+'/workflow-events')['watermark']
                        try:
                            if mode in RACE_MODES:
                                expected = 'EXTERNAL_' + uuid.uuid4().hex
                                if mode == 'dict_race':
                                    expected = before+'\n// '+expected+'\n'
                                record['external'] = expected
                                def approve():
                                    if not browser:
                                        return stream(client, tool_path + ':approve', {}, mode + '-approved')
                                    setup = {'caseId': case['id'], 'userId': api.call('GET','/auth/me')['id'],
                                             'workspaceId': api.call('GET','/workspaces')[0]['id'],
                                             'conversationId': cid, 'folder': str(api.root),
                                             'approvalPath': '/api/v1'+tool_path+':approve'}
                                    subprocess.run(['node',str(SERVER/'approval_conflict_browser.cjs')], check=True,
                                        cwd=SERVER.parent/'web', timeout=150, env={**os.environ, 'VITE_API_TARGET': api.base,
                                        'VF_TEST_SESSION': api.token, 'VF_TEST_SETUP': json.dumps(setup)})
                                    return json.loads((api.root/'race-approved-events.json').read_text(encoding='utf8'))
                                events = edit_after_approval(api, store, path, expected, approve)
                            elif mode in DICT_CAPACITY_MODES and not automatic:
                                events, record['history_capacity'] = approval_through_history(api,case['id'],
                                    lambda:stream(client, tool_path+':approve', {}, mode+'-approved'))
                            elif large_file and not automatic:
                                events, record['file_capacity'] = approval_overlap(api,
                                    lambda:stream(client, tool_path+':approve', {}, mode+'-approved'),'approval-file')
                            elif not automatic:
                                events = stream(client, tool_path + ':approve', {}, mode + '-approved')
                        finally:
                            record['file_after_approval'] = read()
                            record['history_after_approval'] = api.call('GET', f'/conversations/{cid}')
                            save()
                        tool_result = next(e for e in events if e.get('type') == 'tool.result')
                        assert tool_result['ok'] == (mode in WRITE_MODES), tool_result
                        if conflict_mode:
                            assert 'CaseWriteConflict' in tool_result['result'], tool_result
                            logged, = [row for row in record['history_after_approval']['tool_calls'] if row['id']==proposal['id']]
                            assert logged['result'] == tool_result['result']
                            assert logged['args'] == (args if mode == 'legacy' else proposal['args'])
                            if mode == 'legacy':
                                assert '缺少文件基线' in logged['result']
                            page = api.call('GET', api.prefix+f'/workflow-events?after={watermark}')
                            assert not page['has_more'] and not page['blocked']
                            changes = [e for e in page['events'] if e['type']=='files.changed' and
                                       path in e['payload'].get('paths', [])]
                            committed = [e['seq'] for e in changes if e['payload'].get('source')!='file_watch']
                            observed = [e['seq'] for e in changes if e['payload'].get('source')=='file_watch']
                            assert not committed and not any(e['type']=='files.transaction_applied' for e in page['events'])
                            assert not api.call('GET', api.prefix+'/jobs')
                            assert not api.call('GET', api.prefix+'/geometry/publications')
                            record['conflict_effects'] = {'changed_events': committed, 'observed_external_events':observed,
                                                          'jobs': 0, 'geometry_publications': 0,
                                                          'file_sha256': hashlib.sha256(expected.encode()).hexdigest()}
                        if mode == 'dict_race':
                            assert api.call('GET', api.prefix+'/caseDict')['dict_revision'] == revision
                            assert api.call('GET', api.prefix+'/events') == audit
                            record['unchanged_revision'] = revision
                        if mode in DICT_CAPACITY_MODES:
                            assert api.call('GET', api.prefix+'/caseDict')['dict_revision']==revision+1
                            assert record['history_capacity']['revision']==revision+1
                        if large_file:
                            assert api.call('GET',api.prefix+'/caseDict')['dict_revision']==revision
                        assert read() == expected
                        assert any(e.get('type') == 'run.completed' for e in events), [e.get('type') for e in events]
                        assert any(e.get('type') == 'message.completed' for e in events)
                        record['tool_result'] = tool_result
                    saved = api.call('GET', f'/conversations/{cid}')
                    assert not saved['pending_tools'], saved['pending_tools']
                    history, = [e['payload'] for e in saved['events'] if e['type'] == 'tool_result' and
                                e['payload'].get('tool_call_id') == proposal['id'].split(':', 1)[-1]]
                    assert history.get('ok') == (mode in WRITE_MODES)
                    if mode in CAPACITY_MODES:
                        assert history.get('auto' if automatic else 'approved') is True
                        assert history['result']==tool_result['result']
                    save()
                    count_before = len(result['upstream'])
                    duplicate = client.post(tool_path + ':approve', json={})
                    assert duplicate.status_code == 409, duplicate.status_code
                    save()
                    assert len(result['upstream']) == count_before, 'Duplicate approval triggered a model request'
                    record.update(persisted=True, duplicate_approval_status=409, passed=True)
                    if store is not None:
                        credits = api.call('GET', '/fixture/storage-credit')
                        assert credits and all(row['state'] == 'settled' for row in credits), credits
                        record['storage_credit_settled'] = True
                        if mode in DICT_CAPACITY_MODES:
                            transfers = api.call('GET',f'/fixture/dictionary-credit/{case["id"]}')
                            assert all(rows and set(rows)=={'settled'} for rows in transfers.values()), transfers
                            record['dictionary_credit_settled'] = transfers
                        elif mode in WRITE_MODES:
                            completed = [row for row in result['upstream'] if row['kind']=='file_capacity'
                                         and row['case_id']==case['id']]
                            operation_id = completed[-1]['operation_id']
                            transfers = api.call('GET',f'/fixture/dictionary-credit/{case["id"]}?operation_id={operation_id}')
                            assert all(rows and set(rows)=={'settled'} for rows in transfers.values()), transfers
                            record['file_credit_settled'] = transfers
                    result['checks'].append(mode + ':effect_history_and_duplicate_verified')
                    save()
                    print('LIVE_PASS ' + mode + ' effect_history_duplicate', flush=True)
            result['passed'] = True
            save()
    except BaseException as error:
        result['passed'] = False
        result['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:1200]}
        save()
        print('LIVE_LLM_FAILED ' + json.dumps(result['failure'], ensure_ascii=False), flush=True)
        return 1
    print('LIVE_LLM_OK ' + str(api.root / 'live-result.json'), flush=True)
    return 0


async def postgres(configuration, modes, store, verifier=verify):
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    url = make_url(Settings(_env_file=SERVER / '.env').database_url)
    assert url.drivername == 'postgresql+asyncpg' and url.host in {'localhost', '127.0.0.1', '::1'}
    name = 'tmp_vf_liveapproval_' + uuid.uuid4().hex
    report = Path(tempfile.mkdtemp(prefix='live-approval-postgres-', dir=SERVER.parents[1] / 'tmp'))
    admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
                                 password=url.password, database='postgres', timeout=10)
    created = False
    try:
        await admin.execute('CREATE DATABASE "' + name + '"')
        created = True
        target = url.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic, target, report, 'upgrade', 'head')
        code = await asyncio.to_thread(verifier, configuration, modes, store, target)
    finally:
        try:
            if created:
                await admin.execute('DROP DATABASE "' + name + '"')
                assert not await admin.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', name)
                (report / 'cleanup.json').write_text(json.dumps({'database': name, 'temporary_database_removed': True}), encoding='utf8')
                print('LIVE_POSTGRES_CLEANUP ' + str(report / 'cleanup.json'), flush=True)
        finally:
            await admin.close()
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--modes', default='approve,reject,stale')
    parser.add_argument('--storage', choices=('fs', 's3'), default='fs')
    parser.add_argument('--database', choices=('sqlite', 'postgresql'), default='sqlite')
    parser.add_argument('--browser', action='store_true', help='Click the actual approval and reload its result in Chromium')
    args = parser.parse_args()
    modes = args.modes.split(',')
    if not modes or set(modes)-({'reject','stale','legacy'} | WRITE_MODES | RACE_MODES) or len(set(modes)) != len(modes):
        parser.error('Unrecognized or duplicate approval mode')
    if 'legacy' in modes and args.database != 'sqlite':
        parser.error('The historical row fixture requires an isolated SQLite database')
    if args.browser and (modes != ['race'] or args.storage!='fs' or args.database!='sqlite'):
        parser.error('Browser acceptance uses --modes race on FS/SQLite')
    if 'quota' in modes and args.storage != 's3':
        parser.error('quota mode requires --storage s3')
    capacity_modes = CAPACITY_MODES.intersection(modes)
    if capacity_modes and args.database != 'sqlite':
        parser.error('Dictionary capacity modes measure an independent SQLite writer')
    configuration = asyncio.run(source(args.source_profile_id))
    if args.storage == 's3':
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed',
                         authenticated=True,
                         volume_max=max(3,12*len(capacity_modes))) as store:
            code = asyncio.run(postgres(configuration, modes, store)) if args.database == 'postgresql' else verify(configuration, modes, store)
    else:
        code = asyncio.run(postgres(configuration, modes, None)) if args.database == 'postgresql' else verify(configuration, modes, browser=args.browser)
    sys.exit(code)
