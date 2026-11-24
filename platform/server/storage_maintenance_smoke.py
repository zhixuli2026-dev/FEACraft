"""Actual startup recovery, retention and account isolation on FS/S3 and SQLite/PG."""
import argparse,asyncio,hashlib,json,os,tempfile,time,uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import httpx
from testsupport import IsolatedServer,SERVER
from case_provision_smoke import crash
from case_constructor_recovery_smoke import contents,put_foreign,read_plans
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME

FIXTURE='''
import os,time
from pathlib import Path
from app.main import app
from app import storage
from app.services import case_provision as provision, uploads, storage_maintenance as maintenance
ROOT=Path(__file__).parent
def mode():return (ROOT/'mode').read_text() if (ROOT/'mode').exists() else ''
def hold():
 (ROOT/'ready').touch()
 while not (ROOT/'release').exists():time.sleep(.02)
link,write,cleanup,modified=os.link,provision.write_file,uploads.cleanup_completed,maintenance.modified
def linking(source,target):
 if mode()=='interrupt' and Path(target).name=='caseDict':hold()
 return link(source,target)
def writing(plan,path,data):
 result=write(plan,path,data)
 if storage._use_s3() and mode()=='interrupt':hold()
 return result
def cleaning(*args):
 if mode()=='cleanup-failure':raise OSError('injected cleanup failure')
 return cleanup(*args)
def modification(prefix,path):
 if (ROOT/'age-history').exists() and path.startswith('history/'):return time.time()-3*86400
 return modified(prefix,path)
os.link,provision.write_file,uploads.cleanup_completed,maintenance.modified=linking,writing,cleaning,modification
'''


def boot(settings,database_url=None):
    api=IsolatedServer('storage-maintenance',app='maintenance_fixture:app',database_url=database_url)
    (api.root/'maintenance_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    return api


def verify(store=None,database_url=None):
    output=Path(tempfile.mkdtemp(prefix='maintenance-evidence-',dir=SERVER.parents[1]/'tmp'))
    settings=store.settings() if store else {}
    if store and not database_url:settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(output/'test.sqlite').as_posix()
    checks=[]
    with boot(settings,database_url) as first:
        case=first.create_case('Maintenance source');token=first.token
        owner=first.call('GET','/auth/me')['id'];ws=first.call('GET','/workspaces')[0]['id']
        source_prefix=read_plans(first,store,owner)[0]['binding']['prefix']
        first.call('POST','/case-provisions:recover')
        (first.root/'mode').write_text('interrupt')
        crash(first,lambda:first.call('POST',f'/workspaces/{ws}/cases',{'name':'Interrupted creation'}))
        plans=read_plans(first,store,owner);assert len(plans)==1
        shared={**settings,'VIBEFLOW_DATABASE_URL':database_url or settings.get('VIBEFLOW_DATABASE_URL',first.env['VIBEFLOW_DATABASE_URL']),
                'VIBEFLOW_JWT_SECRET':first.env['VIBEFLOW_JWT_SECRET']}
        if not store:
            shared.update({key:first.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_INDEX_ROOT')})
            stage=Path(first.env['VIBEFLOW_STORAGE_FS_ROOT'])/f'workspaces/{owner}/.case-provisions/staging'
            intents=list(stage.glob('*.json'));assert len(intents)==1
            raw=json.loads(intents[0].read_text());raw['created_at']=time.time()-2*86400
            intents[0].write_text(json.dumps(raw));os.utime(intents[0],(raw['created_at'],raw['created_at']))
            # Retain old unowned files and a partial/foreign file with a valid intent.
            legacy=stage/uuid.uuid4().hex;legacy.write_bytes(b'LEGACY UNOWNED')
            foreign_id=uuid.uuid4().hex;foreign=dict(raw,id=foreign_id)
            bad_meta=stage/(foreign_id+'.json');bad_meta.write_text(json.dumps(foreign));os.utime(bad_meta,(raw['created_at'],raw['created_at']))
            (stage/(foreign_id+'.part')).write_bytes(b'FOREIGN')
        automatic={**shared,'VIBEFLOW_STORAGE_MAINTENANCE_ENABLED':'true','VIBEFLOW_STORAGE_MAINTENANCE_INTERVAL_SECONDS':'1'}
        with boot(automatic,database_url) as recovered:
            recovered.token=token
            deadline=time.monotonic()+30
            while recovered.call('GET','/case-provisions:maintenance')['pending_records']:
                assert time.monotonic()<deadline,'Startup did not recover pending creation';time.sleep(.1)
            assert recovered.call('GET',f'/workspaces/{ws}/cases')[0]['id']==case['id']
            assert len(recovered.call('GET',f'/workspaces/{ws}/cases'))==1
            assert not contents(first,store,plans[0]['binding']['prefix'])
            if not store:
                deadline=time.monotonic()+20
                while intents[0].exists():
                    assert time.monotonic()<deadline;time.sleep(.1)
                assert legacy.read_bytes()==b'LEGACY UNOWNED'
                assert (stage/(foreign_id+'.part')).read_bytes()==b'FOREIGN'
                checks.append('owned_aged_stage_cleaned_partial_and_legacy_staging_retained')
            checks.append('real_process_interruption_recovered_automatically_on_fresh_API_startup')
        manual={**shared,'VIBEFLOW_STORAGE_MAINTENANCE_ENABLED':'false','VIBEFLOW_PROVISION_HISTORY_RETENTION_DAYS':'1'}
        with boot(manual,database_url) as api:
            api.token=token;route=f'/cases/{case["id"]}'
            before=api.call('GET','/case-provisions:maintenance');assert before['history_records']>=2
            # A real, aged rollback receipt can remove its empty directory and history.
            (api.root/'age-history').touch()
            result=api.call('POST','/case-provisions:maintenance')
            assert sum(r['status']=='expired_history_removed' for r in result['history'])>=2,result
            assert api.call('GET','/case-provisions:maintenance')['reservation_records']>=2
            assert contents(first,store,source_prefix)
            checks.append('expired_verified_receipts_removed_claims_and_committed_case_preserved')
            outsider=f'workspaces/{owner}/cases/999999'
            put_foreign(first,store,outsider,'foreign.txt',b'ORPHAN')
            # Diagnostic reads never recover or delete; another account sees only its own storage.
            other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'maintenance-test-123','name':'Other'})
            other_headers={'Authorization':'Bearer '+other['token']}
            assert outsider in api.call('GET','/case-provisions:maintenance')['unreferenced_prefixes']
            assert outsider not in api.call('GET','/case-provisions:maintenance',extra_headers=other_headers)['unreferenced_prefixes']
            api.call('POST','/case-provisions:maintenance',extra_headers=other_headers)
            api.call('POST','/case-provisions:maintenance')
            assert contents(first,store,outsider)=={'foreign.txt':b'ORPHAN'}
            checks.append('unreferenced_prefix_diagnosed_retained_and_account_isolated')
            body=b'completed upload data';pending=b'pending'
            sessions=[]
            for path,data in [('Ref/complete.bin',body),('Ref/pending.bin',pending)]:
                item=api.call('POST',route+'/uploads',{'path':path,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'overwrite':False})
                with httpx.Client(trust_env=False,timeout=20) as client:
                    sent=client.put(api.base+'/api/v1'+route+'/uploads/'+item['id']+'?offset=0',content=data,headers={'Authorization':'Bearer '+token})
                    assert sent.status_code==200,sent.text
                sessions.append(item)
            (api.root/'mode').write_text('cleanup-failure')
            api.call('POST',route+'/uploads/'+sessions[0]['id']+':complete')
            (api.root/'mode').unlink()
            api.call('PUT',route+'/file',{'path':'Ref/complete.bin','content':'LATER EDIT'})
            result=api.call('POST','/case-provisions:maintenance')
            assert any(r.get('upload_id')==sessions[0]['id'] and r['status']=='completed_upload_cleaned' for r in result['uploads']),result
            assert any(r.get('upload_id')==sessions[1]['id'] and r['status']=='incomplete_upload_retained' for r in result['uploads']),result
            assert api.call('GET',route+'/raw?path=Ref/complete.bin',raw=True)==b'LATER EDIT'
            assert api.call('GET',route+'/uploads/'+sessions[1]['id'])['offset']==len(pending)
            checks.append('committed_upload_cleanup_retried_pending_upload_and_later_edit_preserved')
            created=api.call('POST',f'/workspaces/{ws}/cases',{'name':'History validation'})
            plan=next(p for p in read_plans(first,store,owner) if p['case_id']==created['id'])
            api.call('POST','/case-provisions:recover')
            history_prefix=f'workspaces/{owner}/.case-provisions'
            history_path=f'history/{plan["id"]}.json'
            raw=contents(first,store,history_prefix)[history_path]
            damaged=json.loads(raw);damaged['plan']['kind']='foreign-history'
            encoded=json.dumps(damaged).encode()
            put_foreign(first,store,history_prefix,history_path,encoded)
            result=api.call('POST','/case-provisions:maintenance')
            assert any(r['path']==history_path and r['status']=='recovery_required' for r in result['history']),result
            assert contents(first,store,history_prefix)[history_path]==encoded
            assert contents(first,store,plan['binding']['prefix'])
            checks.append('aged_history_with_mismatched_SQL_proof_retained_with_case_content')
            # Recovery waits for a creator instead of mistaking its files for stale data.
            (api.root/'mode').write_text('interrupt')
            with boot(shared,database_url) as peer:
                peer.token=token
                with ThreadPoolExecutor(max_workers=2) as pool:
                    creating=pool.submit(api.call,'POST',f'/workspaces/{ws}/cases',{'name':'Active creator'})
                    deadline=time.monotonic()+20
                    while not (api.root/'ready').exists():
                        assert time.monotonic()<deadline;time.sleep(.03)
                    maintenance=pool.submit(peer.call,'POST','/case-provisions:maintenance',expect=(200,409))
                    try:
                        time.sleep(.3)
                        if maintenance.done():
                            assert store is not None and maintenance.result()['detail']['code']=='storage_quota_busy','Maintenance bypassed owner lock'
                    finally:(api.root/'release').touch()
                    made=creating.result(timeout=30);result=maintenance.result(timeout=30)
                    if 'detail' in result:
                        assert result['detail']['code']=='storage_quota_busy'
                        result=peer.call('POST','/case-provisions:maintenance')
                    assert any(r.get('case_id')==made['id'] and r['status']=='committed' for r in result['creation']),result
            checks.append('maintenance_waits_or_refuses_active_credit_then_observes_commit')
    result={'passed':True,'count':len(checks),'checks':checks,'storage':'s3' if store else 'fs','database':'postgresql' if database_url else 'sqlite'}
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'count':len(checks),'evidence':str(output)}),flush=True)
    return output,result


async def main():
    from contextlib import ExitStack
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    with ExitStack() as stack:
        store=stack.enter_context(ObjectStore(executable=RUNTIME,volume_max=12)) if args.s3 else None
        if not args.postgres:
            await asyncio.to_thread(verify,store);return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_maintenance_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            logs=Path(tempfile.mkdtemp(prefix='maintenance-migration-',dir=SERVER.parents[1]/'tmp'))
            await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
            output,result=await asyncio.to_thread(verify,store,temporary)
        finally:
            if created:
                await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',name)
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result['temporary_database_removed']=True
        (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
