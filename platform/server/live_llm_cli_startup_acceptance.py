"""Actual CLI startup plus a real model reading its observed initialization evidence."""
import argparse,asyncio,hashlib,json,re,tempfile,time,traceback,uuid
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
from cli_startup_deadline_checks import FIXTURE,verify as startup


def verify(configuration,*,database_url=None,durable_only=False,store=None,scenario=startup,fixture=FIXTURE,environment_overrides=None):
    api=IsolatedServer('live-cli-startup',app='live_fixture:app',database_url=database_url)
    settings=dict(store.settings()) if store is not None else {}
    if environment_overrides is not None:
        if (not isinstance(environment_overrides,dict) or set(environment_overrides)-{
                'VIBEFLOW_DISTRIBUTED_JOBS','VIBEFLOW_REDIS_HOST','VIBEFLOW_REDIS_PORT','VIBEFLOW_REDIS_PASSWORD'}
                or any(not isinstance(v,str) for v in environment_overrides.values())):
            raise ValueError('Only explicit isolated Redis runtime settings are accepted')
        settings.update(environment_overrides)
    prefix='import os\nos.environ.update('+repr(settings)+')\n' if settings else ''
    (api.root/'live_fixture.py').write_text(prefix+LIVE_FIXTURE+fixture,encoding='utf8')
    report={'passed':False,'simulated_llm':False,'mock_llm_calls':0,'provider':'deepseek',
        'host':'api.deepseek.com','model':configuration['model'],'cli_model_inference_verified':False}
    print('LIVE_CLI_STARTUP_EVIDENCE '+str(api.root),flush=True)
    def save():
        trace=api.root/'upstream.jsonl'
        report['upstream']=[json.loads(v) for v in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions') for v in report['upstream'])
        encoded=json.dumps(report,ensure_ascii=False,indent=2);assert configuration['key'] not in encoded
        (api.root/'live-result.json').write_text(encoded,encoding='utf8')
    try:
        with api:
            api.create_case('Actual installed CLI startup deadlines')
            evidence=scenario(api,durable_only=durable_only)
            proof='CLI_STARTUP_'+hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()
            # A professional job can become visible while another admitted job
            # still holds the case source-validation fence. Wait for that real
            # writer exclusion; never remove/revoke the fence to seed evidence.
            deadline=time.monotonic()+30
            busy=storage_busy=0
            while True:
                saved=api.call('PUT',api.prefix+'/file',{'path':'Ref/cli-startup-proof.txt','content':proof+'\n'},expect=(200,409))
                detail=saved.get('detail')
                if not detail:break
                assert isinstance(detail,dict) and detail.get('code') in {'validation_busy','storage_quota_busy'},saved
                if time.monotonic()>=deadline:raise TimeoutError('Actual case source-validation fence did not drain')
                if detail['code']=='validation_busy':busy+=1
                else:storage_busy+=1
                time.sleep(.1)
            report['readback_seed_validation_busy_retries']=busy
            report['readback_seed_storage_busy_retries']=storage_busy
            profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual startup evidence readback',
                'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
            conv=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual CLI evidence',
                'permission_mode':'plan','provider_profile_id':profile['id'],'model':configuration['model']})
            events=[]
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
                with client.stream('POST',f'/conversations/{conv["id"]}/messages',json={
                    'content':'请仅调用一次 read_text_file 读取 Ref/cli-startup-proof.txt，完整原样回复其中的 CLI_STARTUP_ 字符串；不要添加前言或其他文字，不修改文件。',
                    'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith('data:'):events.append(json.loads(line[5:]))
            raw=json.dumps(events,ensure_ascii=False,indent=2);assert configuration['key'] not in raw
            (api.root/'model-events.json').write_text(raw,encoding='utf8')
            tools=[v for v in events if v.get('type')=='tool.result']
            report['answer']=''.join(v.get('text','') for v in events if v.get('type')=='text.delta')
            report['strict_readback']=report['answer']==proof;save()
            assert len(tools)==1 and tools[0]['tool']=='read_text_file' and tools[0]['ok'] and proof in tools[0]['result']
            assert report['strict_readback'] and any(v.get('type')=='run.completed' for v in events)
            assert not any(v.get('type') in {'stream.error','request.failed','request.timed_out'} for v in events)
            history=api.call('GET',f'/conversations/{conv["id"]}')
            assert ''.join(v['payload'].get('content') or '' for v in history['events']
                if v['type']=='message' and v['payload'].get('role')=='assistant')==proof
            report['passed']=True;save()
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1500],
            'frames':[{'file':frame.filename,'line':frame.lineno,'function':frame.name} for frame in traceback.extract_tb(error.__traceback__)[-8:]]};save()
        raise RuntimeError('Actual CLI startup acceptance failed; inspect '+str(api.root/'live-result.json')) from None
    finally:
        report['api_exited']=api.proc is not None and api.proc.poll() is not None;save()
    print('LIVE_CLI_STARTUP_OK '+str(api.root),flush=True)
    return api.root


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true');parser.add_argument('--durable-only',action='store_true');args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    if not args.postgres:
        await asyncio.to_thread(verify,configuration,durable_only=args.durable_only);return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_clistart_'+uuid.uuid4().hex
    assert re.fullmatch('tmp_vf_clistart_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='cli-startup-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False,'mock_llm_calls':0}
    print('CLI_STARTUP_POSTGRES '+str(root),flush=True)
    admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,user=configured.username,
        password=configured.password,database='postgres',timeout=10)
    created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=configured.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        report['root']=str(await asyncio.to_thread(verify,configuration,database_url=url,durable_only=args.durable_only))
        report['passed']=True
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await admin.close();(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
if __name__=='__main__':asyncio.run(main())
