"""Real native session shutdowns keep SQL controls and leases live during staging."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from testsupport import SERVER
import terminal_staging_smoke as terminal
import viz_staging_smoke as viz
import agent_shell_staging_smoke as agent
import codex_session_staging_smoke as cli


async def run(report,boundary,kinds,url=None):
    database='postgresql' if url else 'sqlite'
    checks=[]
    for kind in kinds:
        if kind=='cli':
            result=await asyncio.to_thread(cli.run_database,url,report/(database+'-cli'),True,boundary)
        else:
            result=await {'terminal':terminal,'viz':viz,'agent':agent}[kind].verify(url,['sync_hold'],boundary)
        assert result['passed'] and result['count']==1
        observation=json.loads((Path(result['evidence'])/'staging-liveness.json').read_text())
        checks.append({'kind':kind,'database':database,'boundary':boundary,'result':result,'observation':observation})
        (report/(database+'.json')).write_text(json.dumps(checks,indent=2),encoding='utf8')
        print('PASS '+database+' '+kind+' '+boundary,flush=True)
    return checks


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--boundary',choices=['collect','prepare','apply'],default='apply')
    parser.add_argument('--kinds',nargs='+',choices=['terminal','viz','agent','cli'],default=['terminal','viz','agent','cli'])
    parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='interactive-staging-evidence-',dir=SERVER.parents[1]/'tmp'))
    checks=await run(report,args.boundary,args.kinds)
    removed=False
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_interstage_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            checks+=await run(report,args.boundary,args.kinds,temporary)
        finally:
            if created:
                await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',name)
                await admin.execute('DROP DATABASE "'+name+'"');removed=True
            await admin.close()
    result={'passed':True,'count':len(checks),'checks':checks,'temporary_database_removed':removed,
            'controlled_S3_facade':True,'real_native_processes':True,'lease_seconds':10,'pause_seconds':12}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'evidence':str(report)}),flush=True)


if __name__=='__main__':asyncio.run(main())
