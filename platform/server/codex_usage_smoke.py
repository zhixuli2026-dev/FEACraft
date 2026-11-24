"""Native usage attribution, persistence and unknown states on disposable DBs."""
import argparse,asyncio,json,re,tempfile,uuid
from pathlib import Path
from sqlalchemy.engine import make_url
from testsupport import IsolatedServer,SERVER
from codex_session_smoke import wait_for
from codex_turn_api_smoke import WORKER,CONTROLLED
from workflow_migration_smoke import alembic

USAGE_HELPER = r'''
def usage(number=100, **extra):
    counts={'inputTokens':number,'cachedInputTokens':20,'cacheWriteInputTokens':3,
        'outputTokens':10,'reasoningOutputTokens':4,'totalTokens':number+10}
    value={'last':dict(counts),'total':dict(counts),'modelContextWindow':32768, 'secret':'TELEMETRY_PRIVATE'}
    value['last']['debug']='TELEMETRY_PRIVATE'
    value.update(extra)
    return value
def report(value,**extra):
    params={'threadId':thread,'turnId':active['turn'],'tokenUsage':value}
    params.update(extra)
    send({'method':'thread/tokenUsage/updated','params':params})
'''
USAGE_BEHAVIOR = r'''
        if kind.startswith('usage_') or (count>1 and mode=='usage_retry'):
            value=usage()
            if kind=='usage_valid':
                report(value);report(value)
                value['last']['inputTokens']=300;value['last']['totalTokens']=310
                value['total']['inputTokens']=400;value['total']['totalTokens']=420
                report(value);terminal()
            elif kind=='usage_foreign':
                report(value,threadId='foreign');report(value,turnId='old');terminal()
            elif kind=='usage_unbound':
                send({'method':'thread/tokenUsage/updated','params':{'tokenUsage':value}});terminal()
            elif kind=='usage_zero':
                value['last']={k:0 for k in value['last'] if k!='debug'}
                value['total']=dict(value['last']);value['modelContextWindow']=None
                report(value);terminal()
            elif kind=='usage_default':
                del value['last']['cacheWriteInputTokens'];del value['total']['cacheWriteInputTokens']
                del value['modelContextWindow'];report(value);terminal()
            elif kind in {'usage_failed','usage_capacity','usage_retry'}:
                report(value);terminal(status='failed',error={'message':'ordinary failure' if kind=='usage_failed' else 'serverOverloaded'})
            elif count>1 and mode=='usage_retry':report(usage(700));terminal()
            else:
                report(value)
                bad={'usage_bool':True,'usage_negative':-1,'usage_float':2.5,'usage_string':'42','usage_overflow':2**53}
                if kind in bad:value['last']['inputTokens']=bad[kind]
                elif kind=='usage_missing':del value['total']['outputTokens']
                elif kind=='usage_window':value['modelContextWindow']=-10
                elif kind=='usage_nondict':value=[]
                report(value);terminal()
            continue
'''
USAGE_WORKER = WORKER.replace('def terminal(',USAGE_HELPER+'\ndef terminal(',1).replace(
    "        if kind in {'command','file','permissions','input','secret','policy','network'}:prompt(kind)",
    USAGE_BEHAVIOR+"        if kind in {'command','file','permissions','input','secret','policy','network'}:prompt(kind)",1)
assert USAGE_WORKER!=WORKER
USAGE_APP = CONTROLLED + '''
@app.post('/api/v1/fixture/corrupt-usage/{identifier}')
async def corrupt_usage(identifier:str):
    from app.models import NativeCliEvent
    async with SessionLocal() as db:
        row=await db.scalar(select(NativeCliEvent).where(NativeCliEvent.turn_id==identifier,
            NativeCliEvent.type=='usage.updated').order_by(NativeCliEvent.seq.desc()).limit(1))
        payload=json.loads(row.payload_json);payload['last']['inputTokens']=-1
        row.payload_json=json.dumps(payload);await db.commit();return {'ok':True}
'''

def run_database(url,folder):
    folder.mkdir(parents=True)
    if url is not None:alembic(url,folder,'upgrade','head')
    api=IsolatedServer('codex-usage-api',app='usage_controlled:app',database_url=url)
    (api.root/'cli.py').write_text(USAGE_WORKER,encoding='utf8')
    (api.root/'usage_controlled.py').write_text(USAGE_APP,encoding='utf8')
    checks=[];receipts=[]
    with api:
        case=api.create_case('Native usage');provider=api.call('POST','/fixture/profile')
        def run(kind,phase='completed'):
            api.call('POST','/fixture/mode/'+('usage_retry' if kind=='usage_retry' else 'normal'))
            conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':provider['id']})
            session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id'],
                'provider_profile_id':provider['id'],'provider_wire_api':'responses'})
            body={'content':kind,'client_message_id':uuid.uuid4().hex}
            value=api.call('POST',f"/codex/sessions/{session['id']}/turns",body)
            value=wait_for(lambda:api.call('GET','/codex/turns/'+value['id']),lambda v:v['phase'] in {'completed','failed'},seconds=45)
            assert value['phase']==phase and value['cleanup_verified'],value
            details=api.call('GET','/fixture/turn/'+value['id']);receipts.append(details)
            return value,details,session,body,conv
        value,details,session,body,conv=run('usage_valid')
        updates=[v for v in details['events'] if v['type']=='usage.updated']
        assert len(updates)==2 and value['usage']['last']['totalTokens']==310 and value['usage']['total']['totalTokens']==420
        assert value['usage']['attempt']==1 and value['usage']['modelContextWindow']==32768
        assert 'TELEMETRY_PRIVATE' not in json.dumps(details)
        checks.append('durable_latest_snapshot_not_sum_duplicate_deduped_private_fields_excluded')
        assert api.call('POST',f"/codex/sessions/{session['id']}/turns",body)['usage']==value['usage']
        listing=api.call('GET',f"/codex/conversations/{conv['id']}/turns")['data'][0]
        assert listing['usage']==value['usage']
        stream=api.call('GET',f"/codex/turns/{value['id']}/events?after={updates[0]['seq']}",raw=True).decode()
        assert stream.count('event: usage.updated')==1 and '420' in stream
        checks.append('closed_session_get_list_idempotent_post_and_cursor_replay_preserve_usage')
        own_token=api.token;api.token=None
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other'})
        # An unauthenticated observer cannot read even finished usage receipts.
        api.call('GET','/codex/turns/'+value['id'],expect=401)
        api.token=other['token']
        api.call('GET','/codex/turns/'+value['id'],expect=404)
        api.call('GET','/codex/turns/'+value['id']+'/events',expect=404)
        api.token=own_token
        checks.append('finished_usage_requires_authentication_and_matching_owner')
        api.call('POST','/fixture/corrupt-usage/'+value['id'])
        assert api.call('GET','/codex/turns/'+value['id'])['usage']['status']=='invalid_record'
        checks.append('malformed_persisted_counts_do_not_appear_as_valid_usage')
        for kind in ('normal','usage_foreign','usage_unbound'):
            row,detail,*_=run(kind)
            assert row['usage']['status']=='not_reported'
            assert not any(v['type']=='usage.updated' for v in detail['events'])
            checks.append(kind+'_does_not_invent_usage')
        row,*_=run('usage_zero');assert row['usage']['total']['totalTokens']==0 and row['usage']['status']=='reported'
        checks.append('reported_zero_remains_distinct_from_unknown')
        row,*_=run('usage_default');assert row['usage']['last']['cacheWriteInputTokens']==0 and row['usage']['modelContextWindow'] is None
        checks.append('optional_cache_write_and_context_window_compatible')
        for kind in ('usage_bool','usage_negative','usage_float','usage_string','usage_overflow','usage_missing','usage_window','usage_nondict'):
            row,*_=run(kind)
            assert row['usage']['status']=='invalid_report' and 'total' not in row['usage'],row
            checks.append(kind+'_hides_stale_counts_without_failing_answer')
        row,*_=run('usage_failed','failed');assert row['usage']['status']=='reported'
        checks.append('failed_attempt_retains_observed_usage')
        row,detail,*_=run('usage_capacity')
        assert len(row['attempts'])==2 and row['usage']['status']=='not_reported'
        assert len([v for v in detail['events'] if v['type']=='usage.updated'])==1
        checks.append('continuation_without_report_does_not_inherit_failed_attempt_counts')
        row,detail,*_=run('usage_retry');assert row['usage']['attempt']==2 and row['usage']['total']['totalTokens']==710
        assert [v['payload']['attempt'] for v in detail['events'] if v['type']=='usage.updated']==[1,2]
        checks.append('continuation_reports_bound_to_actual_attempt_without_adding_snapshots')
    (api.root/'usage-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='codex-usage-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run_database,None,root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_cliusage_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_cliusage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            results['postgresql']=await asyncio.to_thread(run_database,url.set(database=name).render_as_string(hide_password=False),root/'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (root/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'counts':{k:v['count'] for k,v in results.items()},'evidence':str(root)}))

if __name__=='__main__':asyncio.run(main())
