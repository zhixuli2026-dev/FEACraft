"""Real competing cleanup APIs on a disposable local PostgreSQL database."""
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
from sandbox_image_concurrency_checks import FIXTURE, after_retirement
from testsupport import SERVER
from workflow_migration_smoke import alembic


async def run(*, fixture_prefix='', continuation=None, execution_fixture=FIXTURE, scenario=after_retirement,before_recovery=None):
    configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_image_'+uuid.uuid4().hex
    assert re.fullmatch('tmp_vf_image_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='image-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False,'llm_calls':0,'mock_llm_calls':0,
        'scenario':scenario.__module__}
    admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,user=configured.username,
        password=configured.password,database='postgres',timeout=10)
    created=False
    async def acceptance(api,sid,rid,proof):
        report['fixture_root']=str(api.root)
        report['llm_calls']=await scenario(api,sid,rid,proof,continuation=continuation)
        return report['llm_calls']
    async def before(api,sid,proof):
        report['fixture_root']=str(api.root)
        if before_recovery is not None:await before_recovery(api,sid,proof)
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=configured.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        await verify(fixture_prefix=fixture_prefix+execution_fixture,after_retirement=acceptance,database_url=url,
                     before_recovery=before if before_recovery is not None else None)
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
                if trace.exists():
                    report['llm_calls']=sum(row['kind']=='http' and row['path'].endswith('/chat/completions')
                        for row in map(json.loads,trace.read_text().splitlines()))
            (root/'result.json').write_text(json.dumps(report,indent=2))
            print('IMAGE_POSTGRES_EVIDENCE '+str(root),flush=True)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    asyncio.run(run())
