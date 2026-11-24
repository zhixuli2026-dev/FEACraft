"""Agent memory derivation, crash recovery and concurrent append on FS/S3."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import uuid

from testsupport import IsolatedServer, SERVER
from file_transactions_smoke import interrupt, wait_ready
from app.services.memory import MEMORY_PATH, MAX_MEMORY_BYTES

FIXTURE = '''
import time,json
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.tools import ToolExecutor
from app.services import memory, file_transactions as journal
from app.services.durable_events import append_event
from app.models import DurableEvent
from app import storage
from app.main import app
root=Path(__file__).parent
def mode():return (root/'mode').read_text() if (root/'mode').exists() else ''
def hold():
    (root/'ready').touch()
    deadline=time.monotonic()+60
    while not (root/'release').exists():
        if time.monotonic()>deadline:raise TimeoutError('Memory barrier')
        time.sleep(.02)
commit=AsyncSession.commit
async def committing(self):
    if self.info.get('local_file_transactions') and mode()=='before_commit':hold()
    result=await commit(self)
    if self.info.get('local_file_transactions') and mode()=='after_commit':hold()
    return result
AsyncSession.commit=committing
read=memory.read_memory
def reading(prefix):
    result=read(prefix)
    if mode()=='derive_conflict':storage.put_text(prefix,memory.MEMORY_PATH,'external edit')
    return result
memory.read_memory=reading
@app.post('/fixture/memory/{case_id}')
async def change(case_id:int,body:dict,db=Depends(get_db),user=Depends(get_current_user)):
    case=await get_owned_case(case_id,user,db)
    executor=ToolExecutor(case.s3_prefix,case_id=case.id,user_id=user.id)
    result=await executor.execute_case_tool(db,case,'update_memory',body)
    await append_event(db,case.id,'fixture.memory_result',{'content':body.get('content','')})
    if mode()=='rollback':
        await db.rollback()
        return {'rolled_back':True}
    await db.commit()
    await journal.finish_committed(db,case)
    return result
@app.get('/fixture/memory/{case_id}')
async def inspect(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
    case=await get_owned_case(case_id,user,db)
    rows=await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case.id,
        DurableEvent.type=='fixture.memory_result').order_by(DurableEvent.seq))
    return [json.loads(row.payload_json) for row in rows]
'''

MODES = ('before_commit', 'after_commit', 'rollback', 'derive_conflict', 'concurrent', 'compact', 'missing', 'invalid')


def server(label, settings, database_url=None):
    api=IsolatedServer(label,app='memory_fixture:app',database_url=database_url)
    (api.root/'memory_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    return api


def verify(store=None,database_url=None,modes=None):
    report=Path(tempfile.mkdtemp(prefix='memory-transactions-evidence-',dir=SERVER.parents[1]/'tmp'))
    settings=store.settings() if store else {}
    if not database_url:
        settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
    checks=[]
    for mode in modes or (MODES + (() if store else ('trash_after_commit',))):
        with server('memory-transaction-'+mode,settings,database_url) as api:
            case=api.create_case('Memory '+mode)
            endpoint='/fixture/memory/'+str(case['id'])
            api.put(MEMORY_PATH,'old memory')
            if mode=='trash_after_commit':
                assert store is None, 'This scenario targets local directory trash'
                overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET','VIBEFLOW_STORAGE_FS_ROOT')}
                overrides.update(settings)
                with server('memory-trash-peer',overrides,database_url) as peer:
                    peer.token=api.token
                    (api.root/'mode').write_text('after_commit')
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        writing=pool.submit(api.call,'POST',endpoint,{'content':'new memory'},api_prefix=False)
                        try:
                            wait_ready(api)
                            peer.call('DELETE',api.prefix)
                            assert not writing.done()
                        finally:(api.root/'release').touch()
                        assert writing.result(timeout=20)['ok']
                    peer.call('POST',api.prefix+':restore')
                    assert peer.call('GET',api.prefix+'/file?path='+MEMORY_PATH)['content']=='old memory\n\nnew memory\n'
                    assert len(peer.call('GET',endpoint,api_prefix=False))==1
                    assert peer.call('GET',api.prefix+'/file-transactions')['status']=='idle'
                checks.append({'mode':mode,'api':str(api.root),'peer':str(peer.root)})
                print('PASS fs '+mode,flush=True)
                continue
            if mode=='missing':api.call('DELETE',api.prefix+'/file?path='+MEMORY_PATH)
            if mode=='compact':api.put(MEMORY_PATH,('older line\n'*8000)+'LATEST MEMORY\n')
            (api.root/'mode').write_text(mode)
            payload={'action':'compact'} if mode=='compact' else {'content':'new memory'}
            if mode in {'before_commit','after_commit'}:
                interrupt(api,lambda:api.call('POST',endpoint,payload,api_prefix=False))
            elif mode=='concurrent':
                with ThreadPoolExecutor(max_workers=2) as pool:
                    a=pool.submit(api.call,'POST',endpoint,{'content':'append A'},api_prefix=False)
                    b=pool.submit(api.call,'POST',endpoint,{'content':'append B'},api_prefix=False)
                    assert a.result()['ok'] and b.result()['ok']
            elif mode=='invalid':
                api.call('POST',endpoint,{'action':'delete'},expect=500,api_prefix=False,raw=True)
            else:
                api.call('POST',endpoint,payload,expect=409 if mode=='derive_conflict' else 200,api_prefix=False)
            overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')}
            overrides.update(settings)
            if not store:overrides['VIBEFLOW_STORAGE_FS_ROOT']=api.env['VIBEFLOW_STORAGE_FS_ROOT']
            peer=server('memory-transaction-peer',overrides,database_url)
            if store:assert not (peer.root/'files').exists()
            with peer:
                peer.token=api.token
                data=peer.call('GET',api.prefix+'/file?path='+MEMORY_PATH)['content']
                events=peer.call('GET',endpoint,api_prefix=False)
                assert peer.call('GET',api.prefix+'/file-transactions')['status']=='idle'
                if mode in {'before_commit','rollback','invalid'}:
                    assert data=='old memory' and events==[],(mode,data,events)
                elif mode=='after_commit':
                    assert data=='old memory\n\nnew memory\n' and len(events)==1,(data,events)
                elif mode=='derive_conflict':
                    assert data=='external edit' and events==[],(data,events)
                elif mode=='concurrent':
                    assert data.count('append A')==data.count('append B')==1 and len(events)==2,(data,events)
                elif mode=='compact':
                    assert len(data.encode())<=MAX_MEMORY_BYTES and 'LATEST MEMORY' in data and len(events)==1
                else:assert data=='new memory\n' and len(events)==1
                assert peer.call('GET',api.prefix+'/file?path='+MEMORY_PATH)['content']==data
            checks.append({'mode':mode,'api':str(api.root),'peer':str(peer.root)})
            print('PASS '+('s3' if store else 'fs')+' '+mode,flush=True)
    result={'backend':'s3' if store else 'fs','count':len(checks),'checks':checks,'database':'postgresql' if database_url else 'sqlite'}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'status':'passed','count':len(checks),'evidence':str(report)}),flush=True)
    return report,result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--s3',action='store_true')
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--modes',nargs='+')
    args=parser.parse_args()
    from s3testsupport import ObjectStore
    from s3_typed_publication_smoke import RUNTIME
    with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
        if not args.postgres:
            await asyncio.to_thread(verify,store,None,args.modes)
            return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_memoryjournal_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            logs=Path(tempfile.mkdtemp(prefix='memory-migration-',dir=SERVER.parents[1]/'tmp'))
            await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
            report,result=await asyncio.to_thread(verify,store,temporary,args.modes)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result['temporary_database_removed']=True
        (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
