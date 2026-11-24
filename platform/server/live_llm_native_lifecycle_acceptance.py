"""Explicit real-model entry for native owner lifecycle and Shell compatibility."""
import argparse,asyncio,json,re,tempfile,uuid
from pathlib import Path
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
from native_request_lifecycle_checks import FIXTURE,verify
from testsupport import IsolatedServer,SERVER


def checks(*,reproduce=False,database_url=None):
    api=IsolatedServer('native-owner-lifecycle',app='live_fixture:app',database_url=database_url)
    (api.root/'live_fixture.py').write_text(LIVE_FIXTURE+FIXTURE,encoding='utf8')
    print('NATIVE_LIFECYCLE_EVIDENCE '+str(api.root),flush=True)
    with api:
        api.create_case('Actual native owner lifecycle')
        verify(api,reproduce=reproduce)
    return api.root


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--reproduce',action='store_true');parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    if args.reproduce and args.postgres:parser.error('Reproduction uses the disposable SQLite fixture')
    from live_llm_native_shell_deadline_acceptance import verify as shell
    if not args.postgres:
        await asyncio.to_thread(checks,reproduce=args.reproduce)
        if not args.reproduce:
            configuration=await source(args.source_profile_id)
            await asyncio.to_thread(shell,configuration)
        return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_lifecycle_'+uuid.uuid4().hex
    assert re.fullmatch('tmp_vf_lifecycle_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='native-lifecycle-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False,'mock_llm_calls':0}
    print('NATIVE_LIFECYCLE_POSTGRES '+str(root),flush=True)
    admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,user=configured.username,
        password=configured.password,database='postgres',timeout=10)
    created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=configured.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        report['checks_root']=str(await asyncio.to_thread(checks,database_url=url))
        configuration=await source(args.source_profile_id)
        report['model_root']=str(await asyncio.to_thread(shell,configuration,database_url=url))
        report['passed']=True
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await admin.close()
            (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
