"""Actual text replacement, rejection, memory update and recall with real S3."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import sys
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from live_llm_approval_acceptance import S3_OBSERVE, postgres
from testsupport import IsolatedServer, SERVER

OBSERVE = '''
import asyncio
from app import storage
from app.services import s3_live_store
from contextlib import closing
from contextvars import ContextVar
from threading import Lock
from app.services.agent_file_reads import stop_signal
from app.services.case_write import CaseWriteConflict
from app.services.file_read import FileRead
from app.services import memory, ordinary_file_edits
from app.services.memory import MAX_MEMORY_BYTES, MEMORY_PATH
from app.services.tools import ToolExecutor
original_fence=s3_live_store.fence
def fence(prefix,*args,**kwargs):
 lease=storage_operations.current()
 assert capacity.current() is not None or (lease is not None and lease.case.s3_prefix==prefix), 'Recovery fence lacks storage admission'
 receipt({'kind':'credited_recovery_fence','admission':'case_lease' if lease is not None else 'owner_credit'})
 return original_fence(prefix,*args,**kwargs)
s3_live_store.fence=fence
original_case_tool=ToolExecutor.execute_case_tool
async def case_tool(self,db,case,name,args,**kwargs):
 if name=='update_memory':assert capacity.current() is not None,'Memory write lacks credit'
 return await original_case_tool(self,db,case,name,args,**kwargs)
ToolExecutor.execute_case_tool=case_tool
original_read=FileRead.read
def selected_read(self,start=None,end=None):
 data=original_read(self,start,end)
 if (stop_signal.get() is not None and self.remote and self.size>MAX_MEMORY_BYTES
     and self.location['Key'].endswith('/'+MEMORY_PATH)):
  receipt({'kind':'prompt_memory_range','source_bytes':self.size,'start':start,'end':end,'body_bytes':len(data)})
 return data
FileRead.read=selected_read
deriving=ContextVar('memory_edit_fixture', default=False)
original_prepare=memory.prepare_memory_edit
pause_selection=Lock()
def pause_write(observation):
 hold=root/'hold-memory-write'
 ready=root/'memory-write-ready.json'
 release=root/'release-memory-write'
 with pause_selection:
  if not hold.exists() or ready.exists():return
  staged=ready.with_suffix('.tmp')
  staged.write_text(json.dumps(observation),encoding='utf8')
  staged.replace(ready)
 deadline=time.monotonic()+(40 if (root/'race-key').exists() else 20)
 while not release.exists():
  if time.monotonic()>=deadline:raise TimeoutError('Actual memory read was not released')
  time.sleep(.025)

def prepare(*args,**kwargs):
 token=deriving.set(True)
 try:
  result=original_prepare(*args,**kwargs)
  if (root/'race-key').exists() and not storage._use_s3():
   # Windows may restrict replacement while a reader still has a file handle.
   pause_write({'phase':'after-derivation'})
  return result
 finally:deriving.reset(token)
memory.prepare_memory_edit=prepare
original_save=ordinary_file_edits.save
async def save(db,case,checked,path,build,**kwargs):
 try:return await original_save(db,case,checked,path,build,**kwargs)
 except CaseWriteConflict as error:
  if path==MEMORY_PATH and (root/'race-key').exists():
   receipt({'kind':'memory_write_conflict','code':error.code})
  raise
ordinary_file_edits.save=save
original_chunks=FileRead.chunks
def held_chunks(self,*args,**kwargs):
 race=(root/'race-key').exists()
 with closing(original_chunks(self,*args,**kwargs)) as stream:
  for block in stream:
   if deriving.get() and (not race or self.remote):
    pause_write({'source_bytes':self.size,'body_bytes':len(block),'phase':'body-read'})
   yield block
FileRead.chunks=held_chunks
memory_send=httpx.AsyncClient.send
first_model_requests=0
first_model_ready=asyncio.Event()
async def observed_memory(self,request,*args,**kwargs):
 global first_model_requests
 proof=root/'memory-proof'
 if request.url.host=='api.deepseek.com' and request.url.path.endswith('/chat/completions') and proof.exists():
  payload=request.content.decode('utf8')
  obsolete=root/'obsolete-memory-proof'
  receipt({'kind':'memory_in_request','exact_marker_present':proof.read_text() in payload,
   'obsolete_marker_present':obsolete.exists() and obsolete.read_text() in payload})
  race=root/'race-key'
  if race.exists() and race.read_text() in payload and not any(row['role']=='tool' for row in json.loads(payload)['messages']):
   first_model_requests+=1
   if first_model_requests==2:first_model_ready.set()
   async with asyncio.timeout(12):await first_model_ready.wait()
 return await memory_send(self,request,*args,**kwargs)
httpx.AsyncClient.send=observed_memory
'''


def verify(configuration, modes, store, database_url=None):
    api = IsolatedServer('live-deepseek-edit-memory', app='live_fixture:app', database_url=database_url)
    settings = store.settings() if store else {}
    if 'race' in modes:
        settings['VIBEFLOW_STORAGE_MEASUREMENT_TIMEOUT_SECONDS'] = '65'
    bootstrap = 'import os\nos.environ.update(' + repr(settings) + ')\n'
    (api.root / 'live_fixture.py').write_text(bootstrap + FIXTURE + S3_OBSERVE + OBSERVE, encoding='utf8')
    result = dict(passed=False, simulated_llm=False, database='postgresql' if database_url else 'sqlite',
                  storage='s3' if store else 'fs', model=configuration['model'], scenarios={})

    def save():
        trace = api.root / 'upstream.jsonl'
        result['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        (api.root / 'live-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')

    def stream(client, path, body, label):
        events = []
        try:
            with client.stream('POST', path, json=body) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith('data:'):
                        events.append(json.loads(line[5:]))
                    if len(json.dumps(events)) > 2 * 1024 * 1024:
                        raise ValueError('Evidence exceeds 2 MiB')
        finally:
            assert configuration['key'] not in json.dumps(events)
            (api.root / (label + '-events.json')).write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding='utf8')
            save()
        assert not [e for e in events if e.get('type') in {'stream.error', 'request.failed', 'request.timed_out'}], events[-4:]
        return events

    print('LIVE_EDIT_MEMORY_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            case = api.create_case('Real edits and memory')
            profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Real edits and memory',
                'base_url': configuration['base_url'], 'default_model': configuration['model'], 'api_key': configuration['key']})
            memory = 'MEMORY_' + uuid.uuid4().hex
            (api.root / 'memory-proof').write_text(memory)
            if 'recall' in modes and 'memory' not in modes:
                api.put('0-caseDict/memory.md', memory + '\n')
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=180) as client:
                conversations = []
                for mode in modes:
                    path = 'Ref/' + mode + '.txt'
                    old, new = 'OLD_' + uuid.uuid4().hex, 'NEW_' + uuid.uuid4().hex
                    before = 'before\n' + old + '\nafter\n'
                    if mode in {'replace', 'reject'}:
                        api.put(path, before)
                    conv = api.call('POST', '/conversations', {'case_id': case['id'], 'title': mode,
                        'provider_profile_id': profile['id'], 'model': configuration['model'],
                        'permission_mode': 'ask' if mode in {'recall','retention'} else 'approve_writes'})
                    cid = conv['id']; conversations.append(cid)
                    if mode=='race':
                        from live_llm_failure_fallback_acceptance import wait_for

                        second = api.call('POST','/conversations', {'case_id':case['id'],'title':'Concurrent memory peer',
                            'provider_profile_id':profile['id'],'model':configuration['model'],'permission_mode':'approve_writes'})
                        conversations.append(second['id'])
                        api.put('0-caseDict/memory.md', 'concurrent starting line\n')
                        (api.root/'memory-write-ready.json').unlink(missing_ok=True)
                        release, hold = api.root/'release-memory-write', api.root/'hold-memory-write'
                        release.unlink(missing_ok=True)
                        hold.touch()
                        race_key = 'MEMRACE_'+uuid.uuid4().hex
                        (api.root/'race-key').write_text(race_key,encoding='utf8')
                        additions = ['FIRST_'+uuid.uuid4().hex, 'SECOND_'+uuid.uuid4().hex]

                        def submit_call(run_cid, line):
                            prompt = f'这是并发验收 {race_key}。请仅调用一次 update_memory，action=append，content 只写 {line}。收到结果后直接报告状态。'
                            return stream(client,f'/conversations/{run_cid}/messages',
                                          {'content':prompt,'provider_profile_id':profile['id'],'model':configuration['model']},line)

                        with ThreadPoolExecutor(max_workers=2) as threads:
                            pending = [threads.submit(submit_call, run_cid, line) for run_cid,line in zip((cid,second['id']),additions)]
                            try:
                                wait_for((api.root/'memory-write-ready.json').exists, 'First concurrent memory read was not reached', seconds=20)
                                def competing_write():
                                    if store:
                                        trace = api.root/'upstream.jsonl'
                                        return 'file_sync_busy' in trace.read_text(encoding='utf8')
                                    return any(any(event['type']=='tool_result' and event['payload'].get('ok')
                                                   for event in api.call('GET',f'/conversations/{run_cid}')['events'])
                                               for run_cid in (cid,second['id']))
                                wait_for(competing_write,'The other memory append did not reach the expected write boundary', seconds=36)
                            finally:
                                release.touch()
                            for future in pending:
                                events = future.result(timeout=100)
                                tool, = [event for event in events if event['type']=='tool.result']
                                assert tool['tool']=='update_memory' and tool['ok'], tool
                                assert any(event['type']=='run.completed' for event in events)
                        hold.unlink()
                        (api.root/'race-key').unlink()
                        actual = api.call('GET',api.prefix+'/file?path=0-caseDict/memory.md')['content']
                        assert set(actual.split())=={'concurrent','starting','line',*additions} and all(actual.count(line)==1 for line in additions)
                        observed = json.loads((api.root/'memory-write-ready.json').read_text(encoding='utf8'))
                        result['scenarios'][mode] = {'passed':True,'both_appends_once':True,'conversation_ids':[cid,second['id']],
                                                    'pause':observed,'distinct_tool_results':2,'awaited_lease':bool(store)}
                        save()
                        print('LIVE_EDIT_MEMORY_PASS ' + mode,flush=True)
                        continue
                    if mode in {'replace', 'reject'}:
                        prompt = (f'请仅调用一次 propose_text_replacement，把 {path} 中唯一的 {old} 替换成 {new}，'
                                  'reason写“真实文本替换验收”。已知文件含这个文本，不要读取文件或调用其他工具。'
                                  '等待审批，收到工具执行结果后只总结，不再调用工具。')
                    elif mode == 'memory':
                        prompt = (f'请仅调用一次 update_memory，action为append，content准确写入：{memory}。'
                                  '不要调用其他工具，不要额外添加内容，收到工具结果后只回复已完成。')
                    elif mode in {'compact', 'cancel'}:
                        prompt = ('请立即调用一次 update_memory，action=compact，压缩当前算例的长期记忆。'
                                  '只调用这个工具。取得工具结果后只报告状态，不再次调用。')
                    else:
                        prompt = '请逐字复制当前算例长期记忆中 MEMORY_ 开头的完整字符串，包括全部字母和数字，不要省略任何字符。不要猜测，不要调用工具。'
                    if mode == 'retention':
                        obsolete = 'REMOVED_' + uuid.uuid4().hex
                        (api.root / 'obsolete-memory-proof').write_text(obsolete)
                        old_memory = obsolete + '\n' + ('older retained entry\n' * 7000) + memory + '\n'
                        api.put('0-caseDict/memory.md', old_memory)
                    body = {'content': prompt, 'provider_profile_id': profile['id'], 'model': configuration['model']}
                    if mode in {'compact','cancel'}:
                        from file_tree_lock_acceptance import writer_available
                        from live_llm_failure_fallback_acceptance import wait_for
                        from app.services.memory import MEMORY_PATH, compact_memory

                        oldest = 'OLDEST_' + uuid.uuid4().hex
                        original = oldest + '\n' + ('有据可复查的记录\n' * 9000) + memory + '\n'
                        api.put(MEMORY_PATH, original)
                        if mode=='compact':
                            assert api.call('POST',api.prefix+'/index:rebuild',{})['ok']
                            initial_index = client.get(api.prefix+'/search',params={'q':oldest})
                            initial_index.raise_for_status()
                            assert any(row['path']==MEMORY_PATH for row in initial_index.json()['hits'])
                        ready = api.root / 'memory-write-ready.json'
                        release = api.root / 'release-memory-write'
                        hold = api.root / 'hold-memory-write'
                        ready.unlink(missing_ok=True)
                        release.unlink(missing_ok=True)
                        hold.touch()
                        with ThreadPoolExecutor(max_workers=1) as threads:
                            pending = threads.submit(stream, client, f'/conversations/{cid}/messages', body, mode + '-message')
                            try:
                                def boundary():
                                    if pending.done():
                                        raise AssertionError(('Memory action stopped before derivation', pending.result()))
                                    return ready.exists()
                                wait_for(boundary, 'Actual memory edits did not start reading', seconds=20)
                                assert writer_available(api,case['id']), 'Memory read blocked the same-case database write lock'
                                if mode=='cancel':
                                    assert api.call('POST', f'/conversations/{cid}/messages:cancel', {})['ok']
                            finally:
                                release.touch()
                            events = pending.result(timeout=100)
                        hold.unlink()
                        tool, = [event for event in events if event.get('type')=='tool.result']
                        assert tool['tool']=='update_memory' and tool['args']['action']=='compact', tool
                        assert tool['ok']==(mode=='compact')
                        if mode=='cancel':
                            assert tool['status']=='cancelled' and any(event.get('type')=='run.cancelled' for event in events)
                        else:
                            assert tool['status']=='completed' and any(event.get('type')=='run.completed' for event in events)
                        expected = compact_memory(original) if mode=='compact' else original
                        actual = api.call('GET', api.prefix+'/raw?path='+MEMORY_PATH, raw=True)
                        assert actual == expected.encode()
                        history = api.call('GET', f'/conversations/{cid}')
                        saved, = [event['payload'] for event in history['events'] if event['type']=='tool_result']
                        assert saved['status']==tool['status']
                        if mode=='compact':
                            import sqlite3

                            index_path = (api.root/'index'/f'case-{case["id"]}.sqlite').as_uri()+'?mode=ro'
                            expected_sha = hashlib.sha256(actual).hexdigest()
                            def committed_index():
                                with sqlite3.connect(index_path,uri=True,timeout=.5) as index:
                                    found=index.execute('SELECT digest FROM indexed_content WHERE path=?',(MEMORY_PATH,)).fetchone()
                                return found==(expected_sha,)
                            wait_for(committed_index,'The configured index did not receive the committed file event',seconds=20)
                            def index_readback():
                                response = client.get(api.prefix+'/search', params={'q':memory})
                                response.raise_for_status()
                                return any(row.get('path')==MEMORY_PATH for row in response.json().get('hits',[]))
                            wait_for(index_readback, 'Committed memory was not found by the public index', seconds=12)
                            stale = client.get(api.prefix+'/search',params={'q':oldest})
                            stale.raise_for_status()
                            assert not any(row['path']==MEMORY_PATH for row in stale.json()['hits'])
                        result['scenarios'][mode] = {'passed':True,'conversation_id':cid,'sha256':hashlib.sha256(actual).hexdigest(),
                            'body_bytes_after':len(actual),'database_write_available':True,'status':tool['status'],
                            'enabled_index_updated':mode=='compact',**json.loads(ready.read_text(encoding='utf8'))}
                        save()
                        print('LIVE_EDIT_MEMORY_PASS ' + mode, flush=True)
                        continue
                    if mode == 'replace':
                        # A genuine quota refusal must precede both persistence and inference.
                        (api.root / 'quota-zero').touch()
                        try:
                            denied = client.post(f'/conversations/{cid}/messages', json=body)
                        finally:
                            (api.root / 'quota-zero').unlink()
                        assert denied.status_code == 429, (denied.status_code, denied.text)
                        history = api.call('GET', f'/conversations/{cid}')
                        assert not any(e['type'] == 'message' for e in history['events'])
                        save()
                        assert not any(r.get('path') == '/v1/chat/completions' for r in result['upstream'])
                        result['message_quota_refused_before_model_and_persistence'] = True
                    events = stream(client, f'/conversations/{cid}/messages', body, mode + '-message')
                    if mode in {'replace', 'reject'}:
                        pending = api.call('GET', f'/conversations/{cid}')['pending_tools']
                        assert len(pending) == 1 and pending[0]['tool'] == 'propose_text_replacement', pending
                        proposal = pending[0]
                        assert proposal['args']['old_text'] == old and proposal['args']['new_text'] == new
                        read = client.get(api.prefix + '/file', params={'path': path}); read.raise_for_status()
                        assert read.json()['content'] == before
                        endpoint = f'/conversations/{cid}/tool-calls/{proposal["id"]}'
                        if mode == 'reject':
                            response = client.post(endpoint + ':reject'); response.raise_for_status()
                        else:
                            events = stream(client, endpoint + ':approve', {}, mode + '-approved')
                            tool = next(e for e in events if e.get('type') == 'tool.result')
                            assert tool['ok'] and tool['tool'] == 'propose_text_replacement', tool
                        read = client.get(api.prefix + '/file', params={'path': path}); read.raise_for_status()
                        assert read.json()['content'] == (before if mode == 'reject' else before.replace(old, new))
                    if mode == 'memory':
                        tool = next(e for e in events if e.get('type') == 'tool.result')
                        assert tool['ok'] and tool['tool'] == 'update_memory', tool
                        read = client.get(api.prefix + '/file', params={'path': '0-caseDict/memory.md'}); read.raise_for_status()
                        assert read.json()['content'].strip() == memory
                    if mode in {'recall', 'retention'}:
                        save()
                        seen = [r for r in result['upstream'] if r.get('kind') == 'memory_in_request']
                        assert seen and seen[-1]['exact_marker_present'], 'Exact memory marker absent from actual upstream request'
                        answer = ''.join(e.get('text', '') for e in events if e.get('type') == 'text.delta')
                        assert memory in answer, answer
                        if mode == 'retention':
                            ranges = [row for row in result['upstream'] if row['kind'] == 'prompt_memory_range']
                            assert len(ranges) == 1 and 0 < ranges[0]['body_bytes'] < 65536 < ranges[0]['source_bytes'], ranges
                            assert ranges[0]['start'] > 0 and ranges[0]['end'] == ranges[0]['source_bytes'] - 1
                            assert not seen[-1]['obsolete_marker_present'], seen[-1]
                            read = client.get(api.prefix + '/file', params={'path': '0-caseDict/memory.md'}); read.raise_for_status()
                            assert read.json()['content'] == old_memory
                    if mode != 'reject':
                        assert any(e.get('type') == 'run.completed' for e in events)
                    history = api.call('GET', f'/conversations/{cid}')
                    assert not history['pending_tools']
                    if mode not in {'recall', 'retention'}:
                        assert any(e['type'] == 'tool_result' and e['payload'].get('ok') == (mode != 'reject') for e in history['events'])
                    result['scenarios'][mode] = {'passed': True, 'conversation_id': cid, 'persisted': True}
                    save(); print('LIVE_EDIT_MEMORY_PASS ' + mode, flush=True)
                for cid in conversations:
                    response = client.delete(f'/conversations/{cid}'); response.raise_for_status()
                    assert client.get(f'/conversations/{cid}').status_code == 404
                credits = api.call('GET', '/fixture/storage-credit')
                assert (credits or store is None) and all(row['state'] == 'settled' for row in credits), credits
                result.update(conversations_deleted=True, all_storage_credit_settled=True, passed=True)
                save()
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:1400]}
        save(); print('LIVE_EDIT_MEMORY_FAILED ' + json.dumps(result['failure'], ensure_ascii=False), flush=True)
        return 1
    print('LIVE_EDIT_MEMORY_OK ' + str(api.root / 'live-result.json'), flush=True)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--database', choices=('sqlite', 'postgresql'), default='sqlite')
    parser.add_argument('--storage', choices=('fs','s3'), default='s3')
    parser.add_argument('--modes', default='replace,reject,memory,recall')
    args = parser.parse_args()
    modes = args.modes.split(',')
    if (not modes or any(m not in {'replace', 'reject', 'memory', 'recall', 'retention', 'compact', 'cancel', 'race'} for m in modes)
            or len(set(modes)) != len(modes) or ('replace' in modes and modes[0] != 'replace')
            or ('retention' in modes and modes[-1] != 'retention')):
        parser.error('Choose unique modes. If selected, replace must run first and retention last.')
    configuration = asyncio.run(source(args.source_profile_id))
    from s3testsupport import ObjectStore
    store_context = (ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed', authenticated=True)
                     if args.storage=='s3' else nullcontext())
    with store_context as store:
        code = asyncio.run(postgres(configuration, modes, store, verifier=verify)) if args.database == 'postgresql' else verify(configuration, modes, store)
    sys.exit(code)
