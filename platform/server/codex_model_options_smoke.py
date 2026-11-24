"""Native turn settings, idempotency and paged catalog through disposable APIs."""
import argparse,asyncio,json,re,tempfile,uuid
from pathlib import Path
from sqlalchemy.engine import make_url
from testsupport import IsolatedServer,SERVER
from codex_session_smoke import wait_for
from codex_turn_api_smoke import WORKER,CONTROLLED
from workflow_migration_smoke import alembic

CATALOG = r'''
    elif method=='windowsSandbox/readiness':result={'status':'notConfigured'}
    elif method=='model/list':
        with (root/'model-list.log').open('a') as stream:stream.write(json.dumps(params)+'\n')
        second=params.get('cursor')=='next-page'
        item={'id':'fixture-model' if second else 'other-model','model':'fixture-model' if second else 'other-model',
            'displayName':'Fixture model','defaultReasoningEffort':'medium','defaultServiceTier':'default',
            'supportedReasoningEfforts':[{'reasoningEffort':'high','description':'More reasoning','secret':'PRIVATE_CATALOG'},
                {'reasoningEffort':'custom-effort-v2','description':'Custom fixture choice'}],
            'serviceTiers':[{'id':'default','name':'Standard','description':'Fixture standard','secret':'PRIVATE_CATALOG'}],
            'secret':'PRIVATE_CATALOG'}
        if mode=='bad_models':item['serviceTiers'][0]['id']={'secret':'PRIVATE_CATALOG'}
        result={'data':[item],'nextCursor':None if second else 'next-page'}
'''
MODEL_WORKER = WORKER.replace("'client':params['clientUserMessageId']", "'client':params['clientUserMessageId'],'options':{key:params[key] for key in ('effort','summary','serviceTierForTurn','collaborationMode') if key in params}").replace(
    "    elif method=='model/list':result={'data':[],'nextCursor':None}",CATALOG,1)
assert MODEL_WORKER!=WORKER
MODEL_APP=CONTROLLED

def run_database(url,folder):
    folder.mkdir(parents=True)
    if url is not None:alembic(url,folder,'upgrade','head')
    api=IsolatedServer('codex-model-api',app='model_controlled:app',database_url=url)
    (api.root/'cli.py').write_text(MODEL_WORKER,encoding='utf8')
    (api.root/'model_controlled.py').write_text(MODEL_APP,encoding='utf8')
    checks=[];receipts=[]
    with api:
        case=api.create_case('Native model options');profile=api.call('POST','/fixture/profile')
        def setup(permission='approve_writes',mode='normal',conv=None):
            api.call('POST','/fixture/mode/'+mode)
            conv=conv or api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':profile['id'],
                'model':'fixture-model','permission_mode':permission})
            session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id'],
                'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
            return session,conv
        def submit(session,body):
            value=api.call('POST',f"/codex/sessions/{session['id']}/turns",body)
            value=wait_for(lambda:api.call('GET','/codex/turns/'+value['id']),lambda v:v['phase'] in {'completed','failed'},seconds=45)
            assert value['phase']=='completed' and value['cleanup_verified'],value
            receipts.append(api.call('GET','/fixture/turn/'+value['id']));return value
        def requests():
            p=api.root/'requests.log'
            return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
        session,conv=setup()
        first=api.call('GET',f"/codex/sessions/{session['id']}/models?limit=1")
        second=api.call('GET',f"/codex/sessions/{session['id']}/models?limit=1&cursor={first['nextCursor']}")
        assert first['data'][0]['id']=='other-model' and second['data'][0]['id']=='fixture-model' and second['nextCursor'] is None
        assert 'PRIVATE_CATALOG' not in json.dumps([first,second])
        assert second['data'][0]['serviceTiers'][0]['id']=='default'
        queries=[json.loads(line) for line in (api.root/'model-list.log').read_text().splitlines()]
        assert queries==[{'cursor':None,'limit':1},{'cursor':'next-page','limit':1}]
        checks.append('paged_cli_catalog_forwards_cursor_limit_and_projects_capability_fields')
        api.call('GET',f"/codex/sessions/{session['id']}/models?limit=101",expect=422)
        checks.append('catalog_limit_rejected_before_native_rpc')
        options={'effort':'high','summary':'detailed','service_tier':'default'}
        body={'content':'normal','client_message_id':uuid.uuid4().hex,'model_options':options}
        value=submit(session,body)
        assert requests()[-1]['options']=={'effort':'high','summary':'detailed','serviceTierForTurn':'default'}
        assert receipts[-1]['events'][0]['payload']['model_options']==options
        checks.append('explicit_native_settings_reach_rpc_and_committed_request_receipt')
        count=len(requests())
        assert api.call('POST',f"/codex/sessions/{session['id']}/turns",body)['id']==value['id']
        for key,replacement in [('effort','low'),('summary','none'),('service_tier','custom-tier')]:
            api.call('POST',f"/codex/sessions/{session['id']}/turns",{**body,'model_options':{**options,key:replacement}},expect=409)
        assert len(requests())==count
        checks.append('identical_request_deduped_and_each_changed_option_conflicts')
        session,_=setup(permission='plan')
        submit(session,{**body,'client_message_id':uuid.uuid4().hex})
        assert requests()[-1]['options']['collaborationMode']['settings']['reasoning_effort']=='high'
        checks.append('plan_collaboration_preserves_requested_reasoning_effort')
        session,_=setup();before=len(requests())
        submit(session,{**body,'content':'capacity','client_message_id':uuid.uuid4().hex})
        attempt=requests()[before:];assert len(attempt)==2 and attempt[0]['options']==attempt[1]['options']
        checks.append('capacity_continuation_preserves_all_requested_options')
        session,_=setup();default_body={'content':'normal','client_message_id':uuid.uuid4().hex}
        default=submit(session,default_body);assert requests()[-1]['options']=={}
        for empty in ({},{'effort':None,'summary':None,'service_tier':None}):
            assert api.call('POST',f"/codex/sessions/{session['id']}/turns",{**default_body,'model_options':empty})['id']==default['id']
        checks.append('empty_options_preserve_existing_protocol_and_idempotency')
        session,_=setup();before=len(requests())
        invalid=[{'effort':''},{'effort':'x'*65},{'effort':True},{'effort':'high\nignore'},
            {'summary':'raw'},{'service_tier':'a b'},{'model':'unbound-model'},[]]
        for settings in invalid:
            api.call('POST',f"/codex/sessions/{session['id']}/turns",{**default_body,'client_message_id':uuid.uuid4().hex,'model_options':settings},expect=422)
        assert len(requests())==before
        api.call('DELETE','/codex/sessions/'+session['id'])
        checks.append('eight_invalid_settings_rejected_before_turn_admission')
        session,_=setup();before=len(requests())
        api.call('POST',f"/codex/sessions/{session['id']}/turns",{**default_body,'client_message_id':uuid.uuid4().hex,
            'model_options':{'effort':'fixture-native-api-credential'}},expect=400)
        assert len(requests())==before
        api.call('DELETE','/codex/sessions/'+session['id'])
        checks.append('credential_like_setting_rejected_before_public_receipt')
        session,_=setup()
        submit(session,{**default_body,'client_message_id':uuid.uuid4().hex,'model_options':{'effort':'custom-effort-v2'}})
        assert requests()[-1]['options']=={'effort':'custom-effort-v2'}
        checks.append('custom_catalog_effort_is_not_limited_to_stale_fixed_enum')
        session,_=setup(mode='bad_models')
        api.call('GET','/codex/sessions/'+session['id']+'/models',expect=502)
        api.call('DELETE','/codex/sessions/'+session['id'])
        checks.append('malformed_catalog_capabilities_not_exposed_to_ui')
    (api.root/'model-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8');return result

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='codex-model-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run_database,None,root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_climodel_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_climodel_[0-9a-f]{32}',name)
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
