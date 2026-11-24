"""Real OpenBao Transit, organization model credentials, and authorized DeepSeek."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from copy import copy
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url

from live_llm_acceptance import FIXTURE, source
from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer
from workflow_migration_smoke import engine_for


KEY_PREFIX = 'vf-j09'
BASE_IMAGE_ID = 'sha256:b22e9b67f9b16a0425187e0ae016f0efea521327f0900ad85ef921f6e88ccb06'


def docker(*arguments):
    proc = subprocess.run(['docker',*arguments],capture_output=True,text=True,timeout=45)
    if proc.returncode:
        raise RuntimeError(f'Isolated Docker {arguments[0]} failed: {proc.stderr.strip()[:1200]}')
    return (proc.stdout + (proc.stderr if arguments[0]=='logs' else '')).strip()


def free_port():
    with socket.socket() as holder:
        holder.bind(('127.0.0.1',0))
        return holder.getsockname()[1]


@contextmanager
def kms(root, executable, endpoint_port, directory, *, tls=False):
    root.mkdir(exist_ok=True)
    (root/directory).mkdir(exist_ok=True)
    cfg = root/f'{directory}.hcl'
    scheme='https' if tls else 'http'
    listener_tls = ('  tls_cert_file = "/fixture/kms-cert.pem"\n  tls_key_file = "/fixture/kms-key.pem"\n'
                    if tls else '  tls_disable = true\n')
    cfg.write_text(
        'disable_mlock = true\n'
        f'api_addr = "{scheme}://127.0.0.1:8200"\n'
        'cluster_addr = "https://127.0.0.1:8201"\n'
        'listener "tcp" {\n  address = "0.0.0.0:8200"\n'+listener_tls+'}\n'
        f'storage "raft" {{\n  path = "/fixture/{directory}"\n  node_id = "vibeflow-j09"\n  performance_multiplier = 1\n}}\n',
        encoding='utf8')
    name = 'vf-j09-kms-'+uuid4().hex[:12]
    assert docker('image','inspect',BASE_IMAGE_ID,'--format','{{.Id}}') == BASE_IMAGE_ID
    docker('run','--pull','never','--detach','--name',name,'--user','65534:65534',
           '--cap-drop','ALL','--security-opt','no-new-privileges',
           '-p',f'127.0.0.1:{endpoint_port}:8200',
           '--mount',f'type=bind,source={root},target=/fixture',
           '--mount',f'type=bind,source={executable},target=/usr/local/bin/bao,readonly',
           '--entrypoint','/usr/local/bin/bao',BASE_IMAGE_ID,'server','-config',f'/fixture/{cfg.name}')
    try:
        verify = ssl.create_default_context(cafile=str(root/'kms-cert.pem')) if tls else True
        with httpx.Client(base_url=f'{scheme}://127.0.0.1:{endpoint_port}',trust_env=False,
                          verify=verify,timeout=20) as client:
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                if docker('inspect',name,'--format','{{.State.Running}}')!='true':
                    raise RuntimeError('OpenBao exited before becoming ready')
                try:
                    ready = client.get('/v1/sys/seal-status',timeout=1)
                    if ready.status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(.15)
            else:
                raise TimeoutError('OpenBao did not become ready')
            yield client
    finally:
        try:
            (root/f'{directory}-bao.log').write_text(docker('logs',name),encoding='utf8')
        finally:
            if docker('inspect',name,'--format','{{.State.Running}}')=='true':
                docker('stop','--time','5',name)
            docker('rm',name)


def transit(client, method, path, body=None, token=None, status=200):
    headers = {'X-Vault-Token':token} if token else {}
    response = client.request(method,'/v1/'+path,json=body,headers=headers)
    assert response.status_code in ((status,) if isinstance(status,int) else status), (method,path,response.status_code)
    return response.json() if response.content else {}


def initialize(client):
    operator = transit(client,'PUT','sys/init',{'secret_shares':1,'secret_threshold':1})
    unseal(client,operator['keys_base64'][0])
    token = operator['root_token']
    transit(client,'POST','sys/mounts/transit',{'type':'transit'},token,status=204)
    policy = '\n'.join(
        f'path "transit/{operation}/{KEY_PREFIX}-org-*" {{ capabilities = ["update"] }}'
        for operation in ('encrypt','decrypt','rewrap'))
    transit(client,'PUT','sys/policies/acl/vf-provider',{'policy':policy},token,status=204)
    limited = transit(client,'POST','auth/token/create',{'policies':['vf-provider'],'ttl':'1h',
                      'no_default_policy':True},token)['auth']['client_token']
    return token,operator['keys_base64'][0],limited


def unseal(client, key):
    result = transit(client,'PUT','sys/unseal',{'key':key})
    assert not result['sealed']
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if client.get('/v1/sys/health',timeout=2).status_code==200:
            return
        time.sleep(.2)
    raise TimeoutError('Unsealed OpenBao did not become leader')


def api_fixture(api, port, token, dist=None, ca=None):
    token_path = api.root/'.kms-token'
    token_path.write_text(token,encoding='ascii')
    scheme='https' if ca else 'http'
    settings = {'VIBEFLOW_PROVIDER_KMS_ADDRESS':f'{scheme}://127.0.0.1:{port}',
        'VIBEFLOW_PROVIDER_KMS_KEY_PREFIX':KEY_PREFIX,'VIBEFLOW_PROVIDER_KMS_TOKEN_FILE':str(token_path)}
    if ca:
        settings['VIBEFLOW_PROVIDER_KMS_CA_FILE']=str(ca)
    code = 'import os\nos.environ.update('+repr(settings)+')\nlive_request_limit=8\n'+FIXTURE
    if dist is not None:
        code += '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory='+repr(str(dist))+',html=True))\n'
    (api.root/'kms_fixture.py').write_text(code,encoding='utf8')


async def profile_row(database, identifier, replace=None, origin=None):
    engine = engine_for(database)
    try:
        async with engine.begin() as db:
            if replace is not None:
                await db.execute(text('UPDATE provider_profiles SET key_ciphertext=:cipher WHERE id=:id'),
                                 {'id':identifier,'cipher':replace})
            if origin is not None:
                await db.execute(text('UPDATE provider_profiles SET base_url=:origin WHERE id=:id'),
                                 {'id':identifier,'origin':origin})
            return (await db.execute(text('SELECT org_id,key_ciphertext FROM provider_profiles WHERE id=:id'),
                                     {'id':identifier})).one()
    finally:
        await engine.dispose()


def sqlite_copy(source, target):
    with sqlite3.connect(str(source)) as origin, sqlite3.connect(str(target)) as output:
        origin.backup(output)


@contextmanager
def postgres_copy(database):
    if database is None:
        yield None
        return
    import asyncpg

    original=make_url(database)
    assert original.host in {'127.0.0.1','localhost','::1'}
    assert re.fullmatch(r'tmp_vf_termagent_[0-9a-f]{32}',original.database or '')
    name='tmp_vf_orgkmsrestore_'+uuid4().hex

    async def execute(operation):
        admin=await asyncpg.connect(host=original.host,port=original.port or 5432,user=original.username,
            password=original.password,database='postgres',timeout=10,command_timeout=40)
        try:
            await admin.execute(operation)
        finally:
            await admin.close()

    asyncio.run(execute(f'CREATE DATABASE "{name}" TEMPLATE "{original.database}"'))
    try:
        yield original.set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(execute(f'DROP DATABASE "{name}"'))


def actor(api, label):
    selected = copy(api)
    email = uuid4().hex+'@example.com'
    selected.token = api.call('POST','/auth/register',
        {'email':email,'password':'isolated-test-pass','name':label})['token']
    return selected,email


def translated(api, identifier, expect=200):
    value=api.call('POST','/llm/translate',{
        'text':'The flow separates near the trailing edge.','target':'zh',
        'provider_profile_id':identifier},expect=expect)
    if expect == 200:
        assert value['ok'] and any('\u3400'<=c<='\u9fff' for c in value['translation'])


EDGE = """
import json,re,sys
from pathlib import Path
from playwright.sync_api import expect,sync_playwright
cfg=json.load(sys.stdin)
out=Path(cfg['root'])
def open_panel(browser,token,width,height):
    context=browser.new_context(viewport={'width':width,'height':height},locale='zh-CN')
    page=context.new_page()
    page.add_init_script('localStorage.setItem("vf_token",'+json.dumps(token)+');')
    page.goto(cfg['address'],wait_until='domcontentloaded')
    if width>500:
        page.locator('.dv-tab:visible').filter(has_text=re.compile('^设置$')).click(timeout=25000)
        page.get_by_role('button',name='管理模型配置',exact=True).click()
    else:
        page.get_by_label('工作面板',exact=True).select_option('providers')
    return context,page,page.get_by_test_id('provider-workspace')
with sync_playwright() as play:
    browser=play.chromium.launch(channel='msedge',headless=True)
    admin,page,panel=open_panel(browser,cfg['admin'],1440,900)
    panel.get_by_role('button',name='新建配置').click()
    panel.get_by_label('配置归属').select_option(str(cfg['org']))
    panel.get_by_label('配置名称').fill('CAE 工程组织模型')
    panel.get_by_label('默认模型').fill(cfg['model'])
    panel.get_by_label('API 地址').fill(cfg['endpoint'])
    panel.get_by_label('API 密钥').fill(cfg['key'])
    with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/provider-profiles')) as saved:
        panel.get_by_role('button',name='保存配置').click()
    assert saved.value.status==201 and cfg['key'] not in saved.value.text()
    expect(panel.get_by_role('status')).to_have_text('模型配置已保存')
    expect(panel.get_by_label('API 密钥')).to_have_value('')
    identifier=saved.value.json()['id']
    panel.screenshot(path=out/'kms-admin-1440.png')
    admin.close()
    member,page,panel=open_panel(browser,cfg['member'],390,844)
    panel.get_by_label('选择配置').select_option(str(identifier))
    expect(panel.get_by_text('组织管理员管理此配置。')).to_be_visible()
    expect(panel.get_by_label('配置名称')).to_be_disabled()
    expect(panel.get_by_label('API 密钥')).to_have_count(0)
    layout=panel.evaluate('node=>[node.clientWidth,node.scrollWidth]')
    assert layout[1]<=layout[0]+2
    panel.screenshot(path=out/'kms-member-390.png')
    (out/'kms-browser.json').write_text(json.dumps({'passed':True,'width':layout,
        'key_cleared':True,'member_edit_disabled':True,'llm_calls':0},indent=2),encoding='utf8')
    member.close();browser.close()
"""


def verify(cfg, database, bao, browser_python, dist):
    tls=database is not None and browser_python is None
    api=IsolatedServer('live-j09-org-kms',app='kms_fixture:app',database_url=database,tls=tls)
    port=free_port()
    kms_root=api.root/'openbao'
    db_url=database or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix()
    report={'passed':False,'model':cfg['model'],'mock_llm_calls':0,'https_kms':tls,'checks':[]}
    print('J09_EVIDENCE',api.root,flush=True)
    ca=None
    if tls:
        kms_root.mkdir()
        ca=kms_root/'kms-cert.pem'
        shutil.copyfile(api.root/'fixture-cert.pem',ca)
        shutil.copyfile(api.root/'fixture-key.pem',kms_root/'kms-key.pem')
    restored=None
    try:
        with kms(kms_root,bao,port,'original',tls=tls) as service:
            operator,share,token=initialize(service)
            api_fixture(api,port,token,dist,ca=ca)
            with api:
                api.create_case('Organization key acceptance')
                owner_id=api.call('GET','/auth/me')['id']
                org=api.call('GET','/org/mine')['org']['id']
                key_name=f'{KEY_PREFIX}-org-{org}'
                transit(service,'POST',f'transit/keys/{key_name}',
                        {'type':'aes256-gcm96','derived':True},operator,status=(200,204))
                transit(service,'POST',f'transit/keys/{key_name}/rotate',{},token,status=403)
                member,email=actor(api,'Organization member')
                outsider,_=actor(api,'Unaffiliated account')
                member_id=member.call('GET','/auth/me')['id']
                api.call('POST',f'/org/{org}/members',{'email':email,'role':'member'},expect=201)
                member.call('POST',f'/org/{org}/invitation:accept',{})
                kwargs={'provider':'deepseek','name':'KMS crew','base_url':cfg['base_url'],
                        'default_model':cfg['model'],'api_key':cfg['key']}
                member.call('POST',f'/org/{org}/provider-profiles',kwargs,expect=403)
                value=api.call('POST',f'/org/{org}/provider-profiles',kwargs,expect=201)
                identifier=value['id']
                path=f'/provider-profiles/{identifier}'
                orig=asyncio.run(profile_row(db_url,identifier))
                assert orig.org_id==org and orig.key_ciphertext.startswith('vibeflow-kms-v1:')
                assert cfg['key'] not in orig.key_ciphertext and cfg['key'] not in json.dumps(value)
                sealed=lambda value: json.loads(value.split(':',1)[1])
                old=sealed(orig.key_ciphertext)
                assert old['wrapped_key'].startswith('vault:v1:')
                assert any(row['id']==identifier and not row['can_manage'] for row in member.call('GET','/provider-profiles'))
                assert not any(row['id']==identifier for row in outsider.call('GET','/provider-profiles'))
                translated(outsider,identifier,expect=404)
                member.call('PATCH',path,{'name':'Unauthorized'},expect=404)
                api.call('PATCH',path,{'base_url':'https://other.example/v1'},expect=422)
                report['checks'].append('Organization ownership, member usage, admin-only management and KMS least privilege')

                translated(member,identifier)
                workspace=member.call('GET','/workspaces')[0]
                case=member.call('POST',f"/workspaces/{workspace['id']}/cases",{'name':'Real KMS chat'})
                conv=member.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':identifier,
                    'model':cfg['model'],'permission_mode':'ask','title':'Organization KMS'})['id']
                marker='KMS_REAL_'+uuid4().hex[:12].upper()
                data=member.call('POST',f'/conversations/{conv}/messages',{
                    'content':f'Reply with {marker} only.','provider_profile_id':identifier,'model':cfg['model']},raw=True)
                events=[json.loads(row[6:]) for row in data.decode().splitlines() if row.startswith('data: {')]
                answer=''.join(row.get('text','') for row in events if row.get('type')=='text.delta')
                assert marker in answer
                report['checks'].append('Actual DeepSeek translation and streamed Agent reply using the organization credential')

                other=api.call('POST',f'/org/{org}/provider-profiles',kwargs | {'name':'Second profile'},expect=201)['id']
                other_row=asyncio.run(profile_row(db_url,other))
                asyncio.run(profile_row(db_url,identifier,other_row.key_ciphertext))
                translated(member,identifier,expect=503)
                asyncio.run(profile_row(db_url,identifier,orig.key_ciphertext,origin='https://untrusted.example/v1'))
                translated(member,identifier,expect=503)
                asyncio.run(profile_row(db_url,identifier,origin=cfg['base_url']))
                transit(service,'POST',f'transit/keys/{key_name}/rotate',{},operator,status=(200,204))
                assert api.call('POST',path+':rewrap',{})['rewrapped']
                assert not api.call('POST',path+':rewrap',{})['rewrapped']
                rotated=sealed(asyncio.run(profile_row(db_url,identifier)).key_ciphertext)
                assert rotated['wrapped_key'].startswith('vault:v2:') and old['ciphertext']==rotated['ciphertext']
                translated(member,identifier)
                report['checks'].append('Cross-profile ciphertext and modified endpoint rejected, real KEK rotation and idempotent rewrap verified')

                if browser_python:
                    context={'address':api.base,'root':str(api.root),'admin':api.token,'member':member.token,
                             'org':org,'model':cfg['model'],'endpoint':cfg['base_url'],'key':cfg['key']}
                    subprocess.run([str(browser_python),'-c',EDGE],input=json.dumps(context),text=True,
                                   check=True,timeout=100)
                    report['checks'].append('Edge administrator creation, masked credential and member read-only view')
                api.call('PATCH',f'/org/{org}/members/{member_id}',{'role':'admin'})
                member.call('PATCH',f'/org/{org}/members/{owner_id}',{'role':'disabled'})
                api.call('PATCH',path,{'name':'Creator revoked'},expect=404)
                assert member.call('PATCH',path,{'name':'Successor-admin KMS'})['name']=='Successor-admin KMS'
                member_email=member.call('GET','/auth/me')['email']
                report['checks'].append('A successor administrator manages the profile after the creator membership is disabled')

        with api:
            member.token=member.call('POST','/auth/login',
                {'email':member_email,'password':'isolated-test-pass'})['token']
            translated(member,identifier,expect=503)
        report['checks'].append('KMS outage returns 503 before contacting DeepSeek')
        shutil.copytree(kms_root/'original',kms_root/'restored')
        with postgres_copy(database) as snapshot:
            restored=IsolatedServer('live-j09-org-kms-restored',app='kms_fixture:app',database_url=snapshot)
            if database is None:
                sqlite_copy(api.root/'test.sqlite',restored.root/'test.sqlite')
            with kms(kms_root,bao,port,'restored',tls=tls) as service:
                unseal(service,share)
                selected=transit(service,'GET',f'transit/keys/{key_name}',token=operator)
                assert selected['data']['latest_version']==2
                api_fixture(restored,port,token,ca=ca)
                with restored:
                    restored.token=restored.call('POST','/auth/login',
                        {'email':member_email,'password':'isolated-test-pass'})['token']
                    assert not (restored.root/'.vault_key').exists()
                    translated(restored,identifier)
                    assert not (restored.root/'.vault_key').exists()
            report['restored_backend']='postgresql' if snapshot else 'sqlite'
            report['restored_api_evidence']=restored.root.name
        report['checks'].append('Fresh API and restored OpenBao storage unwrap version 2 without a local Fernet key')
        report['passed']=True
    finally:
        (kms_root/'kms-key.pem').unlink(missing_ok=True)
        (api.root/'fixture-key.pem').unlink(missing_ok=True)
        receipts=[]
        for selected in [api]+([restored] if restored else []):
            (selected.root/'.kms-token').unlink(missing_ok=True)
            (selected.root/'.vault_key').unlink(missing_ok=True)
            receipt=selected.root/'upstream.jsonl'
            if receipt.exists():
                receipts.extend(json.loads(row) for row in receipt.read_text(encoding='utf8').splitlines())
        report['upstream']=receipts
        report['real_deepseek_http_200']=sum(item.get('status')==200 for item in receipts)
        report['passed']=report['passed'] and report['real_deepseek_http_200']==4
        (api.root/'organization-kms.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    assert report['real_deepseek_http_200']==4


async def main(args):
    cfg=await source(args.source_profile_id)
    async with disposable_database(args.postgres) as database:
        await asyncio.to_thread(verify,cfg,database,args.bao.resolve(strict=True),
            args.browser_python,args.dist.resolve(strict=True) if args.dist else None)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--bao',type=Path,required=True)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--browser-python',type=Path)
    parser.add_argument('--dist',type=Path)
    args=parser.parse_args()
    if bool(args.browser_python)!=bool(args.dist):
        parser.error('--browser-python and --dist are used together')
    asyncio.run(main(args))
