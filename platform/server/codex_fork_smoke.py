"""Native fork API lifecycle against actual owned fixture workers and disposable DBs."""
import argparse,asyncio,json,os,re,socket,subprocess,sys,tempfile,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlalchemy.engine import make_url
from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from codex_session_smoke import wait_for
from codex_turn_api_smoke import WORKER,CONTROLLED
from app.services import process_identity_native as identity

FORK_WORKER=WORKER.replace("    elif method=='turn/start':",'''    elif method=='thread/fork':
        with (root/'forks.log').open('a') as stream:stream.write(json.dumps(params)+'\\n')
        if mode=='fork_hold':
            (root/'fork-held').touch()
            time.sleep(120)
        if mode=='fork_error':
            send({'id':rid,'error':{'code':-32600,'message':'Rejected fixture fork'}});continue
        parent=params['threadId'];thread='fork-'+str(os.getpid())
        result={'thread':{'id':thread,'forkedFromId':parent},'model':'fixture-model'}
        if mode=='fork_foreign':result['thread']['forkedFromId']='foreign'
    elif method=='turn/start':''')
FORK_APP=CONTROLLED+'''
from app.services import codex_forks
from app.models import NativeCliFork
original_thread_ready=sessions.records.thread_ready
@app.post('/api/v1/fixture/fork/{identifier}/source/{source_id}')
async def corrupt_fork_source(identifier:str,source_id:str):
    async with SessionLocal() as db:
        row=await db.get(NativeCliFork,identifier);row.source_turn_id=source_id
        await db.commit();return {'ok':True}
async def fork_ready(db,owner,transport,thread_id):
    if mode=='fork_commit_failure' and thread_id.startswith('fork-'):
        raise RuntimeError('FixtureForkRegistrationFailure')
    return await original_thread_ready(db,owner,transport,thread_id)
sessions.records.thread_ready=fork_ready
@app.get('/api/v1/fixture/fork/{identifier}')
async def fork_receipt(identifier:str):
    async with SessionLocal() as db:
        row=await db.get(NativeCliFork,identifier)
        source=await db.get(ConversationExecution,row.source_conversation_id)
        target=await db.get(ConversationExecution,row.conversation_id)
        return {'source_phase':source.phase,'target_phase':target.phase,'phase':row.phase}
'''

def run_database(url,folder):
    folder.mkdir(parents=True)
    if url:alembic(url,folder,'upgrade','head')
    api=IsolatedServer('codex-fork-api',app='fork_fixture:app',database_url=url)
    (api.root/'cli.py').write_text(FORK_WORKER,encoding='utf8');(api.root/'fork_fixture.py').write_text(FORK_APP,encoding='utf8')
    checks=[];receipts=[]
    with api:
        case=api.create_case('Native fork');provider=api.call('POST','/fixture/profile')['id']
        def setup():
            api.call('POST','/fixture/mode/normal')
            conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':provider})
            session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id'],
                'provider_profile_id':provider,'provider_wire_api':'responses'})
            turn=api.call('POST',f"/codex/sessions/{session['id']}/turns",{'content':'normal','client_message_id':uuid.uuid4().hex})
            value=wait_for(lambda:api.call('GET','/codex/turns/'+turn['id']),lambda v:v['phase']=='completed')
            return conv,value
        def fork(conv,turn,key=None):
            body={'source_turn_id':turn['id'],'client_request_id':key or uuid.uuid4().hex}
            return api.call('POST',f"/codex/conversations/{conv['id']}/fork",body),body
        def done(row,phase='completed'):
            value=wait_for(lambda:api.call('GET','/codex/forks/'+row['id']),lambda v:v['phase'] in {'completed','failed','recovery_required','evidence_invalid'})
            assert value['phase']==phase,value;receipts.append(value);return value
        conv,turn=setup();row,body=fork(conv,turn);branch=done(row)
        assert branch['thread_id']!=turn['thread_id'] and branch['cleanup_verified']
        params=json.loads((api.root/'forks.log').read_text().splitlines()[-1])
        assert params['threadId']==turn['thread_id'] and params['lastTurnId']==turn['native_turn_id']
        assert params['excludeTurns'] and params['deferGoalContinuation']
        detail=api.call('GET',f"/conversations/{branch['conversation_id']}")
        assert detail['forked_from_id']==conv['id'] and detail['events'][-1]['payload']['kind']=='native_fork'
        assert api.call('GET','/fixture/fork/'+row['id'])=={'source_phase':'idle','target_phase':'idle','phase':'completed'}
        checks.append('fork_uses_exact_completed_native_boundary_and_closes_scope')
        before=(api.root/'forks.log').read_text()
        with ThreadPoolExecutor(2) as pool:
            duplicates=list(pool.map(lambda _:api.call('POST',f"/codex/conversations/{conv['id']}/fork",body),range(2)))
        assert all(v['id']==row['id'] for v in duplicates) and (api.root/'forks.log').read_text()==before
        assert api.call('GET',f"/codex/conversations/{conv['id']}/forks?client_request_id={body['client_request_id']}")[0]['id']==row['id']
        checks.append('duplicate_and_response_loss_lookup_do_not_repeat_fork_rpc')
        api.call('POST',f"/codex/conversations/{conv['id']}/fork",dict(body,source_turn_id=uuid.uuid4().hex),expect=409)
        checks.append('idempotency_conflict_cannot_change_source')
        foreign_conv,foreign_turn=setup()
        api.call('POST',f"/fixture/fork/{row['id']}/source/{foreign_turn['id']}")
        assert api.call('GET','/codex/forks/'+row['id'])['phase']=='evidence_invalid'
        api.call('PATCH',f"/codex/conversations/{branch['conversation_id']}/settings",{'provider_profile_id':provider,'model':'','permission_mode':'ask'},expect=409)
        api.call('POST',f"/fixture/fork/{row['id']}/source/{turn['id']}")
        assert api.call('GET','/codex/forks/'+row['id'])['phase']=='completed'
        checks.append('tampered_source_binding_is_not_accepted_as_completed')
        current=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':branch['conversation_id'],
            'provider_profile_id':provider,'provider_wire_api':'responses'})
        continued=api.call('POST',f"/codex/sessions/{current['id']}/turns",{'content':'normal','client_message_id':uuid.uuid4().hex,'resume':True})
        continued=wait_for(lambda:api.call('GET','/codex/turns/'+continued['id']),lambda v:v['phase'] in {'completed','failed'})
        assert continued['phase']=='completed' and continued['thread_id']==branch['thread_id'],continued
        checks.append('first_branch_turn_resumes_forked_thread_without_text_reconstruction')
        api.call('DELETE',f"/conversations/{conv['id']}",expect=409)
        api.call('DELETE',f"/conversations/{branch['conversation_id']}")
        api.call('DELETE',f"/conversations/{conv['id']}")
        checks.append('completed_branch_deletes_in_fk_order_and_source_retains_history_until_then')
        conv,turn=setup();owner=api.token
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other'})
        api.token=other['token']
        try:api.call('POST',f"/codex/conversations/{conv['id']}/fork",{'source_turn_id':turn['id'],'client_request_id':uuid.uuid4().hex},expect=404)
        finally:api.token=owner
        checks.append('other_account_cannot_fork_private_context')
        for mode in ('fork_hold','fork_foreign','fork_commit_failure','fork_error'):
            conv,turn=setup();api.call('POST','/fixture/mode/'+mode)
            row,body=fork(conv,turn)
            if mode=='fork_hold':
                wait_for(lambda:(api.root/'fork-held').exists(),bool)
                api.call('PATCH',f"/codex/conversations/{conv['id']}/settings",{'provider_profile_id':provider,'model':'','permission_mode':'ask'},expect=409)
                api.call('POST',f"/conversations/{row['conversation_id']}/messages",{'content':'must not run','provider_profile_id':provider},expect=409)
                api.call('POST','/codex/forks/'+row['id']+':cancel')
                (api.root/'fork-held').unlink()
            unknown=done(row,'recovery_required')
            assert unknown['cleanup_verified'],unknown
            count=len((api.root/'forks.log').read_text().splitlines())
            assert api.call('POST',f"/codex/conversations/{conv['id']}/fork",body)['id']==row['id']
            assert len((api.root/'forks.log').read_text().splitlines())==count
            api.call('DELETE',f"/conversations/{row['conversation_id']}",expect=409)
            internal=api.call('GET','/fixture/fork/'+row['id'])
            assert internal['source_phase']=='idle' and internal['target_phase']=='recovery_required',internal
            checks.append(mode+'_retains_uncertainty_and_prevents_replay')
        conv,turn=setup();api.call('POST','/fixture/mode/fork_hold');row,body=fork(conv,turn)
        wait_for(lambda:(api.root/'fork-held').exists(),bool)
        current=api.call('GET','/codex/forks/'+row['id'])
        internal=api.call('GET','/fixture/internal/'+current['session_id'])
        second=IsolatedServer('fork-reader')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));second.port=sock.getsockname()[1]
        second.base=f'http://127.0.0.1:{second.port}';second.token=api.token
        log=(api.root/'second-api.log').open('w',encoding='utf8')
        proc=subprocess.Popen([sys.executable,'-m','uvicorn',api.app,'--app-dir',str(SERVER),'--host','127.0.0.1',
            '--port',str(second.port),'--no-access-log'],cwd=api.root,env=api.env,stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            def healthy():
                try:return second.call('GET','/api/health',api_prefix=False).get('ok')
                except OSError:return False
            wait_for(healthy,bool)
            assert second.call('GET','/codex/forks/'+row['id'])['phase']=='forking'
            second.call('POST','/codex/forks/'+row['id']+':cancel',expect=409)
            assert second.call('POST',f"/codex/conversations/{conv['id']}/fork",body)['id']==row['id']
            checks.append('second_worker_observes_fork_without_owner_takeover')
            before=(api.root/'forks.log').read_text()
            api.proc.kill();api.proc.wait(timeout=10)
            wait_for(lambda:identity.probe(internal['state']['process']['root']['host']),lambda v:v['state']=='exited')
            unknown=wait_for(lambda:second.call('GET','/codex/forks/'+row['id']),lambda v:v['phase']=='recovery_required')
            assert unknown['cleanup_verified'] is not True
            assert second.call('POST',f"/codex/conversations/{conv['id']}/fork",body)['id']==row['id']
            second.call('POST',f"/conversations/{row['conversation_id']}/messages",{'content':'do not replay','provider_profile_id':provider},expect=409)
            assert (api.root/'forks.log').read_text()==before
            checks.append('actual_owner_death_leaves_read_only_unknown_fork_without_replay')
        finally:
            proc.terminate();proc.wait(timeout=10);log.close()
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'fork-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='codex-fork-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run_database,None,root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_clifork_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_clifork_[0-9a-f]{32}',name)
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
    print(json.dumps({'passed':True,'counts':{key:v['count'] for key,v in results.items()},'evidence':str(root)}))

if __name__=='__main__':asyncio.run(main())
