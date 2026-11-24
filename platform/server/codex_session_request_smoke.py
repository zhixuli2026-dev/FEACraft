"""Conversation startup admission, response loss and fencing on disposable APIs."""
import argparse,asyncio,json,re,socket,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlalchemy.engine import make_url
from testsupport import IsolatedServer,SERVER
from codex_session_smoke import wait_for
from codex_turn_api_smoke import WORKER,CONTROLLED
from workflow_migration_smoke import alembic

START_WORKER=WORKER.replace("    if method=='initialize':result={'userAgent':'fixture/turns'}",r'''
    if method=='initialize':
        with (root/'initializations.log').open('a') as stream:stream.write(str(os.getpid())+'\n')
        if mode=='init_wait':
            (root/('init-held-'+str(os.getpid()))).touch()
            while not (root/'release-init').exists():time.sleep(.02)
        if mode=='bad_init':result={}
        else:result={'userAgent':'fixture/turns'}
''',1)
assert START_WORKER!=WORKER
START_APP=CONTROLLED+r'''
from app.models import NativeCliSessionRequest
from app.services import codex_session_requests as startups
from app.services import supervisor
original_prepare=sessions.Session.prepare
async def startup_prepare(self):
    if mode=='prepare_failure':raise OSError('isolated before prepare')
    if mode in {'prepare_hold','revoke_before_prepare'}:
        (root/('prepare-held-'+self.id)).touch()
        while not (root/('release-prepare-'+self.id)).exists():await asyncio.sleep(.02)
    return await original_prepare(self)
sessions.Session.prepare=startup_prepare
original_submit=startups.submit
async def startup_submit(*args,**kwargs):
    value=await original_submit(*args,**kwargs)
    if mode=='response_hold_start':
        (root/'startup-response-held.json').write_text(json.dumps({'id':value['id']}))
        while not (root/'release-startup-response').exists():await asyncio.sleep(.02)
    return value
startups.submit=startup_submit
original_commit=AsyncSession.commit
acknowledged=set()
async def startup_commit(db):
    candidates=[row for row in db.identity_map.values() if isinstance(row,NativeCliSessionRequest)]
    fault=next((row for row in candidates if row.id not in acknowledged and
        (mode=='ready_ack' and row.phase=='completed' or mode=='admission_ack' and row.phase=='admitted')),None)
    value=await original_commit(db)
    if fault is not None:
        acknowledged.add(fault.id)
        if mode=='ready_ack':
            (root/'ready-ack-held.json').write_text(json.dumps({'id':fault.id}))
            while not (root/'release-ready-ack').exists():await asyncio.sleep(.02)
        raise OSError('isolated committed startup acknowledgement lost')
    return value
AsyncSession.commit=startup_commit
@app.get('/api/v1/fixture/startup/{identifier}')
async def startup_detail(identifier:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeCliSessionRequest,identifier)
        control=await db.get(ConversationExecution,row.conversation_id)
        return {'request':await startups.public(db,row),'raw_phase':row.phase,'not_started':row.not_started,
            'control':control.phase,'actor':identifier in startups.RUNS,
            'children':await db.scalar(select(func.count()).select_from(NativeCliSession).where(NativeCliSession.id==identifier)),
            'supervised':[v['kind'] for v in supervisor.active(row.case_id)]}
@app.post('/api/v1/fixture/startup/{identifier}/{action}')
async def startup_fault(identifier:str,action:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeCliSessionRequest,identifier)
        control=await db.get(ConversationExecution,row.conversation_id)
        if action=='expire':control.lease_expires_at=now()-timedelta(seconds=2)
        elif action=='revoke':
            selected=json.loads(row.selection_json)
            profile=await db.get(ProviderProfile,selected['credential']['profile_id'])
            profile.key_ciphertext=llm.encrypt_key('replacement-fixture-key')
        elif action=='corrupt':row.selection_json='{}'
        elif action=='restore':
            owner=startups.RUNS[identifier].owner
            control.lease_expires_at=now()+timedelta(seconds=30)
        await db.commit();return {'ok':True}
@app.post('/api/v1/fixture/startup-timeout/{seconds}')
async def startup_timeout(seconds:float,user=Depends(get_current_user)):
    startups.STARTUP_SECONDS=seconds;return {'ok':True}
@app.post('/api/v1/fixture/close-startups')
async def close_startups(user=Depends(get_current_user)):
    await startups.close_all();return {'ok':True}
'''


def run_database(url,folder):
    folder.mkdir(parents=True)
    if url is not None:alembic(url,folder,'upgrade','head')
    api=IsolatedServer('codex-startup-api',app='startup_controlled:app',database_url=url)
    (api.root/'cli.py').write_text(START_WORKER,encoding='utf8')
    (api.root/'startup_controlled.py').write_text(START_APP,encoding='utf8')
    checks=[];receipts=[]
    with api:
        case=api.create_case('Native session startup');profile=api.call('POST','/fixture/profile')
        def setup(mode='normal',profile_id=None):
            api.call('POST','/fixture/mode/'+mode)
            pid=profile_id or profile['id']
            conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':pid,
                'model':'fixture-model','permission_mode':'approve_writes'})
            body={'client_request_id':uuid.uuid4().hex,'provider_profile_id':pid,'provider_wire_api':'responses',
                'runtime':'windows','purpose':'chat'}
            return '/codex/conversations/'+str(conv['id']),body,conv
        def submit(prefix,body):return api.call('POST',prefix+'/session-requests',body)
        def read(value):return api.call('GET','/codex/session-requests/'+value['id'])
        def done(value,phase='completed'):
            value=wait_for(lambda:read(value),lambda v:v['phase'] not in {'admitted','starting'},seconds=45)
            assert value['phase']==phase,value
            wait_for(lambda:api.call('GET','/fixture/startup/'+value['id']),lambda v:not v['actor'])
            receipts.append(value);return value
        def close(value):
            value=api.call('DELETE','/codex/sessions/'+value['session']['id'])
            assert value['cleanup_verified'],value
        def launches():return len(list(api.root.glob('cli-*.json')))
        def lookup(prefix,body):return api.call('GET',prefix+'/session-requests?client_request_id='+body['client_request_id'])
        prefix,body,conv=setup();before=launches()
        with ThreadPoolExecutor(max_workers=3) as pool:
            values=list(pool.map(lambda _:submit(prefix,body),range(3)))
        assert len({v['id'] for v in values})==1
        ready=done(values[0]);assert launches()==before+1
        assert ready['session']['phase']=='active' and ready['session']['thread_id'] is None
        assert lookup(prefix,body)[0]['id']==ready['id']
        assert api.call('GET',prefix+'/activity')['phase']=='idle'
        checks.append('concurrent_identical_startup_keys_launch_exactly_one_cli_without_a_turn')
        for patch in ({'purpose':'inspect'},{'runtime':'wsl'},{'provider_wire_api':None}):
            api.call('POST',prefix+'/session-requests',{**body,**patch},expect=409)
        assert launches()==before+1
        checks.append('same_startup_key_with_changed_selection_conflicts_before_launch')
        close(ready);assert submit(prefix,body)['session']['phase']=='closed' and launches()==before+1
        checks.append('completed_startup_remains_historical_after_verified_close')
        api.call('DELETE','/conversations/'+str(conv['id']))
        checks.append('verified_startup_history_deletes_with_conversation_in_fk_order')
        prefix,body,conv=setup()
        for patch in ({'client_request_id':''},{'client_request_id':'x'*129},{'client_request_id':'x\0y'},
                      {'runtime':'linux'},{'purpose':'arbitrary'},{'case_id':case['id']}):
            api.call('POST',prefix+'/session-requests',{**body,**patch},expect=422)
        api.call('GET',prefix+'/session-requests?client_request_id='+('x'*129),expect=422)
        assert lookup(prefix,body)==[]
        checks.append('invalid_requests_rejected_without_admission')
        prefix,body,conv=setup('init_wait');value=submit(prefix,body)
        wait_for(lambda:list(api.root.glob('init-held-*')),bool)
        count=launches()
        for _ in range(3):
            assert read(value)['phase']=='starting';assert lookup(prefix,body)[0]['id']==value['id']
        activity=api.call('GET',prefix+'/activity')
        assert activity['active_session_request']['id']==value['id'] and activity['owned_by_current_user']
        api.call('PATCH',prefix+'/settings',{'provider_profile_id':profile['id'],'model':'changed','permission_mode':'ask'},expect=409)
        api.call('POST',prefix+'/session-requests',{**body,'client_request_id':uuid.uuid4().hex},expect=409)
        api.call('DELETE','/conversations/'+str(conv['id']),expect=409)
        assert launches()==count
        checks.append('read_only_discovery_and_conversation_fence_preserve_starting_owner')
        cancelled=api.call('POST','/codex/session-requests/'+value['id']+':cancel')
        assert cancelled['phase']=='cancelled' and cancelled['cleanup_verified'],cancelled
        assert api.call('GET',prefix+'/activity')['phase']=='idle'
        checks.append('cancel_during_initialize_verifies_owned_scope_before_unlock')
        prefix,body,conv=setup('prepare_failure');count=launches();value=done(submit(prefix,body),'failed')
        assert value['session'] is None and value['cleanup_verified'] and launches()==count
        assert api.call('GET','/fixture/startup/'+value['id'])['not_started']
        checks.append('preparation_failure_records_proven_not_started_without_child')
        prefix,body,conv=setup('bad_init');value=done(submit(prefix,body),'failed')
        assert value['cleanup_verified'] and value['session']['phase']=='failed'
        checks.append('initialization_failure_retains_child_and_verified_cleanup')
        prefix,body,conv=setup('init_wait');api.call('POST','/fixture/startup-timeout/1')
        value=done(submit(prefix,body),'failed');assert value['cleanup_verified']
        api.call('POST','/fixture/startup-timeout/45')
        checks.append('bounded_startup_timeout_closes_child_and_unlocks')
        prefix,body,conv=setup('prepare_hold');count=launches();value=submit(prefix,body)
        wait_for(lambda:(api.root/('prepare-held-'+value['id'])).exists(),bool)
        assert 'codex_start' in api.call('GET','/fixture/startup/'+value['id'])['supervised']
        api.call('POST','/fixture/close-startups');value=done(value,'failed')
        assert value['cleanup_verified'] and value['session'] is None and launches()==count
        checks.append('shutdown_tracks_and_cancels_preparing_actor_before_session_registration')
        # Use a separate profile so revocation cannot poison later cases.
        revoked=api.call('POST','/fixture/profile')
        prefix,body,conv=setup('revoke_before_prepare',revoked['id']);count=launches();value=submit(prefix,body)
        wait_for(lambda:(api.root/('prepare-held-'+value['id'])).exists(),bool)
        api.call('POST','/fixture/startup/'+value['id']+'/revoke')
        (api.root/('release-prepare-'+value['id'])).touch();value=done(value,'failed')
        assert value['session'] is None and value['cleanup_verified'] and launches()==count
        checks.append('credential_revision_rechecked_after_admission_before_any_native_gate')
        prefix,body,conv=setup('response_hold_start');count=launches()
        payload=json.dumps(body).encode();connection=socket.create_connection(('127.0.0.1',api.port))
        connection.sendall((f'POST /api/v1{prefix}/session-requests HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {api.token}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n').encode()+payload)
        wait_for(lambda:(api.root/'startup-response-held.json').exists(),bool);connection.close()
        found=wait_for(lambda:lookup(prefix,body),bool)[0];value=done(found)
        assert launches()==count+1 and not (api.root/'requests.log').exists()
        close(value);(api.root/'release-startup-response').touch()
        checks.append('lost_startup_http_response_recovers_by_key_without_resending_or_inference')
        prefix,body,conv=setup('ready_ack');value=submit(prefix,body)
        wait_for(lambda:(api.root/'ready-ack-held.json').exists(),bool)
        observed=read(value);assert observed['phase']=='completed' and observed['session']['phase']=='active'
        turn=api.call('POST','/codex/sessions/'+value['id']+'/turns',{'content':'hold','client_message_id':uuid.uuid4().hex})
        wait_for(lambda:api.call('GET','/codex/turns/'+turn['id']),lambda v:v['phase']=='running')
        (api.root/'release-ready-ack').touch();done(value)
        assert api.call('GET','/codex/turns/'+turn['id'])['phase']=='running'
        stopped=api.call('POST','/codex/turns/'+turn['id']+':cancel');assert stopped['cleanup_verified']
        checks.append('lost_ready_commit_ack_never_closes_session_already_handed_to_a_turn')
        prefix,body,conv=setup('normal');value=done(submit(prefix,body));saved=api.token
        api.create_case('Other startup owner')
        api.call('GET','/codex/session-requests/'+value['id'],expect=404)
        api.call('POST','/codex/session-requests/'+value['id']+':cancel',expect=404)
        api.call('GET',prefix+'/session-requests',expect=404)
        api.token=saved;close(value)
        checks.append('other_user_cannot_query_cancel_or_discover_startup_history')
        prefix,body,conv=setup('admission_ack');count=launches()
        api.call('POST',prefix+'/session-requests',body,expect=502)
        value=lookup(prefix,body)[0];api.call('POST','/fixture/startup/'+value['id']+'/expire')
        assert read(value)['phase']=='recovery_required'
        assert submit(prefix,body)['id']==value['id'] and launches()==count
        api.call('POST','/codex/session-requests/'+value['id']+':cancel',expect=409)
        api.call('DELETE','/conversations/'+str(conv['id']),expect=409)
        checks.append('unknown_admission_commit_preserved_without_automatic_takeover_or_relaunch')
        assert not api.call('GET','/fixture/startup/'+value['id'])['actor']
    (api.root/'startup-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8');return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='codex-startup-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run_database,None,root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_clistart_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_clistart_[0-9a-f]{32}',name)
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
