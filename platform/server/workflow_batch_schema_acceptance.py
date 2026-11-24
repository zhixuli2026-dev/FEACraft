"""Actual disposable SQL migration and ownership checks; zero LLM calls."""
import argparse,asyncio,json,os,re,subprocess,sys,tempfile,uuid
from pathlib import Path
from sqlalchemy import delete,inspect,select,text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from workflow_migration_smoke import engine_for

SERVER=Path(__file__).resolve().parent
PREVIOUS='e8c3f09624ab';HEAD='f9d4a10735bc'


async def verify(url,root):
    from app.models import User,Workspace,Case,Organization,WorkflowRun,WorkflowBatch,WorkflowBatchMember
    root.mkdir();engine=engine_for(url);checks=[]
    async def migrate(*args,success=True):
        await engine.dispose()
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-B','-m','alembic',*args],cwd=SERVER,
            env=dict(os.environ,VIBEFLOW_DATABASE_URL=url,VIBEFLOW_STORAGE_BACKEND='fs'),capture_output=True,timeout=60)
        with (root/'migration.log').open('ab') as stream:stream.write(result.stdout+result.stderr)
        assert (result.returncode==0)==success,'Inspect '+str(root/'migration.log')
    async def refused(table,values):
        try:
            async with engine.begin() as db:await db.execute(table.insert().values(**values))
        except IntegrityError:return
        raise AssertionError('Actual SQL accepted an invalid workflow batch record')
    try:
        await migrate('upgrade',PREVIOUS)
        rid=str(uuid.uuid4())
        async with engine.begin() as db:
            await db.execute(User.__table__.insert().values(id=1,email='batch-schema@example.com',name='Schema',password_hash='no-login'))
            await db.execute(Organization.__table__.insert().values(id=1,name='Schema'))
            await db.execute(Workspace.__table__.insert().values(id=1,user_id=1,name='Schema'))
            for cid in [1,2]:await db.execute(Case.__table__.insert().values(id=cid,workspace_id=1,name='Schema '+str(cid),s3_prefix='batch-schema/'+str(cid)))
            await db.execute(WorkflowRun.__table__.insert().values(id=rid,case_id=1,created_by=1,org_id=1,
                target='solve',expected_revision=0,run_dir='C-run/case-1',request_hash='a'*64))
            before=dict((await db.execute(select(WorkflowRun.__table__))).mappings().one())
        await migrate('upgrade','head');await migrate('check')
        async with engine.connect() as db:
            assert dict((await db.execute(select(WorkflowRun.__table__))).mappings().one())==before
            names=await db.run_sync(lambda c:set(inspect(c).get_table_names()))
            assert {'workflow_batches','workflow_batch_members'}<=names
        checks.append('actual_populated_upgrade_preserves_old_workflow_fields')
        bid=str(uuid.uuid4())
        parent={'id':bid,'created_by':1,'owner_id':1,'idempotency_key':'actual-schema-key','request_hash':'b'*64,
            'request_json':'{}','membership_json':'[]','membership_sha256':'c'*64,'max_concurrent':2,'delivery_mode':'redis'}
        member={'run_id':rid,'batch_id':bid,'ordinal':0,'member_key':'native-member','case_id':1,'request_hash':'a'*64}
        async with engine.begin() as db:
            await db.execute(WorkflowBatch.__table__.insert().values(**parent))
            await db.execute(WorkflowBatchMember.__table__.insert().values(**member))
        checks.append('actual_parent_and_existing_typed_run_membership_roundtrip')
        await refused(WorkflowBatch.__table__,{**parent,'id':str(uuid.uuid4())})
        checks.append('same_creator_request_key_is_unique_in_database')
        for change in [{'max_concurrent':0},{'max_concurrent':5},{'delivery_mode':'unknown'},{'version':-1}]:
            await refused(WorkflowBatch.__table__,{**parent,'id':str(uuid.uuid4()),'idempotency_key':uuid.uuid4().hex,**change})
        checks.append('invalid_capacity_delivery_mode_and_version_refused')
        async with engine.begin() as db:await db.execute(delete(WorkflowBatchMember))
        for change in [{'case_id':2},{'batch_id':str(uuid.uuid4())},{'ordinal':50}]:
            await refused(WorkflowBatchMember.__table__,{**member,**change})
        checks.append('actual_cross_case_run_unknown_parent_and_ordinal_refused')
        async with engine.begin() as db:await db.execute(WorkflowBatchMember.__table__.insert().values(**member))
        await migrate('downgrade',PREVIOUS,success=False)
        async with engine.connect() as db:
            assert await db.scalar(text('SELECT version_num FROM alembic_version'))==HEAD
            assert (await db.execute(select(WorkflowBatchMember.run_id))).scalar_one()==rid
            assert dict((await db.execute(select(WorkflowRun.__table__))).mappings().one())==before
        checks.append('populated_downgrade_refused_with_membership_and_old_workflow_intact')
        async with engine.begin() as db:
            await db.execute(delete(WorkflowBatchMember));await db.execute(delete(WorkflowBatch))
        await migrate('downgrade',PREVIOUS)
        async with engine.connect() as db:
            assert dict((await db.execute(select(WorkflowRun.__table__))).mappings().one())==before
            names=await db.run_sync(lambda c:set(inspect(c).get_table_names()))
            assert 'workflow_batches' not in names and 'workflow_batch_members' not in names
        await migrate('upgrade','head');await migrate('check')
        checks.append('empty_downgrade_reupgrade_retains_original_workflow')
        report={'passed':True,'kind':'actual_SQL_schema_and_protocol','checks':checks,'llm_calls':0,'mock_llm_calls':0}
        (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        return report
    finally:await engine.dispose()


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='workflow-batch-schema-',dir=SERVER.parents[1]/'tmp'))
    (root/'executed-entry-source.py').write_bytes(Path(__file__).read_bytes())
    print('BATCH_SCHEMA_EVIDENCE '+str(root),flush=True)
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'developer_database_modified':False,'databases':{}}
    try:
        report['databases']['sqlite']=await verify('sqlite+aiosqlite:///'+(root/'schema.sqlite').as_posix(),root/'sqlite')
        if args.postgres:
            import asyncpg
            from app.config import Settings
            configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
            assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
            name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
            admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,user=configured.username,
                password=configured.password,database='postgres',timeout=10);created=False
            try:
                await admin.execute('CREATE DATABASE "'+name+'"');created=True
                report['databases']['postgresql']=await verify(configured.set(database=name).render_as_string(hide_password=False),root/'postgresql')
            finally:
                try:
                    if created:
                        assert await admin.fetchval('SELECT current_database()')=='postgres'
                        await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
                finally:await admin.close()
        report['passed']=True
    finally:(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'checks':{k:len(v['checks']) for k,v in report['databases'].items()},'evidence':str(root),'llm_calls':0,'mock_llm_calls':0}))


if __name__=='__main__':asyncio.run(main())
