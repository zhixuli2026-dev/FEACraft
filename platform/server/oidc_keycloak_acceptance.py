"""Exercise two real Keycloak issuers, PostgreSQL/SQLite and an Edge browser."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

import httpx

from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer


@contextmanager
def keycloak(runtime, java_home, evidence):
    with socket.socket() as holder:
        holder.bind(('127.0.0.1',0))
        port=holder.getsockname()[1]
    temporary=evidence/'java-tmp'
    temporary.mkdir()
    admin_id,admin_secret='acceptance-admin',uuid4().hex
    env=dict(os.environ,JAVA_HOME=str(java_home),JAVA_OPTS_APPEND=f'-Djava.io.tmpdir={temporary}',
             JAVA_OPTS_KC_HEAP='-Xms128m -Xmx576m',KC_BOOTSTRAP_ADMIN_CLIENT_ID=admin_id,
             KC_BOOTSTRAP_ADMIN_CLIENT_SECRET=admin_secret)
    origin=f'http://127.0.0.1:{port}'
    command=['cmd.exe','/d','/c',str(runtime/'bin/kc.bat'),'start-dev','--db=dev-mem',
             '--http-host=127.0.0.1',f'--http-port={port}',f'--hostname={origin}']
    with (evidence/'keycloak.log').open('w',encoding='utf8') as log:
        server=subprocess.Popen(command,cwd=runtime,env=env,stdout=log,stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        outcome={'pid':server.pid,'port':port,'stopped':False}
        try:
            with httpx.Client(base_url=origin,trust_env=False,timeout=15) as client:
                deadline=time.monotonic()+100
                while time.monotonic()<deadline and server.poll() is None:
                    try:
                        if client.get('/realms/master/.well-known/openid-configuration',timeout=2).status_code==200:
                            break
                    except httpx.RequestError:
                        pass
                    time.sleep(.4)
                else:
                    raise RuntimeError('Actual Keycloak did not start; see keycloak.log')
                authorization=client.post('/realms/master/protocol/openid-connect/token',
                    data={'grant_type':'client_credentials','client_id':admin_id,'client_secret':admin_secret})
                assert authorization.status_code==200,authorization.status_code
                access=authorization.json()['access_token']
                client.headers['Authorization']='Bearer '+access
                yield client,origin
        finally:
            if server.poll() is None:
                subprocess.run(['taskkill.exe','/pid',str(server.pid),'/t','/f'],capture_output=True,check=True,timeout=20)
            server.wait(timeout=15)
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=2):
                    pass
            except OSError:
                outcome['stopped']=True
            (evidence/'keycloak-process.json').write_text(json.dumps(outcome,indent=2),encoding='utf8')
            assert outcome['stopped']


def issuer(client,oidc_origin,site,tag,display,email,password,public):
    realm='vf-'+tag+'-'+uuid4().hex[:10]
    secret='' if public else uuid4().hex
    configuration={'client_id':'vibeflow-web','issuer':oidc_origin+'/realms/'+realm,
        'name':display,'token_auth_method':'none' if public else 'client_secret_basic',
        'client_secret':secret,'rp_logout':not public}
    selected_client={'clientId':'vibeflow-web','enabled':True,'protocol':'openid-connect',
        'publicClient':public,'standardFlowEnabled':True,'directAccessGrantsEnabled':False,
        'redirectUris':[site+f'/api/v1/auth/oidc/{tag}/callback'],
        'attributes':{'pkce.code.challenge.method':'S256','post.logout.redirect.uris':site+'/'}}
    if secret:
        selected_client['secret']=secret
    data={'realm':realm,'enabled':True,'sslRequired':'none','clients':[selected_client],
          'users':[{'username':'engineer','enabled':True,'email':email,'emailVerified':True,
              'firstName':'CAE','lastName':'Engineer',
              'credentials':[{'type':'password','value':password,'temporary':False}]}]}
    response=client.post('/admin/realms',json=data)
    assert response.status_code==201,(tag,response.status_code)
    result=client.get(f'/realms/{realm}/.well-known/openid-configuration')
    assert result.status_code==200 and result.json()['issuer']==configuration['issuer']
    return configuration


def configure(api,primary,providers,dist=None,peer=None):
    path=primary.root/'private-providers.json'
    if api is primary:
        path.write_text(json.dumps(providers,ensure_ascii=False),encoding='utf8')
    settings={'VIBEFLOW_JWT_SECRET':primary.shared_secret,'VIBEFLOW_OIDC_PUBLIC_ORIGIN':primary.base,
        'VIBEFLOW_OIDC_PROVIDER_FILE':str(path),'VIBEFLOW_STORAGE_FS_ROOT':str(primary.root/'files')}
    if primary.database_url is None:
        settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(primary.root/'test.sqlite').as_posix()
    code='import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n'
    if peer is not None:
        code += 'PEER='+repr(peer.base)+'\nPEER_CA='+repr(str(peer.root/'fixture-cert.pem'))+'\n'
        code += """
import httpx,ssl
from starlette.responses import Response
from fastapi import Request
peer_tls=ssl.create_default_context(cafile=PEER_CA)

@app.middleware("http")
async def forward_oauth_callback(request: Request,call_next):
    path=request.url.path
    if request.method!='GET' or not (path.startswith('/api/v1/auth/oidc/') and path.endswith('/callback')):
        return await call_next(request)
    async with httpx.AsyncClient(verify=peer_tls,trust_env=False,timeout=30,follow_redirects=False) as client:
        upstream=await client.get(PEER+path+'?'+request.url.query,
            headers={'Cookie':request.headers.get('cookie','')})
    response=Response(upstream.content,status_code=upstream.status_code)
    response.raw_headers=upstream.headers.raw
    response.headers['x-vf-test-peer']='true'
    return response
"""
    if dist is not None:
        code+='from fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory='+repr(str(dist))+',html=True))\n'
    (api.root/'oidc_fixture.py').write_text(code,encoding='utf8')


def browser_binding(primary,peer):
    with httpx.Client(verify=primary.tls_context,trust_env=False,timeout=12,follow_redirects=False) as client:
        start=client.post(primary.base+'/api/v1/auth/oidc/alpha/start',
                          json={'intent':'login'},headers={'Origin':primary.base})
        assert start.status_code==200,start.status_code
        state=parse_qs(urlsplit(start.json()['authorization_url']).query)['state'][0]
        cookie=start.headers['set-cookie'].split(';',1)[0]
    url=peer.base+'/api/v1/auth/oidc/alpha/callback'
    params={'state':state,'error':'access_denied'}
    with httpx.Client(verify=peer.tls_context,trust_env=False,timeout=12,follow_redirects=False) as client:
        answer=client.get(url,params=params)
        assert answer.status_code==303 and answer.headers['location'].endswith('oidc=state_invalid')
        accepted=client.get(url,params=params,headers={'Cookie':cookie})
        assert accepted.status_code==303 and accepted.headers['location'].endswith('oidc=access_denied')
        replay=client.get(url,params=params,headers={'Cookie':cookie})
        assert replay.status_code==303 and replay.headers['location'].endswith('oidc=state_invalid')


EDGE = """
import json,re,sys
from pathlib import Path
from urllib.parse import parse_qs,parse_qsl,urlencode,urlsplit,urlunsplit
from playwright.sync_api import expect,sync_playwright
cfg=json.load(sys.stdin)
site,peer=cfg['site'],cfg['peer']
out=Path(cfg['out'])
callbacks=[]
def observe(page):
    def callback(response):
        if '/api/v1/auth/oidc/' in urlsplit(response.url).path and '/callback' in urlsplit(response.url).path:
            assert response.status==303 and response.headers.get('x-vf-test-peer')=='true'
            callbacks.append(parse_qs(urlsplit(response.headers['location']).query)['oidc'][0])
    page.on('response',callback)
def authenticate(page):
    page.wait_for_url(re.compile(r'/(protocol/openid-connect/auth|login-actions/authenticate)|oidc='))
    if page.locator('#username').is_visible():
        page.locator('#username').fill('engineer')
        page.locator('#password').fill(cfg['password'])
        page.locator('#kc-login').click()
    page.wait_for_url(re.compile(r'^'+re.escape(site)+r'/?(?:\\?|$)'))
def settings(page,mobile=False):
    if mobile:
        page.get_by_label('工作面板',exact=True).select_option('settings')
    else:
        page.locator('.dv-tab:visible').filter(has_text=re.compile('^设置$')).click()
    account=page.get_by_role('region',name='账户与登录')
    expect(account).to_be_visible()
    return account
def cookie(context):
    matched=[item for item in context.cookies() if item['name']=='__Host-vf_session']
    assert len(matched)==1 and matched[0]['httpOnly'] and matched[0]['secure'] and matched[0]['sameSite']=='Lax'
    return matched[0]['name']+'='+matched[0]['value']
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    desktop=browser.new_context(viewport={'width':1440,'height':900},ignore_https_errors=True)
    page=desktop.new_page()
    observe(page)
    page.goto(site,wait_until='domcontentloaded')
    expect(page.get_by_role('button',name='使用工程身份登录')).to_be_visible()
    expect(page.get_by_role('button',name='使用协作身份登录')).to_be_visible()
    page.screenshot(path=out/'oidc-login-1440.png')
    page.get_by_role('button',name='使用工程身份登录').click()
    authenticate(page)
    expect(page.get_by_role('alert')).to_contain_text('此邮箱已有账户')
    assert callbacks[-1]=='account_link_required'

    page.get_by_label('邮箱',exact=True).fill(cfg['local_email'])
    page.get_by_label('密码',exact=True).fill(cfg['password'])
    page.get_by_role('button',name='登录',exact=True).click()
    account=settings(page)
    account.get_by_role('button',name='关联工程身份').click()
    authenticate(page)
    assert callbacks[-1]=='complete'
    assert page.request.get(peer+'/api/v1/auth/me').json()['id']==cfg['local_user']
    assert page.evaluate('localStorage.getItem("vf_token")') is None
    old_cookie=cookie(desktop)
    account=settings(page)
    expect(account.get_by_text('工程身份 · '+cfg['local_email'])).to_be_visible()
    account.screenshot(path=out/'oidc-linked-1440.png')
    account.get_by_role('button',name='退出登录').click()
    page.wait_for_url(re.compile(r'/protocol/openid-connect/logout'))
    page.locator('#kc-logout').click()
    expect(page.get_by_role('button',name='使用工程身份登录')).to_be_visible()
    assert page.request.get(peer+'/api/v1/auth/me',headers={'Cookie':old_cookie}).status==401
    page.get_by_label('邮箱',exact=True).fill(cfg['local_email'])
    page.get_by_label('密码',exact=True).fill(cfg['password'])
    page.get_by_role('button',name='登录',exact=True).click()
    account=settings(page)
    account.get_by_role('button',name='解除关联',exact=True).click()
    account.get_by_role('button',name='确认解除关联').click()
    expect(account.get_by_role('status')).to_contain_text('已解除关联')
    desktop.close()

    mobile=browser.new_context(viewport={'width':390,'height':844},ignore_https_errors=True)
    page=mobile.new_page()
    observe(page)
    pattern=re.compile(re.escape(cfg['beta_issuer'])+r'/protocol/openid-connect/auth\\?')
    def wrong_nonce(route):
        url=urlsplit(route.request.url)
        query=[(name,'invalid-for-this-transaction' if name=='nonce' else value)
            for name,value in parse_qsl(url.query)]
        route.continue_(url=urlunsplit(url._replace(query=urlencode(query))))
    page.route(pattern,wrong_nonce)
    page.goto(site,wait_until='domcontentloaded')
    page.get_by_role('button',name='使用协作身份登录').click()
    authenticate(page)
    expect(page.get_by_role('alert')).to_contain_text('组织身份验证失败')
    assert callbacks[-1]=='identity_invalid'
    page.unroute(pattern,wrong_nonce)
    page.get_by_role('button',name='使用协作身份登录').click()
    authenticate(page)
    assert callbacks[-1]=='complete'
    actual=page.request.get(peer+'/api/v1/auth/me').json()
    assert actual['id']!=cfg['local_user'] and actual['email']==cfg['beta_email']
    cookie(mobile)
    account=settings(page,mobile=True)
    expect(account.get_by_text('协作身份 · '+cfg['beta_email'])).to_be_visible()
    sizes=account.evaluate('node=>[node.clientWidth,node.scrollWidth]')
    assert sizes[1]<=sizes[0]+3
    account.screenshot(path=out/'oidc-account-390.png')
    connection=page.request.get(site+'/api/v1/auth/oidc/connections').json()[0]
    deletion=page.request.delete(peer+'/api/v1/auth/oidc/connections/'+connection['id'],headers={'Origin':site})
    assert deletion.status==409
    (out/'oidc-browser.json').write_text(json.dumps({'passed':True,'real_keycloak_issuers':2,
        'callback_outcomes':callbacks,'secondary_received_callbacks':True,'local_and_oidc_logout':True,
        'unlink_verified':True,'last_method_protected':True,'mobile_width':sizes,'llm_calls':0},indent=2),encoding='utf8')
    mobile.close();browser.close()
"""


def verify(args,database):
    primary=IsolatedServer('ff-j10-oidc-primary',app='oidc_fixture:app',database_url=database,tls=True)
    peer=IsolatedServer('ff-j10-oidc-peer',app='oidc_fixture:app',database_url=database,tls=True,access_log=True)
    primary.shared_secret=uuid4().hex
    report={'passed':False,'real_keycloak_issuers':2,'real_llm_calls':0,'mock_llm_calls':0}
    print('J10_EVIDENCE',primary.root,peer.root,flush=True)
    try:
        with keycloak(args.keycloak.resolve(strict=True),args.java.resolve(strict=True),primary.root) as (client,base):
            password='VF-'+uuid4().hex
            local_email='engineering-'+uuid4().hex[:8]+'@example.com'
            beta_email='collaborator-'+uuid4().hex[:8]+'@example.com'
            providers={'alpha':issuer(client,base,primary.base,'alpha','工程身份',local_email,password,False),
                       'beta':issuer(client,base,primary.base,'beta','协作身份',beta_email,password,True)}
            configure(primary,primary,providers,args.dist.resolve(strict=True),peer=peer)
            configure(peer,primary,providers)
            with primary,peer:
                local=primary.call('POST','/auth/register',{'email':local_email,'password':password,'name':'Engineering member'})
                peer.call('POST','/auth/oidc/alpha/start',{'intent':'login'},expect=403)
                assert [item['id'] for item in peer.call('GET','/auth/oidc/providers')]==['alpha','beta']
                browser_binding(primary,peer)
                report['browser_binding_and_replay']=True
                inputs={'site':primary.base,'peer':peer.base,'out':str(primary.root),'local_email':local_email,
                        'local_user':local['user']['id'],'password':password,'beta_email':beta_email,
                        'beta_issuer':providers['beta']['issuer']}
                subprocess.run([str(args.browser_python),'-c',EDGE],input=json.dumps(inputs),text=True,check=True,timeout=160)
        report['passed']=True
    finally:
        for api in (primary,peer):
            (api.root/'fixture-key.pem').unlink(missing_ok=True)
        (primary.root/'private-providers.json').unlink(missing_ok=True)
        (primary.root/'oidc-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


async def main(args):
    async with disposable_database(args.postgres) as database:
        await asyncio.to_thread(verify,args,database)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keycloak',type=Path,required=True)
    parser.add_argument('--java',type=Path,required=True)
    parser.add_argument('--browser-python',type=Path,required=True)
    parser.add_argument('--dist',type=Path,required=True)
    parser.add_argument('--postgres',action='store_true')
    asyncio.run(main(parser.parse_args()))
