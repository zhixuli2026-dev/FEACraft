"""Actual SeaweedFS/CLI preparation and actual DeepSeek evidence readback."""
import argparse,asyncio,json,re,tempfile,uuid
from pathlib import Path
from testsupport import SERVER
from s3testsupport import ObjectStore
from live_llm_acceptance import source
from live_llm_cli_startup_acceptance import verify
from cli_staging_deadline_checks import FIXTURE,verify as scenario


def run(configuration,database_url=None):
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed')
    report={'passed':False,'mock_llm_calls':0}
    try:
        with store:
            report['root']=str(verify(configuration,database_url=database_url,store=store,scenario=scenario,fixture=FIXTURE))
            report['passed']=True
    finally:
        report['store_stopped']=store.proc is not None and store.proc.poll() is not None
        (store.root/'cli-staging-run.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return store.root


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    if not args.postgres:
        await asyncio.to_thread(run,configuration);return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_clistage_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_clistage_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='cli-staging-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False}
    print('CLI_STAGING_POSTGRES '+str(root),flush=True)
    admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,user=configured.username,
        password=configured.password,database='postgres',timeout=10)
    created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=configured.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        report['store_root']=str(await asyncio.to_thread(run,configuration,url));report['passed']=True
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await admin.close();(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
