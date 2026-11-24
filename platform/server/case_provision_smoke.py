"""Real API creation crashes, proof-based recovery, prefix reuse and foreign bytes."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import asyncio
import uuid

import httpx
from testsupport import IsolatedServer, SERVER

FIXTURE = '''
import time
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.services import case_provision as provision
ROOT=Path(__file__).parent
original_prepare,original_write,original_commit=provision.prepare,provision.write_file,AsyncSession.commit
def mode():return (ROOT/'mode').read_text() if (ROOT/'mode').exists() else ''
def hold():
 (ROOT/'ready').write_text('ready')
 while not (ROOT/'release').exists():time.sleep(.02)
def preparing(*args):
 result=original_prepare(*args)
 if mode()=='after_plan':hold()
 return result
def writing(*args):
 result=original_write(*args)
 if mode() in {'after_file','external','extra'}:hold()
 return result
async def committing(self):
 owned=self.info.get('case_provisions',{})
 if owned and mode() in {'before_commit','corrupt'}:hold()
 if owned and mode()=='rollback':raise RuntimeError('fixture failure before commit')
 result=await original_commit(self)
 if owned and mode() in {'after_commit','committed_external'}:hold()
 if owned and mode()=='lost_commit':raise RuntimeError('fixture lost commit acknowledgement')
 return result
provision.prepare,provision.write_file,AsyncSession.commit=preparing,writing,committing
'''


def peer(api):
    server=IsolatedServer('case-provision-peer',app='provision_peer:app',database_url=api.database_url)
    overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT',
        'VIBEFLOW_JWT_SECRET','VIBEFLOW_INDEX_ROOT')}
    (server.root/'provision_peer.py').write_text('import os\nos.environ.update('+repr(overrides)+')\nfrom app.main import app\n',encoding='utf8')
    server.token=api.token
    return server


def crash(api,request):
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(request)
        deadline=time.monotonic()+30
        while not (api.root/'ready').exists():
            if api.proc.poll() is not None or time.monotonic()>deadline:
                raise AssertionError('Creation crash boundary not reached: '+str(api.root))
            time.sleep(.03)
        api.proc.terminate();api.proc.wait(timeout=10)
        try:future.result(timeout=10)
        except (httpx.TransportError,OSError):pass
        else:raise AssertionError('Killed creation returned success')


def run(modes=None,database_url=None):
    checks=[]
    for mode in modes or ['after_plan','after_file','before_commit','after_commit','external','extra',
                         'committed_external','corrupt','rollback','lost_commit','normal']:
        api=IsolatedServer('case-provision-'+mode,app='provision_fixture:app',database_url=database_url)
        (api.root/'provision_fixture.py').write_text(FIXTURE,encoding='utf8')
        with api:
            api.create_case('Initial '+mode)
            assert api.call('POST','/case-provisions:recover')['recovery_required']==0
            ws=api.call('GET','/workspaces')[0]['id']
            root=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])
            endpoint=f'/api/v1/workspaces/{ws}/cases'
            if mode=='normal':
                before=api.call('GET',api.prefix+'/tree')
                assert len(before['entries'])==13
                assert api.call('POST','/case-provisions:recover')['results']==[]
                checks.append({'mode':mode,'api_root':str(api.root),'passed':True});continue
            (api.root/'mode').write_text(mode)
            with httpx.Client(base_url=api.base,headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=40) as client:
                request=lambda:client.post(endpoint,json={'name':'Interrupted '+mode})
                if mode in {'rollback','lost_commit'}:
                    assert request().status_code==500
                else:crash(api,request)
            pending=list(root.glob('workspaces/*/.case-provisions/pending/*.json'))
            assert len(pending)==1,pending
            plan_path=pending[0];plan=json.loads(plan_path.read_text(encoding='utf8'))
            target=root/plan['binding']['prefix']
            files=[p for p in target.rglob('*') if p.is_file()] if target.exists() else []
            if mode in {'external','committed_external'}:
                assert files;files[0].write_bytes(b'EXTERNAL EDIT')
            if mode=='extra':(target/'foreign.txt').write_bytes(b'FOREIGN')
            if mode=='corrupt':
                altered=dict(plan);altered['binding']={**plan['binding'],'prefix':'workspaces/999/cases/999'}
                plan_path.write_text(json.dumps(altered),encoding='utf8')
            with peer(api) as recovered:
                outcome=recovered.call('POST','/case-provisions:recover')
                row=outcome['results'][0]
                if mode in {'external','extra','corrupt'}:
                    assert row['status']=='recovery_required',outcome
                    assert plan_path.exists()
                    if mode=='external':assert files[0].read_bytes()==b'EXTERNAL EDIT'
                    if mode=='extra':assert (target/'foreign.txt').read_bytes()==b'FOREIGN'
                elif mode in {'after_commit','committed_external','lost_commit'}:
                    assert row['status']=='committed',outcome
                    assert not plan_path.exists() and files
                    if mode=='committed_external':assert files[0].read_bytes()==b'EXTERNAL EDIT'
                    else:assert all(p.exists() for p in files)
                else:
                    assert row['status']=='rolled_back',outcome
                    assert not plan_path.exists() and not any(p.is_file() for p in target.rglob('*'))
                if mode not in {'after_commit','committed_external','lost_commit'}:
                    new=recovered.call('POST',f'/workspaces/{ws}/cases',{'name':'Replacement '+mode})
                    new_pending=[p for p in root.glob('workspaces/*/.case-provisions/pending/*.json') if p!=plan_path]
                    assert len(new_pending)==1,new_pending
                    new_plan=json.loads(new_pending[0].read_text(encoding='utf8'))
                    assert new_plan['binding']['prefix']!=plan['binding']['prefix']
                    assert new_plan['case_id']==new['id']
                    assert recovered.call('GET',f'/cases/{new["id"]}/caseDict')['dict_revision']==0
                checks.append({'mode':mode,'api_root':str(api.root),'peer_root':str(recovered.root),'passed':True})
    evidence=Path(tempfile.mkdtemp(prefix='case-provision-evidence-',dir=SERVER.parents[1]/'tmp'))
    result={'passed':True,'count':len(checks),'checks':checks}
    (evidence/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'count':len(checks),'evidence':str(evidence)}),flush=True)
    return evidence,result


async def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--modes',nargs='+');parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    if not args.postgres:
        await asyncio.to_thread(run,args.modes);return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    url=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
    name='tmp_vf_provision_'+uuid.uuid4().hex
    admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
    created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        temporary=url.set(database=name).render_as_string(hide_password=False)
        logs=Path(tempfile.mkdtemp(prefix='provision-migration-',dir=SERVER.parents[1]/'tmp'))
        await asyncio.to_thread(alembic,temporary,logs,'upgrade','head')
        output,result=await asyncio.to_thread(run,args.modes,temporary)
    finally:
        if created:
            await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1',name)
            await admin.execute('DROP DATABASE "'+name+'"')
        await admin.close()
    result['temporary_database_removed']=True
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
