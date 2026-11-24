"""Real partial node publication recovery with actual DeepSeek Agent readback."""
import argparse,asyncio,json,re,tempfile,uuid
from pathlib import Path
from testsupport import SERVER
from live_llm_acceptance import source
from live_llm_cli_startup_acceptance import verify
from cli_node_fragment_checks import FIXTURE,verify as scenario


def run(configuration,*,database_url=None,s3=False):
    options=dict(database_url=database_url,scenario=scenario,fixture=FIXTURE)
    if not s3:return verify(configuration,**options)
    from s3testsupport import ObjectStore
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed');report={'passed':False}
    try:
        with store:
            result=verify(configuration,store=store,**options);report.update(passed=True,root=str(result))
    finally:
        report['store_stopped']=store.proc is not None and store.proc.poll() is not None
        (store.root/'cli-node-fragment-run.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    if not args.postgres:await asyncio.to_thread(run,configuration,s3=args.s3);return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername=='postgresql+asyncpg' and selected.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_clinodefragment_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_clinodefragment_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='cli-node-fragment-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False}
    print('CLI_NODE_FRAGMENT_POSTGRES '+str(root),flush=True)
    admin=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database='postgres',timeout=10);created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=selected.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        report['root']=str(await asyncio.to_thread(run,configuration,database_url=url,s3=args.s3));report['passed']=True
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await admin.close();(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
