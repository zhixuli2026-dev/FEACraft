"""Real API/process crash boundaries for sealed-input preparation and SQL proof."""
import argparse,asyncio,json,tempfile,time,uuid
from pathlib import Path
from testsupport import IsolatedServer,SERVER
from case_provision_smoke import crash
from case_constructor_recovery_smoke import contents,put_foreign,read_plans
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME

FIXTURE='''
import time
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app import storage
from app.services import attempt_seal_journal as seals,attempt_workspace as workspace,attempt_objects as objects
ROOT=Path(__file__).parent
def mode():return (ROOT/'mode').read_text() if (ROOT/'mode').exists() else ''
def hold():
 (ROOT/'ready').touch()
 while not (ROOT/'release').exists():time.sleep(.02)
prepare,copy,local_prepare,remote_prepare,remote_put,commit,witness=seals.prepare,workspace.copy_records,workspace.Snapshot.prepare,objects.Snapshot.prepare,objects.put_new,AsyncSession.commit,seals.witness
def preparing(*args):
 value=prepare(*args)
 if mode() in {'after-plan','legacy'}:hold()
 return value
def copying(source,target,*args,**kwargs):
 result=copy(source,target,*args,**kwargs)
 if not storage._use_s3() and Path(target).name=='inputs' and mode() in {'during-copy','foreign'}:hold()
 return result
def local_ready(self):
 result=local_prepare(self)
 if not storage._use_s3() and mode() in {'after-copy','corrupt','automatic'}:hold()
 return result
def remote_ready(self):
 result=remote_prepare(self)
 if mode() in {'after-copy','corrupt','automatic'}:hold()
 return result
def putting(prefix,path,data,*args,**kwargs):
 result=remote_put(prefix,path,data,*args,**kwargs)
 if '/attempts/' in path and '/inputs/' in path and mode() in {'during-copy','foreign'}:hold()
 return result
async def committing(self):
 owned=self.info.get('attempt_seals',{})
 if owned and mode()=='before-commit':hold()
 result=await commit(self)
 if owned and mode() in {'after-commit','missing-proof'}:hold()
 if owned and mode()=='lost-commit':raise RuntimeError('lost sealing commit response')
 return result
seals.prepare,workspace.copy_records,workspace.Snapshot.prepare,objects.Snapshot.prepare,objects.put_new,AsyncSession.commit=preparing,copying,local_ready,remote_ready,putting,committing
async def witnessing(db,case,attempt,plan):
 if mode()=='missing-proof':
  db.info.setdefault('attempt_seals',{})[attempt.id]=db.get_transaction()
  return
 return await witness(db,case,attempt,plan)
seals.witness=witnessing
'''


def boot(settings,database_url=None,fixture=True):
    api=IsolatedServer('attempt-seal',app='seal_fixture:app',database_url=database_url)
    (api.root/'seal_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+(FIXTURE if fixture else 'from app.main import app\n'),encoding='utf8')
    return api


def verify(store=None,database_url=None,modes=None):
    output=Path(tempfile.mkdtemp(prefix='seal-recovery-evidence-',dir=SERVER.parents[1]/'tmp'))
    settings=store.settings() if store else {}
    if store and not database_url:settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(output/'test.sqlite').as_posix()
    checks=[]
    for mode in modes or ['after-plan','during-copy','after-copy','before-commit','after-commit','lost-commit','foreign']:
        with boot(settings,database_url) as api:
            source=api.create_case('Seal '+mode);owner=api.call('GET','/auth/me')['id']
            prefix=read_plans(api,store,owner)[0]['binding']['prefix']
            api.put('Ref/input.txt','SEALED INPUT')
            initial=contents(api,store,prefix)
            (api.root/'mode').write_text(mode)
            request=lambda:api.call('POST',api.prefix+'/jobs',{'command':'echo SEALED_RECOVERY','input_paths':['Ref/input.txt']},expect=500 if mode=='lost-commit' else 200,raw=mode=='lost-commit')
            if mode=='lost-commit':request()
            else:crash(api,request)
            files=contents(api,store,prefix)
            records=[(path,json.loads(data)) for path,data in files.items() if path.startswith('.vibeflow/attempt-provisions/pending/')]
            assert len(records)==1,records
            record_path,plan=records[0];identifier=plan['attempt_id']
            legacy_path=None
            if mode=='legacy':
                legacy_path='.vibeflow/attempts/'+str(uuid.uuid4())+'/old.txt'
                put_foreign(api,store,prefix,legacy_path,b'LEGACY NO PROOF')
            if mode=='corrupt':
                changed=json.loads(json.dumps(plan));changed['binding']['prefix']='foreign/case'
                put_foreign(api,store,prefix,record_path,json.dumps(changed).encode())
            if mode=='foreign':
                path='.vibeflow/attempts/'+(identifier if store else '.preparing-'+identifier)+'/FOREIGN.txt'
                put_foreign(api,store,prefix,path,b'FOREIGN')
            shared={**settings,'VIBEFLOW_DATABASE_URL':database_url or settings.get('VIBEFLOW_DATABASE_URL',api.env['VIBEFLOW_DATABASE_URL']),
                    'VIBEFLOW_JWT_SECRET':api.env['VIBEFLOW_JWT_SECRET']}
            if not store:shared.update({key:api.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_INDEX_ROOT')})
            if mode=='automatic':shared.update(VIBEFLOW_STORAGE_MAINTENANCE_ENABLED='true',VIBEFLOW_STORAGE_MAINTENANCE_INTERVAL_SECONDS='1')
            with boot(shared,database_url,fixture=False) as peer:
                peer.token=api.token
                endpoint=f'/cases/{source["id"]}/attempt-preparations'
                diagnosed=peer.call('GET',endpoint)
                if mode=='automatic':
                    deadline=time.monotonic()+30
                    while diagnosed['results']:
                        assert time.monotonic()<deadline,diagnosed
                        time.sleep(.1);diagnosed=peer.call('GET',endpoint)
                committed=mode in {'after-commit','lost-commit'}
                if mode!='automatic':assert diagnosed['results'][0]['status']==('recovery_required' if mode in {'corrupt','missing-proof'} else 'committed' if committed else 'uncommitted'),diagnosed
                if legacy_path:assert any(legacy_path.startswith(row['path']+'/') for row in diagnosed['untracked']),diagnosed
                before=contents(api,store,prefix)
                result=peer.call('POST',endpoint+':recover')['results']
                expected='committed' if committed else 'recovery_required' if mode in {'foreign','corrupt','missing-proof'} else 'rolled_back'
                if mode=='automatic':
                    assert result==[]
                    history=json.loads(contents(api,store,prefix)['.vibeflow/attempt-provisions/history/'+identifier+'.json'])
                    assert history['outcome']=='rolled_back' and history['plan']==plan
                else:assert len(result)==1 and result[0]['status']==expected,(mode,result)
                after=contents(api,store,prefix)
                assert after['Ref/input.txt']==initial['Ref/input.txt']
                if committed:
                    assert after[plan['manifest']['inputs_path']+'/Ref/input.txt']==b'SEALED INPUT'
                    assert len(peer.call('GET',f'/cases/{source["id"]}/jobs'))==1
                elif mode in {'foreign','corrupt','missing-proof'}:
                    if mode!='missing-proof':assert after==before
                    else:assert after[record_path]==before[record_path]
                    assert record_path in after
                    assert len(peer.call('GET',f'/cases/{source["id"]}/jobs'))==(1 if mode=='missing-proof' else 0)
                else:
                    assert not any(path.startswith('.vibeflow/attempts/') and path!=legacy_path for path in after),after.keys()
                    if legacy_path:assert after[legacy_path]==b'LEGACY NO PROOF'
                    assert peer.call('GET',f'/cases/{source["id"]}/jobs')==[]
                if mode not in {'foreign','corrupt','missing-proof'}:
                    assert record_path not in after
                    assert peer.call('POST',endpoint+':recover')['results']==[]
                # Sealing recovery records are private execution metadata.
                assert peer.call('PUT',f'/cases/{source["id"]}/file',{'path':record_path,'content':'FORGED'},expect=409)['detail']['code']=='input_sealed'
                checks.append({'mode':mode,'api':str(api.root),'peer':str(peer.root),'attempt_id':identifier})
        (output/'checks.json').write_text(json.dumps({'passed':False,'count':len(checks),'checks':checks},indent=2),encoding='utf8')
        print('PASS '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'storage':'s3' if store else 'fs','database':'postgresql' if database_url else 'sqlite'}
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'count':len(checks),'evidence':str(output)}),flush=True)
    return output,result


async def main():
    from contextlib import ExitStack
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
    with ExitStack() as stack:
        store=stack.enter_context(ObjectStore(executable=RUNTIME,volume_max=12)) if args.s3 else None
        if not args.postgres:
            await asyncio.to_thread(verify,store,None,args.modes);return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_seal_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            logs=Path(tempfile.mkdtemp(prefix='seal-migration-',dir=SERVER.parents[1]/'tmp'))
            await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
            output,result=await asyncio.to_thread(verify,store,temporary,args.modes)
        finally:
            if created:
                await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',name)
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result['temporary_database_removed']=True
        (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
