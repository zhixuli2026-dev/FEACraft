"""Verify the persisted index before any search, with a real approved Agent write."""
import asyncio
from contextlib import closing
from functools import partial
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

import httpx

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put
from live_llm_publication_restart_acceptance import FIXTURE as PID_FIXTURE, reboot


FIXTURE = PID_FIXTURE + '''
from app.services import durable_events
deliver = durable_events.deliver_pending
async def held_delivery(*args,**kwargs):
    if (root/'hold-index-outbox').exists():
        (root/'index-outbox-held').touch()
        return 0
    return await deliver(*args,**kwargs)
durable_events.deliver_pending = held_delivery
'''


def scenario(api, *, configuration, store=None, **_):
    case = api.prefix
    root = api.root
    report_path = root/'immediate-file-index.json'
    report = {'storage':'s3' if store else 'fs','operations':{}}

    def save():
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')

    def marker():
        return 'index'+uuid.uuid4().hex

    agent_path = 'Ref/agent-index.txt'
    incumbent, untouched = marker(), marker()
    put(api,agent_path,incumbent)
    put(api,'Ref/index-untouched.txt',untouched)
    assert api.call('POST',case+'/index:rebuild',{})['ok']
    database = root/'index'/f'case-{api.case_id}.sqlite'

    def connection():
        return sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)

    def cached(term):
        with closing(connection()) as conn:
            return [p for p, in conn.execute('SELECT path FROM file_fts WHERE file_fts MATCH ? ORDER BY path',(term,))]

    def index_identity(path):
        with closing(connection()) as conn:
            return conn.execute('SELECT digest,fts_rowid FROM indexed_content WHERE path=?',(path,)).fetchone()

    untouched_identity = index_identity('Ref/index-untouched.txt')
    assert untouched_identity is not None
    flag = root/'hold-index-outbox'
    flag.touch()
    wait_for((root/'index-outbox-held').exists,'Dispatcher did not reach the gate')
    initial = api.call('GET',case+'/workflow-events?after=0&limit=200')['watermark']
    dict_path = '0-caseDict/caseDict'
    approval_path = 'Ref/approval-evidence.txt'
    upload_path = 'Ref/multipart-index.txt'
    chunk_path = 'Ref/chunked-index.txt'
    direct_path = 'Ref/direct-agent-index.txt'
    dic, uploaded, proposed, deferred, chunked, direct = (marker() for _ in range(6))

    def observe(name, term, path):
        hits = cached(term)
        report['operations'][name] = {'cached_before_search':hits,'expected':path}
        save()
        assert hits==[path], (name,hits)

    headers = {'Authorization':'Bearer '+api.token}
    with httpx.Client(base_url=api.base+'/api/v1',headers=headers,timeout=90,trust_env=False) as client:
        def upload(path, text):
            reply = client.post(case+'/upload',params={'path':path},files={'file':('index.txt',text,'text/plain')})
            assert reply.status_code==200 and reply.json()['ok'], (reply.status_code,reply.text[:300])

        def stream(path, body):
            with client.stream('POST',path,json=body) as reply:
                reply.raise_for_status()
                events = [json.loads(line[5:]) for line in reply.iter_lines() if line.startswith('data:')]
            assert not any(event['type'] in {'stream.error','request.failed','request.timed_out'} for event in events)
            return events

        current = api.call('GET',case+'/caseDict')
        updated = current['content']+'\n// '+dic+'\n'
        assert api.call('PUT',case+'/caseDict',{'content':updated})['ok']
        observe('caseDict',dic,dict_path)
        upload(upload_path,uploaded)
        observe('multipart',uploaded,upload_path)
        chunk_content = (chunked+'\n').encode()
        session = api.call('POST',case+'/uploads',{'path':chunk_path,'size':len(chunk_content),
            'sha256':hashlib.sha256(chunk_content).hexdigest()},extra_headers={'Idempotency-Key':uuid.uuid4().hex})
        upload_route = case+'/uploads/'+session['id']
        chunk = client.put(upload_route+'?offset=0',content=chunk_content)
        assert chunk.status_code==200 and chunk.json()['offset']==len(chunk_content), chunk.text
        assert cached(chunked)==[]
        api.call('POST',upload_route+':complete',{})
        observe('chunked_publication',chunked,chunk_path)

        profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual index tool write',
            'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
        model = {'provider_profile_id':profile['id'],'model':configuration['model']}
        conv = api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual index tool write',
            'permission_mode':'approve_writes',**model})
        route = '/conversations/'+str(conv['id'])
        prompt = (f'这是隔离验收。请只调用一次 propose_file_update，把文件 {agent_path} 的内容完整替换为 {proposed}，'
                  '不要添加换行，不要读取文件。等待我批准。收到工具结果后只回复已完成，不要再调用工具。')
        frames = stream(route+'/messages',{'content':prompt,**model})
        proposals = [f['tool_call'] for f in frames if f['type']=='approval.requested']
        assert len(proposals)==1 and proposals[0]['tool']=='propose_file_update'
        tool = proposals[0]
        assert tool['args']['path']==agent_path and tool['args']['content']==proposed
        assert cached(proposed)==[] and cached(incumbent)==[agent_path]
        foreign = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'password':'index-test-password-23','name':'Other index account'})
        outsider = {'Authorization':'Bearer '+foreign['token']}
        approve = route+'/tool-calls/'+tool['id']+':approve'
        api.call('POST',approve,{},expect=404,extra_headers=outsider)
        applied = stream(approve,{})
        assert any(item['type']=='tool.result' and item['ok'] and item.get('approved') for item in applied)
        assert any(item['type']=='run.completed' for item in applied)
        observe('agent_approved_write',proposed,agent_path)
        assert cached(incumbent)==[] and index_identity('Ref/index-untouched.txt')==untouched_identity
        api.call('GET',case+'/search?q='+proposed,expect=404,extra_headers=outsider)

        direct_conv = api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual automatic index tool write',
            'permission_mode':'full_access',**model})
        frames = stream('/conversations/'+str(direct_conv['id'])+'/messages',{
            'content':f'这是隔离验收。请只调用一次 propose_file_update 新建 {direct_path}，完整内容为 {direct}。'
                      '当前允许直接写入。工具结果返回后只回复已完成，不要再调用工具。',**model})
        results = [f for f in frames if f['type']=='tool.result']
        assert len(results)==1 and results[0]['tool']=='propose_file_update' and results[0]['ok']
        assert not any(f['type']=='approval.requested' for f in frames)
        args = results[0]['args']
        try:
            assert args['path']==direct_path and args['content']==direct, 'Model supplied different write arguments'
            observe('agent_direct_write',direct,direct_path)
        except AssertionError:
            raw = client.get(case+'/raw',params={'path':direct_path})
            indexed = index_identity(direct_path)
            report['direct_write_diagnostics'] = {
                'tool_path_matches':args['path']==direct_path, 'tool_content_matches':args['content']==direct,
                'file_status':raw.status_code, 'file_matches_requested_bytes':raw.content==direct.encode(),
                'index_has_path':indexed is not None,
                'index_matches_requested_digest':bool(indexed and indexed[0]==hashlib.sha256(direct.encode()).hexdigest()),
            }
            save()
            raise

        with closing(sqlite3.connect(str(database),timeout=.5)) as held:
            held.execute('BEGIN IMMEDIATE')
            try:
                upload(approval_path,deferred)
                assert cached(deferred)==[]
            finally:
                held.rollback()
        assert api.call('GET',case+'/raw?path=Ref%2Fapproval-evidence.txt',raw=True)==deferred.encode()
        report['file_retained_when_index_busy'] = True
        save()

    batch = api.call('GET',case+f'/workflow-events?after={initial}&limit=200')['events']
    held_event = next(row['event_id'] for row in batch if row['type']=='files.changed' and approval_path in row['paths'])
    report['pids'] = reboot(api)
    assert all(cached(term)==[path] for term,path in ((dic,dict_path),(uploaded,upload_path),(proposed,agent_path),
                                                    (chunked,chunk_path),(direct,direct_path)))
    assert cached(deferred)==[]
    flag.unlink()

    def receipt():
        try:
            with closing(connection()) as conn:
                return conn.execute('SELECT count(*) FROM consumed_events WHERE event_id=?',(held_event,)).fetchone()[0]==1
        except sqlite3.OperationalError:
            return False

    wait_for(receipt,'Durable index replay did not settle the busy writer',55)
    assert cached(deferred)==[approval_path]
    for term,path in ((dic,dict_path),(uploaded,upload_path),(proposed,agent_path),
                      (deferred,approval_path),(chunked,chunk_path),(direct,direct_path)):
        result = api.call('GET',case+'/search?q='+term)
        assert [hit['path'] for hit in result['hits']]==[path]
    report.update(no_websocket=True,no_search_before_index_checks=True,index_receipt_after_restart=held_event,
        unrelated_fts_row_preserved=True,foreign_write_and_search_http=404)
    save()
    return report


if __name__=='__main__':
    asyncio.run(main(selected_factory=lambda configuration:partial(scenario,configuration=configuration),
        fixture_override=FIXTURE,store_volume_max=6))
