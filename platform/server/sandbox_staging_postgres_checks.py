"""Real S3/WSL and competing API processes on one disposable PostgreSQL DB."""
import asyncio
import json
from pathlib import Path
import re
import sys
import tempfile
import uuid

import asyncpg
from sqlalchemy.engine import make_url
from app.config import Settings
from sandbox_retirement_checks import verify
from sandbox_staging_concurrency_checks import FIXTURE, after_retirement
from s3testsupport import ObjectStore
from testsupport import SERVER
from workflow_migration_smoke import alembic


async def run(*,fixture_prefix='',execution_fixture=FIXTURE,scenario=after_retirement,continuation=None):
    configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_staging_'+uuid.uuid4().hex
    assert re.fullmatch('tmp_vf_staging_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='staging-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False,'llm_calls':0,'mock_llm_calls':0}
    admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,user=configured.username,
        password=configured.password,database='postgres',timeout=10)
    created=False
    async def acceptance(api,sid,rid,proof):
        report['fixture_root']=str(api.root)
        if continuation is None:return await scenario(api,sid,rid,proof)
        return await scenario(api,sid,rid,proof,continuation=continuation)
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=configured.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
            report['store_root']=str(store.root)
            await verify(store,fixture_prefix=fixture_prefix+execution_fixture,after_retirement=acceptance,database_url=url)
        report['object_store_stopped']=store.proc.poll() is not None
        report['passed']=True
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
                report['temporary_database_dropped']=True
        finally:
            await admin.close()
            if report.get('fixture_root'):
                trace=Path(report['fixture_root'])/'upstream.jsonl'
                if trace.exists():report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions')
                    for v in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
            (root/'result.json').write_text(json.dumps(report,indent=2))
            print('STAGING_POSTGRES_EVIDENCE '+str(root),flush=True)


async def main():await run()


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    asyncio.run(main())
