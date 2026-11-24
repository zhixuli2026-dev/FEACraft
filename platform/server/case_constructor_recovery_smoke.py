"""Real constructor interruption matrix across SQL dialects and storage backends."""
import argparse,asyncio,hashlib,io,json,tempfile,time,uuid,zipfile
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from case_provision_smoke import crash
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME

KINDS={'import':'case.import','case-copy':'case.copy','workspace-copy':'workspace.copy','restore':'backup.restore'}
FIXTURE='''
import json,time
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.services import case_provision as provision
from app import storage
ROOT=Path(__file__).parent
original_write,original_seal,original_commit=provision.write_file,provision.seal,AsyncSession.commit
def options():return json.loads((ROOT/'fault.json').read_text()) if (ROOT/'fault.json').exists() else {}
def hold():
 (ROOT/'ready').write_text('ready')
 while not (ROOT/'release').exists():time.sleep(.02)
def writing(plan,path,data):
 result=original_write(plan,path,data)
 fault=options()
 if plan['kind']!=fault.get('kind'):return result
 if fault['mode'] in {'after-file','external'}:hold()
 if fault['mode']=='rollback':raise RuntimeError('injected constructor rollback')
 if fault['mode']=='source-change':
  storage.put_bytes(fault['source_prefix'],'Ref/marker.txt',b'EXTERNAL SOURCE')
 return result
async def sealing(db,case,plan):
 result=await original_seal(db,case,plan)
 fault=options()
 if plan['kind']==fault.get('kind') and fault['mode']=='first-case':hold()
 return result
async def committing(self):
 fault=options();owned=self.info.get('case_provisions',{})
 if owned and fault.get('mode')=='before-commit':hold()
 result=await original_commit(self)
 if owned and fault.get('mode')=='after-commit':hold()
 if owned and fault.get('mode')=='lost-commit':raise RuntimeError('injected lost SQL commit response')
 return result
provision.write_file,provision.seal,AsyncSession.commit=writing,sealing,committing
'''


def boot(settings,label,database_url=None,fixture=True):
    api=IsolatedServer(label,app='constructor_fixture:app',database_url=database_url)
    (api.root/'constructor_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+
        (FIXTURE if fixture else 'from app.main import app\n'),encoding='utf8')
    return api


def read_plans(api,store,owner):
    prefix=f'workspaces/{owner}/.case-provisions/pending/'
    if store:
        keys=[row['Key'] for page in store.client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket,Prefix=prefix)
              for row in page.get('Contents',[])]
        return [json.loads(store.client.get_object(Bucket=store.bucket,Key=key)['Body'].read()) for key in keys]
    root=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])
    return [json.loads(path.read_text(encoding='utf8')) for path in (root/prefix).glob('*.json')]


def contents(api,store,prefix):
    if store:
        return {row['Key'][len(prefix)+1:]:store.client.get_object(Bucket=store.bucket,Key=row['Key'])['Body'].read()
                for page in store.client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket,Prefix=prefix+'/')
                for row in page.get('Contents',[])}
    root=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/prefix
    return {path.relative_to(root).as_posix():path.read_bytes() for path in root.rglob('*') if path.is_file()}


def put_foreign(api,store,prefix,path,data):
    if store:store.client.put_object(Bucket=store.bucket,Key=prefix+'/'+path,Body=data)
    else:
        target=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/prefix/path
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)


def verify(store=None,database_url=None,kinds=None,modes=None):
    output=Path(tempfile.mkdtemp(prefix='constructor-recovery-evidence-',dir=SERVER.parents[1]/'tmp'))
    checks=[]
    settings=store.settings() if store else {}
    if store and not database_url:settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(output/'test.sqlite').as_posix()
    for kind in kinds or KINDS:
        for mode in modes or ['after-file','before-commit','after-commit','rollback','lost-commit','first-case','source-change','external']:
            if mode=='first-case' and kind not in {'workspace-copy','restore'}:continue
            if mode=='source-change' and kind not in {'case-copy','workspace-copy'}:continue
            if mode=='external' and kind!='import':continue
            with boot(settings,'constructor-'+kind+'-'+mode,database_url) as api:
                source=api.create_case('Source '+kind+' '+mode)
                owner=api.call('GET','/auth/me')['id'];ws=api.call('GET','/workspaces')[0]['id']
                api.put('Ref/marker.txt','SOURCE MARKER')
                document=api.call('GET',api.prefix+'/caseDict')['content']+'\n// Source revision\n'
                api.call('PUT',api.prefix+'/caseDict',{'content':document})
                source_plan=read_plans(api,store,owner)[0];source_prefix=source_plan['binding']['prefix']
                second=api.call('POST',f'/workspaces/{ws}/cases',{'name':'Source second'})
                api.call('POST','/case-provisions:recover')
                before_cases=api.call('GET',f'/workspaces/{ws}/cases')
                before_workspaces=api.call('GET','/workspaces')
                source_dict=api.call('GET',api.prefix+'/caseDict')
                source_files=contents(api,store,source_prefix)
                archive=io.BytesIO()
                with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as bundle:
                    for path,data in source_files.items():
                        if not path.startswith('.vibeflow/'):bundle.writestr(path,data)
                backup=api.call('GET',f'/workspaces/{ws}/backup',raw=True) if kind=='restore' else None
                (api.root/'fault.json').write_text(json.dumps({'kind':KINDS[kind],'mode':mode,'source_prefix':source_prefix}),encoding='utf8')
                with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
                    if kind=='import':request=lambda:client.post(f'/workspaces/{ws}/cases:import',data={'name':'Imported target'},files={'file':('source.zip',archive.getvalue(),'application/zip')})
                    elif kind=='case-copy':request=lambda:client.post(f'/cases/{source["id"]}:copy',json={'name':'Copied target'})
                    elif kind=='workspace-copy':request=lambda:client.post(f'/workspaces/{ws}:copy',json={'name':'Copied workspace','include_cases':True})
                    else:request=lambda:client.post('/workspaces:restore',files={'file':('source.zip',backup,'application/zip')})
                    if mode in {'rollback','lost-commit','source-change'}:
                        response=request();assert response.status_code==(409 if mode=='source-change' else 500),(kind,mode,response.status_code,response.text[:600])
                    else:crash(api,request)
                plans=read_plans(api,store,owner)
                assert plans and all(p['kind']==KINDS[kind] for p in plans),(kind,mode,plans)
                if mode=='external':
                    put_foreign(api,store,plans[0]['binding']['prefix'],'FOREIGN.txt',b'FOREIGN')
                before_recovery={p['id']:contents(api,store,p['binding']['prefix']) for p in plans}
                peer_settings={**settings,'VIBEFLOW_DATABASE_URL':database_url or settings.get('VIBEFLOW_DATABASE_URL',api.env['VIBEFLOW_DATABASE_URL']),
                               'VIBEFLOW_JWT_SECRET':api.env['VIBEFLOW_JWT_SECRET']}
                if not store:peer_settings.update({key:api.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_INDEX_ROOT')})
                with boot(peer_settings,'constructor-peer',database_url,fixture=False) as recovered:
                    recovered.token=api.token
                    committed=mode in {'after-commit','lost-commit'}
                    visible_cases=recovered.call('GET',f'/workspaces/{ws}/cases')
                    visible_workspaces=recovered.call('GET','/workspaces')
                    if not committed:
                        assert visible_cases==before_cases,(kind,mode,'partial case rows')
                        assert visible_workspaces==before_workspaces,(kind,mode,'partial workspace rows')
                    elif kind in {'import','case-copy'}:assert len(visible_cases)==len(before_cases)+1
                    else:assert len(visible_workspaces)==len(before_workspaces)+1
                    if committed:
                        # Edits made after commit are legitimate; recovery must never replay the captured constructor.
                        target=plans[0]
                        recovered.call('PUT',f'/cases/{target["case_id"]}/file',{'path':'Ref/marker.txt','content':'LATER TARGET EDIT'})
                    outcome=recovered.call('POST','/case-provisions:recover')
                    expected='committed' if committed else 'recovery_required' if mode=='external' else 'rolled_back'
                    assert len(outcome['results'])==len(plans) and all(row['status']==expected for row in outcome['results']),(kind,mode,outcome)
                    for plan in plans:
                        actual=contents(api,store,plan['binding']['prefix'])
                        if expected=='rolled_back':assert not actual,(kind,mode,actual.keys())
                        elif expected=='recovery_required':assert actual==before_recovery[plan['id']]
                        else:
                            assert '0-caseDict/caseDict' in actual
                            if plan is plans[0]:assert actual['Ref/marker.txt']==b'LATER TARGET EDIT'
                            for path,admitted in plan['files'].items():
                                if plan is plans[0] and path=='Ref/marker.txt':continue
                                assert {'size':len(actual[path]),'sha256':hashlib.sha256(actual[path]).hexdigest()}==admitted,(kind,mode,path)
                            mirror=recovered.call('GET',f'/cases/{plan["case_id"]}/caseDict')
                            if kind!='restore':assert mirror['dict_revision']==1 if kind=='import' else mirror['dict_revision'] in {0,source_dict['dict_revision']}
                            # The final plan already contains the remapped dictionary history.
                            if kind in {'import','case-copy','workspace-copy'}:
                                event=json.loads(actual['0-caseDict/case-events.jsonl'].splitlines()[-1])
                                assert event['case_id']==plan['case_id'] and event['revision']==mirror['dict_revision']
                    original=contents(api,store,source_prefix)
                    if mode=='source-change':
                        assert original.pop('Ref/marker.txt')==b'EXTERNAL SOURCE'
                        prior=dict(source_files);prior.pop('Ref/marker.txt');assert original==prior
                    else:assert original==source_files,(kind,mode,'source modified')
                    checks.append({'kind':kind,'mode':mode,'plans':len(plans),'api':str(api.root),'peer':str(recovered.root)})
            (output/'checks.json').write_text(json.dumps({'passed':False,'count':len(checks),'checks':checks},indent=2),encoding='utf8')
            print('PASS '+kind+' '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'database':'postgresql' if database_url else 'sqlite','storage':'s3' if store else 'fs'}
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'count':len(checks),'evidence':str(output)}),flush=True)
    return output,result


def capacity():
    """Constructors may exceed the ordinary write journal's 256-file batch cap."""
    import subprocess, sys
    with IsolatedServer('constructor-capacity') as api:
        source=api.create_case('Capacity source')
        owner=api.call('GET','/auth/me')['id'];ws=api.call('GET','/workspaces')[0]['id']
        plan=read_plans(api,None,owner)[0];prefix=plan['binding']['prefix']
        for index in range(300):
            put_foreign(api,None,prefix,f'Ref/capacity/{index}.txt',f'FILE {index}'.encode())
        original=contents(api,None,prefix)
        original={path:data for path,data in original.items() if not path.startswith('.vibeflow/')}
        target_ws=api.call('POST','/workspaces',{'name':'Capacity targets'})['id']
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as bundle:
            for path,data in original.items():bundle.writestr(path,data)
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=120) as client:
            response=client.post(f'/workspaces/{target_ws}/cases:import',data={'name':'Capacity import'},
                                 files={'file':('source.zip',archive.getvalue(),'application/zip')})
            assert response.status_code==200,response.text[:500]
            imported=response.json()['id']
            copied=api.call('POST',f'/cases/{source["id"]}:copy',{'name':'Capacity copy','workspace_id':target_ws})['id']
            workspace=api.call('POST',f'/workspaces/{ws}:copy',{'name':'Capacity workspace','include_cases':True})['id']
            workspace_case=api.call('GET',f'/workspaces/{workspace}/cases')[0]['id']
            backup=api.call('GET',f'/workspaces/{ws}/backup',raw=True)
            response=client.post('/workspaces:restore',files={'file':('source.zip',backup,'application/zip')})
            assert response.status_code==200,response.text[:500]
            restored=api.call('GET',f'/workspaces/{response.json()["workspace_id"]}/cases')[0]['id']
        root=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])
        for identifier in (imported,copied,workspace_case,restored):
            destination=next((root/f'workspaces/{owner}/cases').rglob(str(identifier)))
            actual={p.relative_to(destination).as_posix():p.read_bytes() for p in (destination/'Ref/capacity').glob('*.txt')}
            assert actual=={path:data for path,data in original.items() if path.startswith('Ref/capacity/')}
        result=api.call('POST','/case-provisions:recover')
        assert result['results'] and all(row['status']=='committed' for row in result['results']),result
        # Scaled boundary proves the optional capture limit is independent of
        # the existing writer default; it does not claim an actual >1 GiB copy.
        code='''from pathlib import Path
from tempfile import TemporaryDirectory
from app.services import file_transactions as journal, case_provision as provision
with TemporaryDirectory() as folder:
 p=Path(folder)/'large';p.write_bytes(b'x'*32);journal.MAX_BYTES=16
 try:journal._capture(p)
 except ValueError:pass
 else:raise AssertionError('ordinary writer lost its size bound')
 assert journal._capture(p,limit=provision.MAX_FILE_BYTES)==provision.digest(b'x'*32)
 journal.MAX_BYTES=64
 assert journal._capture(p)['size']==32
'''
        subprocess.run([sys.executable,'-X','utf8','-B','-c',code],cwd=SERVER,env=api.env,check=True,timeout=20)
        output=Path(tempfile.mkdtemp(prefix='constructor-capacity-evidence-',dir=SERVER.parents[1]/'tmp'))
        evidence={'passed':True,'count':5,'checks':['import_300_files','case_copy_300_files',
                  'workspace_copy_300_files','backup_restore_300_files','scaled_optional_capture_limit_preserves_default'],
                  'api':str(api.root),'actual_large_file_above_1GiB_tested':False}
        (output/'checks.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')
        print(json.dumps({'passed':True,'count':5,'evidence':str(output)}),flush=True)


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--kinds',nargs='+',choices=KINDS);parser.add_argument('--modes',nargs='+')
    parser.add_argument('--capacity',action='store_true');args=parser.parse_args()
    if args.capacity:
        await asyncio.to_thread(capacity);return
    from contextlib import ExitStack
    with ExitStack() as stack:
        store=stack.enter_context(ObjectStore(executable=RUNTIME,volume_max=12)) if args.s3 else None
        if not args.postgres:
            await asyncio.to_thread(verify,store,None,args.kinds,args.modes);return
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_constructor_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            logs=Path(tempfile.mkdtemp(prefix='constructor-migration-',dir=SERVER.parents[1]/'tmp'))
            await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
            output,result=await asyncio.to_thread(verify,store,temporary,args.kinds,args.modes)
        finally:
            if created:
                await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',name)
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result['temporary_database_removed']=True
        (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
