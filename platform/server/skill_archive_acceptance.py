"""Actual bounded skill files/ZIPs and isolated API compatibility; no LLM path."""
import argparse,ast,asyncio,hashlib,io,json,os,re,struct,sys,tempfile,threading,time,uuid,warnings,zipfile
from pathlib import Path
from unittest.mock import patch

from app.services import skill_store as skills
from testsupport import SERVER,IsolatedServer

API_FIXTURE=r'''
import asyncio,json,os,threading,zipfile
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app.services import skill_store
skill_store.DATA_ROOT=Path(__file__).parent/'isolated-skills'
from app.main import app
from app.db import get_db,SessionLocal
from app.deps import get_current_user
from app.models import Membership,User
from app.routers import skills as skill_routes
entered=threading.Event();release=threading.Event();selected=None
original_open=zipfile.ZipFile.open
original_read=zipfile.ZipExtFile.read
def archive_open(self,*args,**kwargs):
 value=original_open(self,*args,**kwargs)
 if selected=='package' and len(args)>1 and args[1]=='w' and not entered.is_set():
  entered.set();assert release.wait(15),'Actual compression boundary was not released'
 return value
def archive_read(self,*args,**kwargs):
 value=original_read(self,*args,**kwargs)
 if selected=='install' and value and not entered.is_set():
  entered.set();assert release.wait(15),'Actual extraction boundary was not released'
 return value
zipfile.ZipFile.open=archive_open;zipfile.ZipExtFile.read=archive_read
@app.get('/api/v1/testing/skill-state')
async def skill_state(user=Depends(get_current_user)):
 return {'pid':os.getpid(),'stages':[p.name for p in skill_store.DATA_ROOT.glob('**/.install-*')],
  'backups':[p.name for p in skill_store.DATA_ROOT.glob('**/.*.bak-*')],
  'journals':[p.name for p in skill_store.DATA_ROOT.glob('**/.skill-install.json')]}
@app.post('/api/v1/testing/skill-share')
async def skill_share(body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 membership=await db.scalar(select(Membership).where(Membership.user_id==user.id,Membership.role=='admin'))
 assert membership is not None and await db.get(User,body['user_id']) is not None
 db.add(Membership(org_id=membership.org_id,user_id=body['user_id'],role='member'));await db.commit()
 return {'org_id':membership.org_id}
@app.post('/api/v1/testing/skill-member-only')
async def skill_member_only(user=Depends(get_current_user),db=Depends(get_db)):
 rows=list(await db.scalars(select(Membership).where(Membership.user_id==user.id)))
 assert rows
 for row in rows:row.role='member'
 await db.commit();return {'actual_membership_rows_changed':len(rows)}
@app.post('/api/v1/testing/skill-cancel/{kind}')
async def skill_cancel(kind:str,user=Depends(get_current_user)):
 global selected
 assert kind in {'package','install'};selected=kind;entered.clear();release.clear()
 async def operation():
  async with SessionLocal() as db:
   if kind=='package':return await skill_routes.package('api-skill','user',user,db)
   from starlette.datastructures import UploadFile
   import io
   uploaded=UploadFile(io.BytesIO((Path(__file__).parent/'actual-package.zip').read_bytes()),filename='actual.zip')
   try:return await skill_routes.install(uploaded,'user',True,user,db)
   finally:await uploaded.close()
 task=asyncio.create_task(operation())
 try:
  async with asyncio.timeout(15):
   while not entered.is_set():
    if task.done():await task
    await asyncio.sleep(.01)
  task.cancel();await asyncio.sleep(.05);assert not task.done(),'Cancellation released a live archive worker'
  release.set()
  try:await task
  except asyncio.CancelledError:pass
  else:raise AssertionError('Cancellation was swallowed')
  return {'cancelled':True,'actual_archive_IO_drained':True}
 finally:
  release.set();selected=None
  if not task.done():
   task.cancel()
   try:await task
   except asyncio.CancelledError:pass
'''

INTERRUPT = r'''
selected_exit = Path(__file__).parent/'stop'
replace = os.replace
def crash_on_publication(source,destination):
 selected = Path(source)
 if (Path(destination).name=='recover-skill' and selected.parent.name=='payload'
     and (selected.parent.parent/'previous').is_dir() and selected_exit.exists()):
  if selected_exit.read_text()=='after':
   replace(source,destination)
  os._exit(79)
 return replace(source,destination)
os.replace=crash_on_publication
'''


def zipped(entries,compression=zipfile.ZIP_DEFLATED):
    output=io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',UserWarning)
        with zipfile.ZipFile(output,'w',compression) as archive:
            for name,body in entries:archive.writestr(name,body)
    return output.getvalue()


def service_checks():
    root=Path(tempfile.mkdtemp(prefix='skill-archive-service-',dir=SERVER.parents[1]/'tmp'))
    os.environ.update(VIBEFLOW_STORAGE_FS_ROOT=str(root/'files'),VIBEFLOW_SKILLS_STORAGE_ROOT=str(root/'skills'),
        VIBEFLOW_CODEX_STATE_ROOT=str(root/'cli'),VIBEFLOW_CODEX_WSL_STATE_ROOT='')
    from app.config import get_settings
    get_settings.cache_clear()
    skills.DATA_ROOT=root/'skills';checks=[];report={'passed':False,'kind':'actual_OS_ZIP','model_calls':0,'mock_llm_calls':0,'checks':checks}
    print('SKILL_ARCHIVE_SERVICE_EVIDENCE '+str(root),flush=True)
    def record(name,**fields):checks.append({'check':name,**fields})
    def refused(operation):
        try:operation()
        except ValueError as error:return str(error)
        raise AssertionError('Actual archive operation unexpectedly succeeded')
    def target(name,user=1):
        path=skills.DATA_ROOT/'users'/str(user)/name;path.mkdir(parents=True)
        (path/'SKILL.md').write_bytes(b'---\nname: '+name.encode()+b'\ndescription: Actual archive input\n---\n\nReal files, no model.\n')
        return path
    def tree(path):return {p.relative_to(path).as_posix():p.read_bytes() for p in path.rglob('*') if p.is_file()}
    def no_stages():assert not list(skills.DATA_ROOT.glob('**/.install-*')) and not list(skills.DATA_ROOT.glob('**/.*.bak-*'))
    try:
        first=skills.write_skill('user',1,None,'normal','Actual Unicode 描述','Actual body',
            interface={'display_name':'Actual Display','short_description':'Actual short','default_prompt':'Use $normal'})
        (first/'assets').mkdir();(first/'assets/payload.bin').write_bytes(os.urandom(65536))
        original=tree(first);package=skills.package_skill('user',1,None,'normal')
        installed=skills.install_skill_zip(package,'user',2,None);assert tree(installed)==original
        installed_org=skills.install_skill_zip(package,'org',2,17);assert tree(installed_org)==original
        no_stages();record('actual_user_and_org_roundtrip',files=len(original),zip_bytes=len(package))
        try:skills.install_skill_zip(package,'user',2,None)
        except FileExistsError:pass
        else:raise AssertionError('Duplicate skill was overwritten without authorization')
        assert tree(installed)==original;record('duplicate_preserves_original')
        skills.write_skill('user',1,None,'normal','Actual replacement','New real body',overwrite=True)
        new=tree(first);assert new['assets/payload.bin']==original['assets/payload.bin']
        skills.install_skill_zip(skills.package_skill('user',1,None,'normal'),'user',2,None,True)
        assert tree(installed)==new;no_stages();record('actual_overwrite_retains_source_assets_and_replaces_target')
        replace = os.replace
        interrupted = []

        def fail_publication(source, destination):
            if Path(destination)==installed and Path(source).parent.name=='payload':
                assert not installed.exists() and (Path(source).parent.parent/'previous').is_dir()
                interrupted.append(True)
                raise OSError('Injected second rename failure')
            return replace(source,destination)

        with patch.object(os,'replace',fail_publication):
            try:
                skills.install_skill_zip(package,'user',2,None,True)
            except OSError as error:
                assert str(error)=='Injected second rename failure'
            else:
                raise AssertionError('Failed skill publication was reported as successful')
        assert interrupted==[True] and tree(installed)==new
        no_stages();record('failed_publication_restores_actual_backup')
        boundary=target('boundary');(boundary/'SKILL.md').write_bytes(b'S' * skills.MAX_SKILL_BYTES)
        reads=[];original_read=zipfile.ZipExtFile.read
        def read(self,n=-1):
            value=original_read(self,n);reads.append({'requested':n,'actual':len(value)});return value
        with patch.object(zipfile.ZipExtFile,'read',read):
            installed_boundary=skills.install_skill_zip(skills.package_skill('user',1,None,'boundary'),'user',4,None)
        assert (installed_boundary/'SKILL.md').read_bytes()==b'S'*skills.MAX_SKILL_BYTES
        assert reads and all(0<n['requested']<=skills.READ_CHUNK_BYTES and n['actual']<=skills.READ_CHUNK_BYTES for n in reads)
        record('exact_1MiB_actual_streamed_roundtrip',maximum_read_request=max(row['requested'] for row in reads))
        many=target('hundred')
        for i in range(99):(many/f'asset-{i:03}.txt').write_bytes(b'actual')
        packed=skills.package_skill('user',1,None,'hundred');skills.install_skill_zip(packed,'user',5,None)
        with zipfile.ZipFile(io.BytesIO(packed)) as archive:assert len(archive.infolist())==100
        record('exact_100_file_roundtrip')
        (many/'one-too-many.txt').write_bytes(b'actual')
        record('export_101_files_refused',detail=refused(lambda:skills.package_skill('user',1,None,'hundred')))
        oversized=target('aggregate');(oversized/'a.bin').write_bytes(os.urandom(600*1024));(oversized/'b.bin').write_bytes(os.urandom(600*1024))
        record('export_aggregate_over_1MiB_refused',detail=refused(lambda:skills.package_skill('user',1,None,'aggregate')))
        for name in ['..','../normal','normal:stream']:
            refused(lambda name=name:skills.package_skill('user',1,None,name))
        record('invalid_export_names_refused')
        malformed=[
            ('expanded_4MiB_entry',zipped([('oversized/SKILL.md',b'X'*(4*1024*1024))])),
            ('aggregate_header_over_limit',zipped([('oversized/SKILL.md',b'X'*(600*1024)),('oversized/b.bin',b'Y'*(600*1024))])),
            ('101_headers',zipped([('too-many/SKILL.md',b'body'),*[(f'too-many/{i}.txt',b'') for i in range(100)]])),
            ('traversal',zipped([('../bad/SKILL.md',b'body')])),
            ('absolute',zipped([('/bad/SKILL.md',b'body')])),
            ('duplicate',zipped([('bad/SKILL.md',b'a'),('bad/SKILL.md',b'b')])),
            ('case_alias',zipped([('bad/SKILL.md',b'a'),('bad/skill.md',b'b')])),
            ('directory_case_alias',zipped([('bad/SKILL.md',b'a'),('bad/Assets/a',b'b'),('bad/assets/b',b'c')])),
            ('file_directory_conflict',zipped([('bad/SKILL.md',b'a'),('bad/assets',b'b'),('bad/assets/file',b'c')])),
            ('alternate_stream',zipped([('bad/SKILL.md',b'a'),('bad/data:stream',b'b')])),
            ('reserved_device',zipped([('bad/SKILL.md',b'a'),('bad/CON',b'b')])),
            ('missing_skill',zipped([('bad/file.txt',b'body')])),
            ('multiple_skills',zipped([('a/SKILL.md',b'a'),('b/SKILL.md',b'b')])),
        ]
        for index,(name,data) in enumerate(malformed,10):
            reads.clear()
            with patch.object(zipfile.ZipExtFile,'read',read):detail=refused(lambda:skills.install_skill_zip(data,'user',index,None))
            assert not reads and not (skills.DATA_ROOT/'users'/str(index)).exists();no_stages()
            record('preflight_'+name,body_reads=0,detail=detail)
        link=zipfile.ZipInfo('bad/SKILL.md');link.create_system=3;link.external_attr=(0o120777<<16)
        record('archive_symlink_metadata_refused',detail=refused(lambda:skills.install_skill_zip(zipped([(link,b'elsewhere')]),'user',30,None)))
        bad=zipped([('normal/SKILL.md',b'Actual corrupt content')],zipfile.ZIP_STORED)
        with zipfile.ZipFile(io.BytesIO(bad)) as archive:
            info=archive.infolist()[0];offset=info.header_offset+30+len(info.filename.encode())+len(info.extra)
        bad=bytearray(bad);bad[offset]^=1;before=tree(installed)
        record('actual_bad_CRC_keeps_original',detail=refused(lambda:skills.install_skill_zip(bytes(bad),'user',2,None,True)))
        assert tree(installed)==before;no_stages()
        bad=bytearray(zipped([('normal/SKILL.md',b'Actual damaged deflate stream')]))
        with zipfile.ZipFile(io.BytesIO(bad)) as archive:
            info=archive.infolist()[0];offset=info.header_offset+30+len(info.filename.encode())+len(info.extra)
        bad[offset]=0x07
        record('actual_bad_deflate_keeps_original',detail=refused(lambda:skills.install_skill_zip(bytes(bad),'user',2,None,True)))
        assert tree(installed)==before;no_stages()
        record('invalid_ZIP_normalized',detail=refused(lambda:skills.install_skill_zip(b'actual invalid zip','user',2,None,True)))
        assert tree(installed)==before
        huge_body='X'*skills.MAX_SKILL_BYTES
        record('create_checks_whole_skill_before_write',detail=refused(lambda:skills.write_skill('user',31,None,'too-large','description',huge_body)))
        assert not (skills.DATA_ROOT/'users/31').exists()
        old=tree(first)
        record('oversized_overwrite_preserves_original',detail=refused(lambda:skills.write_skill('user',1,None,'normal','description',huge_body,overwrite=True)))
        assert tree(first)==old
        hard=target('hardlink');outside=root/'actual-outside.bin';outside.write_bytes(b'actual bytes');os.link(outside,hard/'asset.bin')
        record('actual_hardlink_export_refused',detail=refused(lambda:skills.package_skill('user',1,None,'hardlink')))
        inventory=target('scan-bound')
        for i in range(6):(inventory/f'empty-{i}').mkdir()
        with patch.object(skills,'MAX_SCAN_ENTRIES',5):
            detail=refused(lambda:skills.package_skill('user',1,None,'scan-bound'))
        record('actual_directory_inventory_bound',configured_limit=5,detail=detail)
        with patch.object(skills,'MAX_ZIP_BYTES',128):detail=refused(lambda:skills.package_skill('user',1,None,'normal'))
        record('actual_compression_output_bound',configured_limit=128,detail=detail)
        changed=target('changed');original_open=zipfile.ZipFile.open;mutated=False
        def opening(self,*args,**kwargs):
            nonlocal mutated
            result=original_open(self,*args,**kwargs)
            if not mutated and len(args)>1 and args[1]=='w':
                mutated=True
                with (changed/'SKILL.md').open('ab') as stream:stream.write(b'actual concurrent growth')
            return result
        with patch.object(zipfile.ZipFile,'open',opening):detail=refused(lambda:skills.package_skill('user',1,None,'changed'))
        assert mutated;record('actual_source_growth_refused',detail=detail)
        no_stages();report['passed']=True
    finally:
        report['production_source_sha256']=hashlib.sha256((SERVER/'app/services/skill_store.py').read_bytes()).hexdigest()
        (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('SKILL_ARCHIVE_SERVICE_OK '+str(root)+' checks='+str(len(checks)),flush=True)
    return root


def api_checks(database_url=None):
    import httpx
    from app.services import process_identity_native as identity
    api=IsolatedServer('skill-archive-api',app='skill_archive_fixture:app',database_url=database_url)
    ast.parse(API_FIXTURE);(api.root/'skill_archive_fixture.py').write_text(API_FIXTURE,encoding='utf8')
    checks=[];report={'passed':False,'kind':'actual_isolated_HTTP_OS_SQL','database':'postgresql' if database_url else 'sqlite',
        'model_calls':0,'mock_llm_calls':0,'checks':checks}
    print('SKILL_ARCHIVE_API_EVIDENCE '+str(api.root),flush=True)
    def record(name,**fields):checks.append({'check':name,**fields})
    try:
        with api:
            api.create_case('Actual bounded skills API')
            owner=api.call('GET','/auth/me');owner_id=owner['id'];owner_token=api.token
            second=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Actual isolated skill consumer'})
            other_token=second['token'];other_id=second['user']['id']
            state=api.call('GET','/testing/skill-state');birth=identity.observe(state['pid'])
            report['actual_api_process']=birth
            content={'name':'api-skill','description':'Actual API skill','body':'Actual original body',
                'display_name':'Actual display','short_description':'Actual short','default_prompt':'Use $api-skill'}
            made=api.call('POST','/skills',content);assert made['ok']
            actual=api.call('GET','/skills/api-skill');assert actual['body']==content['body'] and actual['interface']['display_name']==content['display_name']
            api.token=other_token;api.call('GET','/skills/api-skill',expect=404)
            api.token=owner_token;api.call('POST','/skills',content,expect=409)
            record('actual_create_metadata_ownership_and_duplicate')
            packed=api.call('POST','/skills/api-skill/package',raw=True);(api.root/'actual-package.zip').write_bytes(packed)
            with zipfile.ZipFile(io.BytesIO(packed)) as archive:
                assert set(archive.namelist())=={'api-skill/SKILL.md','api-skill/agents/openai.yaml'}
            record('actual_package_HTTP_bytes_and_metadata',bytes=len(packed))
            with httpx.Client(base_url=api.base+'/api/v1',timeout=30,trust_env=False) as client:
                def installing(data,token=other_token,scope='user',overwrite=False,status=200):
                    response=client.post('/skills/install',headers={'Authorization':'Bearer '+token},
                        data={'scope':scope,'overwrite':'true' if overwrite else 'false'},files={'file':('actual.zip',data,'application/zip')})
                    assert response.status_code==status,(response.status_code,response.text[:500]);return response.json()
                installed=installing(packed);assert installed['name']=='api-skill'
                api.token=other_token;assert api.call('GET','/skills/api-skill')['body']==content['body']
                installing(packed,status=409);record('actual_install_roundtrip_and_duplicate')
                api.token=owner_token
                changed=dict(content,body='Actual replacement body',overwrite=True)
                api.call('POST','/skills',changed);new=api.call('POST','/skills/api-skill/package',raw=True)
                installing(new,overwrite=True)
                api.token=other_token;assert api.call('GET','/skills/api-skill')['body']==changed['body']
                record('actual_HTTP_overwrite_roundtrip')
                invalid=[b'actual invalid archive',zipped([('large/SKILL.md',b'X'*(4*1024*1024))]),
                    zipped([('bad/SKILL.md',b'a'),('bad/skill.md',b'b')]),zipped([('../bad/SKILL.md',b'a')])]
                for data in invalid:installing(data,overwrite=True,status=400)
                assert api.call('GET','/skills/api-skill')['body']==changed['body']
                record('invalid_HTTP_archives_400_preserve_existing',cases=len(invalid))
                installing(packed,scope='invalid',status=400)
                api.token=owner_token;shared=api.call('POST','/testing/skill-share',{'user_id':other_id})
                org=dict(content,name='org-skill',scope='org');api.call('POST','/skills',org)
                api.token=other_token;assert api.call('GET','/skills/org-skill')['body']==org['body']
                api.call('POST','/skills/org-skill/package?scope=org',expect=404)
                installing(packed,scope='org')
                assert not (api.root/'isolated-skills/orgs'/str(shared['org_id'])/'api-skill').exists()
                api.call('POST','/skills',dict(org,name='second-org-skill',body='Another organization'))
                assert {'org-skill','second-org-skill'}<={v['name'] for v in api.call('GET','/skills')['skills']}
                own=dict(org,scope='user',description='Personal description',body='Personal override')
                created=api.call('POST','/skills',own)
                assert api.call('GET','/skills/org-skill')['body']==own['body']
                definition=Path(created['path'])/'SKILL.md'
                for invalid in (b'Bad header',b'\xff\xfe'):
                    definition.write_bytes(invalid)
                    assert api.call('GET','/skills/org-skill')['body']==org['body']
                    listed=next(item for item in api.call('GET','/skills')['skills'] if item['name']=='org-skill')
                    assert listed['description']==org['description']
                definition.unlink()
                assert api.call('GET','/skills/org-skill')['body']==org['body']
                record('multi_org_directory_and_user_override_fallback')
                demoted=api.call('POST','/testing/skill-member-only',{})
                assert demoted['actual_membership_rows_changed']>=2
                installing(packed,scope='org',status=404)
                api.call('POST','/skills/org-skill/package?scope=org',expect=404)
                record('actual_org_visibility_and_admin_write_export_install_policy',org_id=shared['org_id'],
                    own_personal_org_install_allowed=True,actual_SQL_member_role_refused=True)
                api.token=owner_token
                api.call('POST','/skills',dict(content,name='oversized-api',body='X'*skills.MAX_SKILL_BYTES),expect=400)
                api.call('GET','/skills/oversized-api',expect=404)
                record('oversized_HTTP_create_not_published')
                for mode in ['package','install']:
                    cancelled=api.call('POST','/testing/skill-cancel/'+mode,{})
                    assert cancelled['cancelled'] and cancelled['actual_archive_IO_drained']
                    state=api.call('GET','/testing/skill-state')
                    assert not state['stages'] and not state['backups'] and not state['journals']
                    record('actual_'+mode+'_thread_cancellation_drains',trigger='Explicit async cancellation at actual archive I/O, not an HTTP disconnect claim')
                # The already-started install completed atomically before the
                # cancellation surfaced; its bytes are the original package.
                assert api.call('GET','/skills/api-skill')['body']==content['body']
                record('cancelled_install_has_complete_outcome_no_staging')
            report['functional_checks_passed']=True
    finally:
        report['api_exited']=api.proc is not None and api.proc.poll() is not None
        if 'birth' in locals():
            deadline=time.monotonic()+10
            while True:
                report['actual_api_exit_probe']=identity.probe(birth)
                if report['actual_api_exit_probe']['state']=='exited' or time.monotonic()>=deadline:break
                time.sleep(.05)
        report['passed']=bool(report.get('functional_checks_passed') and report['api_exited']
            and report.get('actual_api_exit_probe',{}).get('state')=='exited')
        report['source_hashes']={name:hashlib.sha256((SERVER/name).read_bytes()).hexdigest() for name in ['app/services/skill_store.py','app/routers/skills.py','app/services/async_ops.py']}
        (api.root/'result-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    assert report['api_exited'] and report['actual_api_exit_probe']['state']=='exited'
    print('SKILL_ARCHIVE_API_OK '+str(api.root)+' checks='+str(len(checks)),flush=True)
    return api.root


def crash_checks(database_url=None):
    import httpx
    from app.services import process_identity_native as identity
    result = []
    cases = (('before','user','install','old contents'), ('after','org','install','new contents'),
             ('before','user','update','old contents'))
    for window, scope, operation, recovered in cases:
        first = IsolatedServer('skill-install-crash',app='skill_archive_fixture:app',database_url=database_url)
        (first.root/'skill_archive_fixture.py').write_text(API_FIXTURE+INTERRUPT,encoding='utf8')

        def contents(body):
            return ('---\nname: recover-skill\ndescription: Crash check\n---\n\n'+body+'\n').encode()

        def install(api, body, overwrite, status=200):
            package=zipped([('recover-skill/SKILL.md',contents(body)),
                            ('recover-skill/attachment.bin',body.encode())])
            with httpx.Client(base_url=api.base+'/api/v1',timeout=25,trust_env=False) as client:
                response=client.post('/skills/install',headers={'Authorization':'Bearer '+api.token},
                    data={'scope':scope,'overwrite':str(overwrite).lower()},
                    files={'file':('actual.zip',package,'application/zip')})
            assert response.status_code==status, (response.status_code,response.text[:300])

        with first:
            first.create_case('Actual skill install interruption')
            install(first,'old contents',False)
            prior=first.call('GET','/skills/recover-skill')
            target=Path(prior['path']).parent
            birth=identity.observe(first.call('GET','/testing/skill-state')['pid'])
            (first.root/'stop').write_text(window)
            try:
                if operation=='install':
                    install(first,'new contents',True)
                else:
                    with httpx.Client(base_url=first.base+'/api/v1',timeout=25,trust_env=False) as client:
                        client.post('/skills',headers={'Authorization':'Bearer '+first.token},json={
                            'name':'recover-skill','scope':scope,'description':'Updated skill',
                            'body':'new contents','overwrite':True})
            except httpx.TransportError:
                pass
            else:
                raise AssertionError('Skill publisher did not exit')
            deadline=time.monotonic()+10
            while identity.probe(birth)['state']!='exited' and time.monotonic()<deadline:
                time.sleep(.05)
            assert identity.probe(birth)['state']=='exited'
            journal=target.parent/'.skill-install.json'
            stage,=target.parent.glob('.install-*')
            assert journal.is_file() and (stage/'previous'/'SKILL.md').read_bytes()==contents('old contents')
            assert target.exists()==(window=='after')
            env={k:first.env[k] for k in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET',
                                         'VIBEFLOW_SKILLS_STORAGE_ROOT','VIBEFLOW_STORAGE_FS_ROOT')}

        cold=IsolatedServer('skill-install-cold',app='skill_archive_fixture:app',database_url=database_url)
        prefix='import os\nos.environ.update('+repr(env)+')\n'
        (cold.root/'skill_archive_fixture.py').write_text(prefix+API_FIXTURE,encoding='utf8')
        with cold:
            cold.token=first.token
            if window=='after':
                archive=cold.call('POST','/skills/recover-skill/package?scope='+scope,raw=True)
                with zipfile.ZipFile(io.BytesIO(archive)) as installed:
                    assert installed.read('recover-skill/SKILL.md')==contents(recovered)
            assert cold.call('GET','/skills/recover-skill')['body']==recovered
            assert 'recover-skill' in {s['name'] for s in cold.call('GET','/skills')['skills']}
            assert (target/'attachment.bin').read_bytes()==recovered.encode()
            assert not journal.exists() and not stage.exists()
            install(cold,'retry contents',False,status=409)
            install(cold,'retry contents',True)
            assert cold.call('GET','/skills/recover-skill')['body']=='retry contents'
            assert not journal.exists() and not list(target.parent.glob('.install-*'))

            # Old releases left a single generated backup beside a missing target.
            legacy=target.with_name('.recover-skill.bak-1795240200000000000')
            os.replace(target,legacy)
            assert cold.call('GET','/skills/recover-skill')['body']=='retry contents'
            assert target.is_dir() and not legacy.exists()
        result.append({'window':window,'scope':scope,'operation':operation,'restored':recovered,'retry_installed':True,
                       'legacy_restored':True,'api_exited':cold.proc.poll() is not None,
                       'origin':str(first.root),'cold':str(cold.root)})
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--api-only',action='store_true');parser.add_argument('--service-only',action='store_true');parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args();assert not (args.api_only and args.service_only)
    if not args.api_only:service_checks()
    if args.service_only:return
    if not args.postgres:
        root=await asyncio.to_thread(api_checks)
        report={'checks':await asyncio.to_thread(crash_checks),'passed':True,'mock_llm_calls':0}
        (root/'crash-recovery.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='skill-archive-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False,'model_calls':0,'mock_llm_calls':0}
    print('SKILL_ARCHIVE_POSTGRES '+str(root),flush=True)
    admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,user=configured.username,password=configured.password,database='postgres',timeout=10);created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=configured.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        result_root=await asyncio.to_thread(api_checks,url)
        result={'checks':await asyncio.to_thread(crash_checks,url),'passed':True,'mock_llm_calls':0}
        (result_root/'crash-recovery.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        report['root']=str(result_root);report['passed']=True
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
