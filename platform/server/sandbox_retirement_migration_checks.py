"""Disposable SQLite/PostgreSQL migration and SQL constraints; no inference."""
import argparse
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from sqlalchemy import event,select,text,update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

SERVER=Path(__file__).resolve().parent
HEAD='b5f0cd6391de'
PREVIOUS='a4e9bc5280cd'


async def verify(url,root):
    from app.models import User,Workspace,Case,NativeSession,NativeRequest,DurableEvent
    root.mkdir()
    env=dict(os.environ,VIBEFLOW_DATABASE_URL=url,VIBEFLOW_STORAGE_BACKEND='fs')
    checks=[]
    async def command(*args,success=True):
        # External DDL invalidates asyncpg's cached prepared row descriptions.
        # Every verification read uses a fresh connection after migration.
        await engine.dispose()
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-B','-m','alembic',*args],
            cwd=SERVER,env=env,capture_output=True,timeout=60)
        with (root/'migration.log').open('ab') as log:log.write(result.stdout+result.stderr)
        assert (result.returncode==0)==success,'Inspect '+str(root/'migration.log')
    engine=create_async_engine(url)
    if url.startswith('sqlite'):
        @event.listens_for(engine.sync_engine,'connect')
        def foreign_keys(connection,_):
            cursor=connection.cursor();cursor.execute('PRAGMA foreign_keys=ON');cursor.close()
    stamp=datetime.now(timezone.utc)
    sid,rid,eid=(str(uuid.uuid4()) for _ in range(3))
    common=dict(case_id=1,created_by=1,owner_id='schema-only',owner_token_sha256='a'*64,
        owner_identity_json='{}',profile_json='{}',heartbeat_at=stamp,lease_expires_at=stamp)
    try:
        await command('upgrade',PREVIOUS)
        async with engine.begin() as db:
            await db.execute(User.__table__.insert().values(id=1,email='schema-only@example.com',name='Schema',password_hash='no-login'))
            await db.execute(Workspace.__table__.insert().values(id=1,user_id=1,name='Schema'))
            await db.execute(Case.__table__.insert().values(id=1,workspace_id=1,name='Schema',s3_prefix='schema-only'))
            await db.execute(NativeSession.__table__.insert().values(**common,id=sid,launch_nonce='b'*64,shell='wsl-sandbox'))
            await db.execute(NativeRequest.__table__.insert().values(**common,id=rid,launch_nonce='c'*64,kind='terminal.export'))
            before_session=dict((await db.execute(text('SELECT * FROM native_sessions'))).mappings().one())
            before_request=dict((await db.execute(text('SELECT * FROM native_requests'))).mappings().one())
        await command('upgrade','head');await command('check')
        async with engine.connect() as db:
            for table,before in [('native_sessions',before_session),('native_requests',before_request)]:
                after=dict((await db.execute(text('SELECT * FROM '+table))).mappings().one())
                assert after.pop('retirement_event_id') is None and after==before
        checks.append('populated_previous_revision_upgrade_preserves_all_old_columns')
        await command('downgrade',PREVIOUS);await command('upgrade','head');await command('check')
        checks.append('unretired_history_roundtrip_matches_metadata')
        async with engine.begin() as db:
            await db.execute(DurableEvent.__table__.insert().values(id=eid,case_id=1,seq=1,type='schema.fixture',payload_json='{}'))
        async def rejects(model,identifier,values):
            try:
                async with engine.begin() as db:
                    await db.execute(update(model).where(model.id==identifier).values(**values))
            except IntegrityError:return
            raise AssertionError('Schema accepted invalid retirement binding')
        await rejects(NativeSession,sid,{'retirement_event_id':eid})
        await rejects(NativeSession,sid,{'retirement_event_id':eid,'phase':'closed','ended_at':stamp})
        await rejects(NativeSession,sid,{'retirement_event_id':eid,'phase':'failed','ended_at':stamp,'shell':'cmd'})
        await rejects(NativeRequest,rid,{'retirement_event_id':eid})
        await rejects(NativeRequest,rid,{'retirement_event_id':eid,'phase':'succeeded','ended_at':stamp})
        await rejects(NativeRequest,rid,{'retirement_event_id':eid,'phase':'failed','ended_at':stamp,'kind':'terminal.recover'})
        checks.append('session_shell_and_both_row_phases_and_request_kind_constrained')
        await rejects(NativeSession,sid,{'retirement_event_id':str(uuid.uuid4()),'phase':'failed','ended_at':stamp})
        await rejects(NativeRequest,rid,{'retirement_event_id':str(uuid.uuid4()),'phase':'failed','ended_at':stamp})
        checks.append('both_witness_foreign_keys_reject_missing_event')
        for model,identifier in [(NativeSession,sid),(NativeRequest,rid)]:
            async with engine.begin() as db:
                await db.execute(update(model).where(model.id==identifier).values(retirement_event_id=eid,phase='failed',ended_at=stamp))
            await command('downgrade',PREVIOUS,success=False)
            async with engine.begin() as db:
                assert await db.scalar(text('SELECT version_num FROM alembic_version'))==HEAD
                assert await db.scalar(select(model.retirement_event_id).where(model.id==identifier))==eid
                await db.execute(update(model).where(model.id==identifier).values(retirement_event_id=None))
            checks.append(model.__tablename__+'_retirement_prevents_destructive_downgrade')
        async with engine.begin() as db:
            await db.execute(update(DurableEvent).where(DurableEvent.id==eid).values(type='terminal.sandbox_retired'))
        await command('downgrade',PREVIOUS,success=False)
        async with engine.begin() as db:
            assert await db.scalar(text('SELECT version_num FROM alembic_version'))==HEAD
            assert await db.scalar(select(DurableEvent.type).where(DurableEvent.id==eid))=='terminal.sandbox_retired'
        checks.append('event_history_alone_prevents_destructive_downgrade')
        result={'passed':True,'classification':'schema_protocol_only','llm_calls':0,'mock_llm_calls':0,'checks':checks}
        (root/'checks.json').write_text(json.dumps(result,indent=2))
        return result
    finally:await engine.dispose()


async def main():
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='sandbox-retirement-migration-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await verify('sqlite+aiosqlite:///'+(root/'schema.sqlite').as_posix(),root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_retirement_'+uuid.uuid4().hex
        assert re.fullmatch('tmp_vf_retirement_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,
            user=configured.username,password=configured.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            results['postgresql']=await verify(configured.set(database=name).render_as_string(hide_password=False),root/'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
    (root/'checks.json').write_text(json.dumps(results,indent=2))
    print('RETIREMENT_MIGRATION_VERIFIED '+str(root),flush=True)


if __name__=='__main__':asyncio.run(main())
