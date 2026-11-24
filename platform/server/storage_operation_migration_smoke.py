"""Storage reservation constraints and history-preserving downgrade on disposable DBs."""
import argparse,asyncio,json,tempfile,uuid
from pathlib import Path
from datetime import datetime,timezone
from sqlalchemy import select,delete,update,text
from sqlalchemy.exc import IntegrityError
from workflow_migration_smoke import alembic,engine_for,legacy,HEAD,SERVER


async def run(url,report):
    report.mkdir(parents=True,exist_ok=False)
    await asyncio.to_thread(alembic,url,report,'upgrade','head')
    await legacy(url,seed=True)
    from app.models import CaseStorageOperation as Operation
    row={'id':str(uuid.uuid4()),'case_id':101,'source_prefix':'migration/legacy/',
         'purpose':'staging','state':'active','owner_json':'{}','created_at':datetime.now(timezone.utc)}
    engine=engine_for(url);checks=[]
    try:
        async with engine.begin() as connection:
            await connection.execute(Operation.__table__.insert().values(**row))
            for label,patch in [('one_active_per_case',{}),('ended_active_forbidden',{'ended_at':row['created_at']}),
                ('invalid_purpose',{'purpose':'unknown','state':'drained','ended_at':row['created_at']})]:
                transaction=await connection.begin_nested()
                try:
                    if label=='ended_active_forbidden':
                        await connection.execute(update(Operation).where(Operation.id==row['id']).values(**patch))
                    else:
                        await connection.execute(Operation.__table__.insert().values(**dict(row,id=str(uuid.uuid4()),**patch)))
                except IntegrityError:await transaction.rollback()
                else:
                    await transaction.rollback();raise AssertionError(label+' was admitted')
                checks.append(label)
        for state in ('active','drained'):
            if state=='drained':
                async with engine.begin() as connection:
                    await connection.execute(update(Operation).where(Operation.id==row['id']).values(state='drained',ended_at=row['created_at']))
            try:await asyncio.to_thread(alembic,url,report,'downgrade','6a4fc1b5e087')
            except AssertionError:pass
            else:raise AssertionError('Downgrade discarded '+state+' storage history')
            async with engine.connect() as connection:
                assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()==HEAD
                assert (await connection.execute(select(Operation.state).where(Operation.id==row['id']))).scalar_one()==state
            checks.append('downgrade_preserves_'+state)
        async with engine.begin() as connection:
            await connection.execute(delete(Operation).where(Operation.id==row['id']))
    finally:await engine.dispose()
    await asyncio.to_thread(alembic,url,report,'downgrade','6a4fc1b5e087')
    await asyncio.to_thread(alembic,url,report,'upgrade','head')
    await asyncio.to_thread(alembic,url,report,'check')
    checks+=['empty_roundtrip','metadata_matches']
    return {'passed':True,'count':len(checks),'checks':checks}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='storage-operation-migration-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await run('sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix(),report/'sqlite')}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_storageop_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            results['postgresql']=await run(url.set(database=name).render_as_string(hide_password=False),report/'postgresql')
        finally:
            if created:
                await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',name)
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'checks.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'evidence':str(report)}),flush=True)


if __name__=='__main__':asyncio.run(main())
