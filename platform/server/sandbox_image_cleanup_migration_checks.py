"""Disposable schema constraints and downgrade protection; no inference."""
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

from sqlalchemy import select,text,update,delete
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from workflow_migration_smoke import engine_for

SERVER=Path(__file__).resolve().parent
HEAD='c6a1de7402ef'
PREVIOUS='b5f0cd6391de'


async def verify(url,root):
    from app.models import User,Workspace,Case,NativeRequest,DurableEvent
    root.mkdir();engine=engine_for(url);checks=[]
    async def command(*args,success=True):
        await engine.dispose()
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-B','-m','alembic',*args],cwd=SERVER,
            env=dict(os.environ,VIBEFLOW_DATABASE_URL=url,VIBEFLOW_STORAGE_BACKEND='fs'),capture_output=True,timeout=60)
        with (root/'migration.log').open('ab') as log:log.write(result.stdout+result.stderr)
        assert (result.returncode==0)==success,'Inspect '+str(root/'migration.log')
    stamp=datetime.now(timezone.utc);rid,cid,eid=(str(uuid.uuid4()) for _ in range(3))
    common={'case_id':1,'created_by':1,'owner_id':'schema-only','owner_token_sha256':'a'*64,
            'owner_identity_json':'{}','profile_json':'{}','heartbeat_at':stamp,'lease_expires_at':stamp}
    try:
        await command('upgrade',PREVIOUS)
        async with engine.begin() as db:
            await db.execute(User.__table__.insert().values(id=1,email='image-schema@example.com',name='Schema',password_hash='no-login'))
            await db.execute(Workspace.__table__.insert().values(id=1,user_id=1,name='Schema'))
            await db.execute(Case.__table__.insert().values(id=1,workspace_id=1,name='Schema',s3_prefix='schema-only'))
            await db.execute(NativeRequest.__table__.insert().values(**common,id=rid,launch_nonce='b'*64,kind='terminal.export'))
            before=dict((await db.execute(select(NativeRequest.__table__))).mappings().one())
        await command('upgrade','head');await command('check')
        async with engine.connect() as db:
            assert dict((await db.execute(select(NativeRequest.__table__))).mappings().one())==before
        checks.append('populated_upgrade_preserves_all_old_request_fields')
        async with engine.begin() as db:
            await db.execute(NativeRequest.__table__.insert().values(**common,id=cid,launch_nonce='c'*64,kind='terminal.cleanup'))
        checks.append('new_cleanup_kind_accepted_by_actual_database_constraint')
        for values in ({'case_id':None},{'kind':'unregistered.cleanup'}):
            try:
                async with engine.begin() as db:await db.execute(update(NativeRequest).where(NativeRequest.id==cid).values(**values))
            except IntegrityError:pass
            else:raise AssertionError('Invalid cleanup scope accepted')
        checks.append('missing_case_and_unknown_kind_rejected')
        await command('downgrade',PREVIOUS,success=False)
        async with engine.begin() as db:
            assert await db.scalar(text('SELECT version_num FROM alembic_version'))==HEAD
            assert await db.scalar(select(NativeRequest.kind).where(NativeRequest.id==cid))=='terminal.cleanup'
            await db.execute(delete(NativeRequest).where(NativeRequest.id==cid))
            await db.execute(DurableEvent.__table__.insert().values(id=eid,case_id=1,seq=1,type='terminal.image_cleanup_started',payload_json='{}'))
        checks.append('registered_cleanup_prevents_destructive_downgrade')
        await command('downgrade',PREVIOUS,success=False)
        async with engine.begin() as db:
            assert await db.scalar(text('SELECT version_num FROM alembic_version'))==HEAD
            await db.execute(update(DurableEvent).where(DurableEvent.id==eid).values(type='terminal.image_cleanup_completed'))
        await command('downgrade',PREVIOUS,success=False)
        async with engine.begin() as db:await db.execute(delete(DurableEvent).where(DurableEvent.id==eid))
        checks.append('intent_or_completion_history_prevents_destructive_downgrade')
        await command('downgrade',PREVIOUS);await command('upgrade','head');await command('check')
        async with engine.connect() as db:assert dict((await db.execute(select(NativeRequest.__table__))).mappings().one())==before
        checks.append('history_without_cleanup_roundtrips_and_matches_metadata')
        result={'passed':True,'classification':'schema_protocol_only','llm_calls':0,'mock_llm_calls':0,'checks':checks}
        (root/'checks.json').write_text(json.dumps(result,indent=2));return result
    finally:await engine.dispose()


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='image-cleanup-migration-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await verify('sqlite+aiosqlite:///'+(root/'schema.sqlite').as_posix(),root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_image_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_image_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,user=configured.username,
            password=configured.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            results['postgresql']=await verify(configured.set(database=name).render_as_string(hide_password=False),root/'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');results['temporary_postgres_dropped']=True
            await admin.close()
    (root/'checks.json').write_text(json.dumps(results,indent=2))
    print('IMAGE_CLEANUP_MIGRATION_VERIFIED '+str(root),flush=True)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)]);asyncio.run(main())
