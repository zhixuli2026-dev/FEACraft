"""Exercise signed client releases through real Docker nginx and Edge."""
from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time
from uuid import uuid4

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import client_update as updates
from testsupport import SERVER


def wait_for(check, description, seconds=100):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        if check():
            return
        time.sleep(.2)
    raise TimeoutError(description)


def worker():
    exchange = Path('/source/tmp')
    feed = exchange/'feed'
    store = Path('/opt/vibeflow-client')
    key = Ed25519PrivateKey.generate()
    expires = int(time.time())+86400
    stable = updates.publish(exchange/'stable-build',feed,key=key,channel='stable',version='1.0.0',sequence=1,expires_at=expires)
    beta = updates.publish(Path('/source/dist'),feed,key=key,channel='beta',version='1.1.0-beta.1',sequence=2,expires_at=expires)

    class Feed(SimpleHTTPRequestHandler):
        fault = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args,directory=str(feed),**kwargs)

        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path!='/packages/'+beta.archive_sha256+'.zip' or not self.fault:
                return super().do_GET()
            self.send_response(200)
            self.send_header('Content-Length',str(beta.archive_bytes))
            self.end_headers()
            with (feed/'packages'/(beta.archive_sha256+'.zip')).open('rb') as source:
                block = source.read(65536)
                if self.fault=='cut':
                    self.wfile.write(block)
                    self.close_connection = True
                    return
                self.wfile.write(bytes([block[0]^1])+block[1:])
                shutil.copyfileobj(source,self.wfile,updates.CHUNK)

    publisher = ThreadingHTTPServer(('127.0.0.1',0),Feed)
    thread = Thread(target=publisher.serve_forever,daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{publisher.server_port}'
    report = {'passed':False,'checks':[],'model_calls':0}

    def check(channel, *, apply=True):
        return updates.update(store,origin,channel,key.public_key(),apply=apply)

    def served(version):
        with httpx.Client(trust_env=False,timeout=10) as client:
            response = client.get('http://web/vibeflow-version.json')
            assert response.status_code==200 and response.json()['release']==version
            assert response.headers.get('cache-control')=='no-store'
            entry = client.get('http://web/')
            assert entry.status_code==200 and f'content="{version}"' in entry.text
            assert entry.headers.get('x-content-type-options')=='nosniff'
            assert client.get('http://web/api/health').json()['ok']

    try:
        assert check('stable',apply=False)['action']=='available'
        assert not (store/'active').exists()
        assert check('stable')=={'action':'activated','release':stable.stamp}
        (exchange/'ready-stable').touch()
        wait_for((exchange/'advance-beta').exists,'Edge did not load the stable client')
        served(stable.stamp)
        channel_path = feed/'channels/beta.json'
        original = channel_path.read_bytes()
        forged = json.loads(original)
        forged['payload']['version']='9.9.9-beta.1'
        expired = beta.model_copy(update={'expires_at':int(time.time())-60})
        for value, label in ((updates.canonical(forged),'tampered signature'),(updates.canonical(
                updates.sign(updates.SCHEMA,expired,key)),'expired signed manifest')):
            channel_path.write_bytes(value)
            try:
                check('beta')
            except (ValueError,InvalidSignature):
                served(stable.stamp)
                report['checks'].append(label)
            else:
                raise AssertionError(label+' activated')
        channel_path.write_bytes(original)
        for fault in ('cut','corrupt'):
            Feed.fault = fault
            try:
                check('beta')
            except (ValueError,httpx.TransportError):
                served(stable.stamp)
                report['checks'].append('actual HTTP '+fault+' preserved stable version')
            else:
                raise AssertionError('Incomplete or corrupt archive activated')
        Feed.fault = None
        assert check('beta')=={'action':'activated','release':beta.stamp}
        served(beta.stamp)
        with httpx.Client(trust_env=False,timeout=10) as client:
            old = client.get('http://web/assets/stable-only.txt')
            assert old.status_code==200 and old.text=='stable retained asset'
        report['checks'].append('verified beta activation and retained previous asset')
        (exchange/'ready-beta').touch()
        wait_for((exchange/'advance-rollback').exists,'Edge did not refresh to the beta client')
        assert updates.rollback(store,key.public_key())=={'action':'rolled_back','release':stable.stamp}
        served(stable.stamp)
        stale = beta.model_copy(update={'sequence':1})
        channel_path.write_bytes(updates.canonical(updates.sign(updates.SCHEMA,stale,key)))
        try:
            check('beta')
        except ValueError:
            served(stable.stamp)
            report['checks'].append('manual rollback works and stops stale channel upgrades')
        else:
            raise AssertionError('The signed stale channel activated after rollback')
        (exchange/'ready-rollback').touch()
        report['passed'] = True
    finally:
        publisher.shutdown()
        publisher.server_close()
        thread.join(5)
        (exchange/'worker-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


EDGE = """
import json,sys,time
from pathlib import Path
from playwright.sync_api import expect,sync_playwright
cfg=json.load(sys.stdin); root=Path(cfg['root'])
def wait(name):
    deadline=time.monotonic()+90
    while not (root/name).exists():
        if time.monotonic()>deadline: raise TimeoutError(name)
        time.sleep(.2)
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    context=browser.new_context(viewport={'width':1440,'height':960})
    context.add_init_script('localStorage.setItem("vf_token",'+json.dumps(cfg['token'])+');')
    page=context.new_page(); errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(cfg['url'],wait_until='domcontentloaded')
    meta=page.locator('meta[name="vibeflow-client-release"]')
    notice=page.get_by_test_id('client-version-notice')
    expect(meta).to_have_attribute('content','stable:1:1.0.0')
    expect(page.locator('.workbench-shell')).to_be_visible()
    assert notice.count()==0
    (root/'advance-beta').touch(); wait('ready-beta')
    page.evaluate('window.dispatchEvent(new Event("focus"))')
    expect(notice).to_contain_text('1.1.0-beta.1')
    notice.screenshot(path=str(root/'update-1440.png'))
    page.set_viewport_size({'width':390,'height':844})
    sizes=notice.evaluate('node=>[node.clientWidth,node.scrollWidth]')
    assert sizes[0]+1>=sizes[1] and sizes[0]>0
    notice.screenshot(path=str(root/'update-390.png'))
    notice.get_by_role('button',name='刷新工作台').click()
    expect(meta).to_have_attribute('content','beta:2:1.1.0-beta.1')
    expect(notice).to_have_count(0)
    (root/'advance-rollback').touch(); wait('ready-rollback')
    page.evaluate('window.dispatchEvent(new Event("focus"))')
    expect(notice).to_contain_text('1.0.0')
    notice.get_by_role('button',name='刷新工作台').click()
    expect(meta).to_have_attribute('content','stable:1:1.0.0')
    assert not errors,errors
    (root/'browser-result.json').write_text(json.dumps({'passed':True,'edge':browser.version,'mobile_width':sizes,
        'stable_to_beta_and_rollback':True,'model_calls':0},indent=2),encoding='utf8')
    browser.close()
"""


def docker(*command, check=True):
    return subprocess.run(['docker',*command],check=check,capture_output=True,text=True,timeout=45)


def orchestrate(args):
    workspace = SERVER.parents[1]
    evidence = Path(tempfile.mkdtemp(prefix='ff-j24-client-updates-',dir=workspace/'tmp'))
    print('J24_EVIDENCE',evidence,flush=True)
    dist = args.dist.resolve(strict=True)
    stable = evidence/'stable-build'
    shutil.copytree(dist,stable)
    (stable/'assets/stable-only.txt').write_text('stable retained asset',encoding='ascii')
    group = 'vf-client-acceptance-'+uuid4().hex[:10]
    api, web = group+'-api',group+'-web'
    runner = None
    report = {'passed':False,'model_calls':0}
    infra = workspace/'platform/infra'
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0))
        port = reservation.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    try:
        docker('network','create',group)
        docker('volume','create','--driver','local','--opt','type=tmpfs','--opt','device=tmpfs',
               '--opt','o=size=900m',group)
        docker('run','--rm','--pull','never','-d','--name',api,'--network',group,'--network-alias','api',
               '--mount',f'type=volume,src={group},dst=/opt/vibeflow-client',
               '--mount',f'type=bind,src={SERVER},dst=/source/platform/server,readonly',
               '--mount',f'type=bind,src={evidence},dst=/source/tmp',
               '--mount',f'type=bind,src={dist},dst=/source/dist,readonly',
               '-e','VIBEFLOW_DATABASE_URL=sqlite+aiosqlite:////tmp/client-fixture.sqlite',
               '-e','VIBEFLOW_STORAGE_FS_ROOT=/tmp/client-fixture-files',
               '-e','VIBEFLOW_STORAGE_MAINTENANCE_ENABLED=false','-e','VIBEFLOW_DISTRIBUTED_JOBS=false',
               '-e','PYTHONDONTWRITEBYTECODE=1','-e','VIBEFLOW_JWT_SECRET='+uuid4().hex,
               '--workdir','/tmp',args.api_image,
               'python','-m','uvicorn','app.main:app','--app-dir','/source/platform/server',
               '--host','0.0.0.0','--port','8000')
        docker('run','--rm','--pull','never','-d','--name',web,'--network',group,'--network-alias','web',
               '-p',f'127.0.0.1:{port}:80',
               '--mount',f'type=volume,src={group},dst=/opt/vibeflow-client,readonly',
               '--mount',f'type=bind,src={infra/"nginx-web.conf"},dst=/etc/nginx/conf.d/default.conf,readonly',
               '--mount',f'type=bind,src={infra/"nginx-upstream-default"},dst=/etc/nginx/vibeflow-upstreams,readonly',
               '--mount',f'type=bind,src={infra/"client-updates"},dst=/etc/nginx/vibeflow-client,readonly',args.web_image)
        with (evidence/'worker.log').open('w',encoding='utf8') as log:
            runner = subprocess.Popen(['docker','exec',api,'python',
                '/source/platform/server/client_update_acceptance.py','--worker'],
                                      stdout=log,stderr=subprocess.STDOUT)

            def ready():
                if runner.poll() is not None:
                    raise RuntimeError('Client update worker exited before publication')
                return (evidence/'ready-stable').exists()

            wait_for(ready,'Signed stable client was not published')
            with httpx.Client(base_url=base,trust_env=False,timeout=15) as app:
                registration=app.post('/api/v1/auth/register',json={'email':uuid4().hex+'@example.com',
                    'password':'isolated-client-updates','name':'Client release acceptance'})
                registration.raise_for_status()
                data=json.dumps({'url':base,'token':registration.json()['token'],'root':str(evidence)})
                subprocess.run([str(args.browser_python),'-c',EDGE],input=data,text=True,check=True,timeout=300)
            assert runner.wait(timeout=25)==0
        assert all(json.loads((evidence/name).read_text(encoding='utf8'))['passed']
                   for name in ('browser-result.json','worker-result.json'))
        report['passed']=True
    finally:
        for name in (web,api):
            existing=docker('container','inspect','--format','{{.Name}}',name,check=False)
            if existing.returncode==0 and existing.stdout.strip()=='/'+name:
                docker('stop','--time','5',name,check=False)
        if runner is not None and runner.poll() is None:
            runner.wait(timeout=15)
        volume=docker('volume','rm',group,check=False)
        network=docker('network','rm',group,check=False)
        report['fixture_removed']=volume.returncode==0 and network.returncode==0
        (evidence/'client-update-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


if __name__=='__main__':
    if sys.argv[1:]==['--worker']:
        worker()
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist',type=Path,required=True)
        parser.add_argument('--browser-python',type=Path,required=True)
        parser.add_argument('--api-image',required=True)
        parser.add_argument('--web-image',required=True)
        orchestrate(parser.parse_args())
