"""Real skill inventory, node credit, personal quota and two-API contention."""
import argparse,ast,asyncio,hashlib,json,os,re,subprocess,sys,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.config import get_settings
from app.services import node_storage_budget as node,skill_store,storage_usage
from app.services.case_write import CaseWriteConflict
from testsupport import SERVER,IsolatedServer
from skill_archive_acceptance import API_FIXTURE,zipped


def configure(root,limit=2):
    os.environ.update(VIBEFLOW_STORAGE_BACKEND='fs',VIBEFLOW_STORAGE_FS_ROOT=str(root/'public'),
        VIBEFLOW_SKILLS_STORAGE_ROOT=str(root/'skills'),VIBEFLOW_CODEX_STATE_ROOT=str(root/'cli'),
        VIBEFLOW_CODEX_WSL_STATE_ROOT='',VIBEFLOW_NODE_STORAGE_LIMIT_MB=str(limit),VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0')
    get_settings.cache_clear();return get_settings()


def service_checks():
    root=Path(tempfile.mkdtemp(prefix='skill-storage-service-',dir=SERVER.parents[1]/'tmp'));settings=configure(root,1)
    checks=[];report={'passed':False,'kind':'actual_OS_files_and_node_claims','model_calls':0,'mock_llm_calls':0,'checks':checks}
    print('SKILL_STORAGE_SERVICE_EVIDENCE '+str(root),flush=True)
    def record(name,**fields):checks.append({'check':name,**fields})
    def put(relative,size):
        p=skill_store.storage_root()/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'A'*size);return p
    def measured(user):return storage_usage._measure(get_settings(),user,time.monotonic()+10,None).bytes
    def claims():return list((root/'public/.node-storage-budget').glob('*.json'))
    def full(operation):
        try:operation()
        except CaseWriteConflict as error:assert error.code=='node_storage_full';return error
        raise AssertionError('Actual full node accepted more skill storage')
    try:
        put('users/1/old/SKILL.md',600*1024);put('orgs/99/shared/SKILL.md',600*1024)
        failure=full(node.observe);assert failure.storage_observation['bytes']>=1200*1024
        record('actual_node_counts_user_and_org_skills',actual_skill_bytes=1200*1024,observed_bytes=failure.storage_observation['bytes'])
        assert measured(1)==600*1024 and measured(2)==0
        record('personal_inventory_isolated_from_other_users_and_orgs',actual_user1_bytes=measured(1),actual_user2_bytes=measured(2),org_user_payer_inferred=False)
        def reserve_more():
            with node.reserve(256*1024,target_root=skill_store.storage_root()):pass
        full(reserve_more);assert not claims();record('actual_extra_256KiB_refused_after_inventory')
        full(lambda:skill_store.write_skill('user',1,None,'refused','Actual','Actual data'))
        assert not (skill_store.storage_root()/'users/1/refused').exists()
        full(lambda:skill_store.install_skill_zip(zipped([('refused/SKILL.md',b'Actual data')]),'org',1,99))
        assert not (skill_store.storage_root()/'orgs/99/refused').exists() and not claims()
        record('user_and_org_writes_refused_before_directory_allocation')
        configure(root,3)
        put('users/1/.install-retained/payload.bin',4096);put('users/1/.old.bak-retained/payload.bin',2048);put('users/2/other/SKILL.md',1024)
        assert measured(1)==600*1024+6144 and measured(2)==1024
        record('retained_install_and_backup_bytes_remain_charged',actual_user1_bytes=measured(1))
        observed=[];original_write=Path.write_bytes;original_disk=node.shutil.disk_usage;disks=[]
        def writing(path,data):
            if path.is_relative_to(skill_store.storage_root()) and path.name=='SKILL.md':
                active=[json.loads(p.read_text(encoding='utf8')) for p in claims()]
                assert active and sum(row['bytes'] for row in active)>=len(data)
                assert any(row.get('target_root')==str(skill_store.storage_root()) for row in active)
                observed.append({'bytes':len(data),'reserved':sum(row['bytes'] for row in active)})
            return original_write(path,data)
        def disk(path):disks.append(str(Path(path)));return original_disk(path)
        with patch.object(Path,'write_bytes',writing),patch.object(node.shutil,'disk_usage',disk):
            made=skill_store.write_skill('user',3,None,'new','Actual','Actual reserved content')
        assert made.is_dir() and observed and not claims() and str(skill_store.storage_root()) in disks
        record('actual_write_holds_node_credit_and_checks_target_volume',observations=observed,actual_target_volume_query=True)
        attachment=made/'attachment.bin'
        attachment.write_bytes(b'K'*(224*1024))
        observed.clear()
        with patch.object(Path,'write_bytes',writing):
            skill_store.write_skill('user',3,None,'new','Actual','Updated body',overwrite=True)
        assert attachment.read_bytes()==b'K'*(224*1024) and not claims()
        assert len(observed)==1 and observed[0]['reserved']>=attachment.stat().st_size+observed[0]['bytes']
        record('actual_update_reserves_retained_attachment_copy',observation=observed[0])
        original_read=__import__('zipfile').ZipExtFile.read;extractions=[]
        def reading(self,*args,**kwargs):
            value=original_read(self,*args,**kwargs)
            if value:
                active=[json.loads(p.read_text(encoding='utf8')) for p in claims()];assert active
                extractions.append({'actual_read':len(value),'reserved':sum(row['bytes'] for row in active)})
            return value
        with patch.object(__import__('zipfile').ZipExtFile,'read',reading):
            installed=skill_store.install_skill_zip(zipped([('installed/SKILL.md',b'Actual install bytes')]),'user',3,None)
        assert installed.is_dir() and extractions and not claims()
        record('actual_extraction_holds_credit_until_cleanup',actual_reads=extractions)
        original=settings=configure(root,3)
        with patch.dict(os.environ,VIBEFLOW_SKILLS_STORAGE_ROOT=str(root/'public/skills')):
            get_settings.cache_clear()
            try:skill_store.storage_root()
            except ValueError:pass
            else:raise AssertionError('Overlapping skill and public roots accepted')
        get_settings.cache_clear();record('overlapping_managed_roots_refused')
        # A real abrupt child leaves its claim and partial files. Process birth
        # proof retires only the claim; retained bytes remain in inventory.
        child="from pathlib import Path\nimport os\nfrom app.services import node_storage_budget as n,skill_store as s\nwith n.reserve(128*1024,target_root=s.storage_root()):\n p=s.storage_root()/'users/9/.install-crashed/payload.bin'\n p.parent.mkdir(parents=True,exist_ok=True)\n p.write_bytes(b'R'*(96*1024))\n os._exit(17)\n"
        process=subprocess.run([sys.executable,'-X','utf8','-B','-c',child],cwd=SERVER,capture_output=True,timeout=20)
        assert process.returncode==17 and claims()
        before_claim=json.loads(claims()[0].read_text(encoding='utf8'))
        observation=node.observe();assert not claims() and measured(9)==96*1024
        record('actual_dead_owner_claim_retired_retained_bytes_counted',actual_exit_code=17,original_claim_owner=before_claim['owner'],retained_bytes=measured(9),node_bytes=observation['bytes'])
        separate=root/'contention';configure(separate,1)
        child="import json\nfrom app.services import node_storage_budget as n,skill_store as s\nfrom app.services.case_write import CaseWriteConflict\ntry:\n with n.reserve(600*1024,target_root=s.storage_root()):print(json.dumps({'admitted':True}))\nexcept CaseWriteConflict as e:print(json.dumps({'admitted':False,'code':e.code}))\n"
        with node.reserve(600*1024,target_root=skill_store.storage_root()):
            result=subprocess.run([sys.executable,'-X','utf8','-B','-c',child],cwd=SERVER,capture_output=True,text=True,encoding='utf8',timeout=20)
            assert result.returncode==0;denied=json.loads(result.stdout);assert denied=={'admitted':False,'code':'node_storage_full'}
        result=subprocess.run([sys.executable,'-X','utf8','-B','-c',child],cwd=SERVER,capture_output=True,text=True,encoding='utf8',timeout=20)
        assert result.returncode==0 and json.loads(result.stdout)=={'admitted':True}
        record('actual_cross_process_node_credit_exclusion_and_release',first=denied,after_release=True)
        assert not list((separate/'public/.node-storage-budget').glob('*.json'));report['passed']=True
    finally:
        report['source_hashes']={name:hashlib.sha256((SERVER/name).read_bytes()).hexdigest() for name in ['app/services/skill_store.py','app/services/skill_capacity.py','app/services/node_storage_budget.py','app/services/storage_usage.py']}
        (root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('SKILL_STORAGE_SERVICE_OK '+str(root)+' checks='+str(len(checks)),flush=True);return root


EXTRA=r'''
from app.models import Plan
from app.config import get_settings
from app.services import storage_usage,node_storage_budget
@app.post('/api/v1/testing/skill-limits')
async def skill_limits(body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 plan=await db.scalar(select(Plan).where(Plan.code=='free'));values=json.loads(plan.quotas)
 if 'storage_mb' in body:values['storage_mb']=body['storage_mb'];plan.quotas=json.dumps(values);await db.commit()
 if 'node_mb' in body:get_settings().node_storage_limit_mb=body['node_mb']
 return {'storage_mb':values['storage_mb'],'node_mb':get_settings().node_storage_limit_mb}
@app.get('/api/v1/testing/skill-usage')
async def skill_usage(user=Depends(get_current_user)):
 value=await storage_usage.measure_user(user.id)
 return {'bytes':value.bytes,'entries':value.entries,'backend':value.backend,'complete':value.complete,
  'skill_root':str(skill_store.storage_root())}
'''
FIXTURE=API_FIXTURE+EXTRA


def api_checks(database_url=None,store=None):
    import httpx
    from app.services import process_identity_native as identity
    api=IsolatedServer('skill-storage-api',app='skill_storage_fixture:app',database_url=database_url)
    settings={**(store.settings() if store else {}),
              'VIBEFLOW_NODE_STORAGE_LIMIT_MB':'10','VIBEFLOW_NODE_STORAGE_MIN_FREE_MB':'0'}
    prefix='import os\nos.environ.update('+repr(settings)+')\n'
    (api.root/'skill_storage_fixture.py').write_text(prefix+FIXTURE,encoding='utf8')
    checks=[];report={'passed':False,'kind':'actual_HTTP_OS_SQL_storage','database':'postgresql' if database_url else 'sqlite',
        'public_storage':'s3' if store else 'fs','model_calls':0,'mock_llm_calls':0,'checks':checks}
    print('SKILL_STORAGE_API_EVIDENCE '+str(api.root),flush=True)
    peer=IsolatedServer('skill-storage-peer',app='skill_storage_peer:app',database_url=database_url)
    try:
        with api:
            api.create_case('Actual skill storage admission')
            role=api.call('GET','/testing/skill-state');report['actual_API_birth']=identity.observe(role['pid'])
            shared={k:v for k,v in api.env.items() if k.startswith('VIBEFLOW_')};shared.update(settings)
            (peer.root/'skill_storage_peer.py').write_text('import os\nos.environ.update('+repr(shared)+')\n'+FIXTURE,encoding='utf8')
            peer.token=api.token
            owner_token=api.token
            other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Actual other skill owner'})
            api.call('POST','/testing/skill-limits',{'storage_mb':1})
            before=api.call('GET','/testing/skill-usage');assert before['complete']
            body={'name':'capacity-first','description':'Actual','body':'A'*(600*1024)}
            with peer,httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=30) as client:
                peer_role=peer.call('GET','/testing/skill-state');report['peer_birth']=identity.observe(peer_role['pid'])
                assert peer.call('GET','/testing/skill-usage')['skill_root']==before['skill_root']
                def submit(server,name):return server.call('POST','/skills',dict(body,name=name),expect=(200,429,409))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(submit,server,name) for server,name in [(api,'capacity-first'),(peer,'capacity-second')]]
                    responses=[f.result() for f in futures]
                successful=[i for i,r in enumerate(responses) if r.get('ok')];assert len(successful)==1
                winner=['capacity-first','capacity-second'][successful[0]];loser=['capacity-first','capacity-second'][1-successful[0]]
                api.call('POST','/skills',dict(body,name=loser),expect=429)
                used=api.call('GET','/testing/skill-usage');assert used['bytes']>=before['bytes']+600*1024
                checks.append({'check':'actual_two_API_personal_quota_serialization','winner':winner,'loser':loser,'used_bytes':used['bytes'],'initial_responses':responses})
                assert api.call('GET','/skills/'+winner)['body']==body['body']
                api.call('GET','/skills/'+loser,expect=404)
                api.call('POST','/testing/skill-limits',{'storage_mb':0})
                api.call('POST','/skills',dict(body,name='zero-write',body='actual'),expect=429)
                package=zipped([('zero-install/SKILL.md',b'Actual install')])
                response=client.post('/skills/install',files={'file':('actual.zip',package,'application/zip')});assert response.status_code==429
                api.call('GET','/skills/zero-write',expect=404);api.call('GET','/skills/zero-install',expect=404)
                checks.append({'check':'zero_personal_quota_refuses_create_and_install_no_publication'})
                api.call('POST','/testing/skill-limits',{'storage_mb':10})
                org=dict(body,name='org-capacity',scope='org');api.call('POST','/skills',org)
                personal=api.call('GET','/testing/skill-usage');assert personal['bytes']==used['bytes']
                checks.append({'check':'org_skill_charged_to_node_without_guessing_user_payer','personal_bytes_unchanged':True})
                api.call('POST','/testing/skill-limits',{'node_mb':1})
                response=client.post('/skills',json=dict(body,name='node-full',body='Actual node data'))
                report['node_create_response']={'status':response.status_code,'body':response.json()}
                assert response.status_code==409 and response.json()['detail']['code']=='node_storage_full'
                response=client.post('/skills/install',data={'scope':'org'},files={'file':('actual.zip',package,'application/zip')})
                report['node_install_response']={'status':response.status_code,'body':response.json()}
                assert response.status_code==409 and response.json()['detail']['code']=='node_storage_full'
                api.call('GET','/skills/node-full',expect=404);api.call('GET','/skills/zero-install',expect=404)
                checks.append({'check':'actual_user_and_org_directories_enforce_node_limit'})
                api.call('POST','/testing/skill-limits',{'node_mb':10})
                api.token=other['token'];other_usage=api.call('GET','/testing/skill-usage');assert other_usage['bytes']==0
                api.call('POST','/skills',dict(body,name='other-skill',body='Other actual bytes'))
                api.token=owner_token;assert api.call('GET','/testing/skill-usage')['bytes']==used['bytes']
                checks.append({'check':'other_user_skill_files_do_not_change_personal_quota'})
                state=api.call('GET','/testing/skill-state');assert not state['stages'] and not state['backups']
                assert not list((api.root/'files/.node-storage-budget').glob('*.json'))
                checks.append({'check':'all_skill_write_credits_and_stages_drained'})
            report['functional_passed']=True
    finally:
        for key in ['actual_API_birth','peer_birth']:
            if key not in report:continue
            until=time.monotonic()+10
            while True:
                probe=identity.probe(report[key])
                if probe['state']=='exited' or time.monotonic()>=until:break
                time.sleep(.05)
            report[key+'_exit_probe']=probe
        report['passed']=bool(report.get('functional_passed') and all(report.get(k+'_exit_probe',{}).get('state')=='exited' for k in ['actual_API_birth','peer_birth']))
        report['peer_root']=str(peer.root)
        report['source_hashes']={n:hashlib.sha256((SERVER/n).read_bytes()).hexdigest() for n in ['app/services/skill_store.py','app/services/skill_capacity.py','app/services/node_storage_budget.py','app/services/storage_usage.py','app/routers/skills.py']}
        (api.root/'capacity-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    assert report['passed'];print('SKILL_STORAGE_API_OK '+str(api.root)+' checks='+str(len(checks)),flush=True);return api.root


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--service-only',action='store_true');parser.add_argument('--api-only',action='store_true');parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    if not args.api_only:service_checks()
    if args.service_only:return
    def run(url=None):
        if not args.s3:return api_checks(url)
        from s3testsupport import ObjectStore
        store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',authenticated=True);receipt={'passed':False}
        print('SKILL_STORAGE_S3 '+str(store.root),flush=True)
        try:
            with store:
                result=api_checks(url,store);receipt.update(passed=True,root=str(result));return result
        finally:
            receipt['store_stopped']=store.proc is not None and store.proc.poll() is not None
            (store.root/'skill-storage-result.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf8')
    if not args.postgres:await asyncio.to_thread(run);return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    u=make_url(Settings(_env_file=SERVER/'.env').database_url);assert u.drivername=='postgresql+asyncpg' and u.host in {'localhost','127.0.0.1','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='skill-storage-postgres-',dir=SERVER.parents[1]/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False,'model_calls':0,'mock_llm_calls':0}
    print('SKILL_STORAGE_POSTGRES '+str(root),flush=True)
    admin=await asyncpg.connect(host=u.host,port=u.port or 5432,user=u.username,password=u.password,database='postgres',timeout=10);created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=u.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head');report['root']=str(await asyncio.to_thread(run,url));report['passed']=True
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await admin.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    asyncio.run(main())
