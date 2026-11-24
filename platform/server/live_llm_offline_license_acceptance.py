"""Real VibeFlow offline grants and hosted quota admissions against official DeepSeek."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Plan
from app.services import offline_license as licenses, offline_license_admin as issuer
from live_llm_acceptance import FIXTURE, source
from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer, SERVER


def install(path, content):
    selected = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    issuer.write_new(selected, content)
    os.replace(selected, path)


async def for_test_limits(url):
    sql = create_async_engine(url)
    try:
        async with sql.begin() as db:
            row = (await db.execute(select(Plan.id, Plan.quotas).where(Plan.code=='free'))).one()
            limits = json.loads(row.quotas)
            await db.execute(update(Plan).where(Plan.id==row.id).values(quotas=json.dumps({**limits,'llm_msgs_per_day':0})))
    finally:
        await sql.dispose()


def load_report(path):
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []


EDGE = """
import json,sys
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
data=json.load(sys.stdin); root=Path(data['root']); observations=[]
with sync_playwright() as driver:
    browser=driver.chromium.launch(channel='msedge',headless=True)
    for width,height in ((1440,900),(390,844)):
        page=browser.new_page(viewport={'width':width,'height':height})
        page.add_init_script('localStorage.setItem("vf_token",'+json.dumps(data['token'])+');')
        page.goto(data['url'], wait_until='domcontentloaded')
        if width==390:
            page.get_by_label('工作面板').select_option('settings')
        else:
            page.locator('.dv-tab').filter(has_text='设置').first.click()
        status=page.get_by_test_id('offline-license-status')
        expect(status).to_contain_text('离线许可')
        expect(status).to_contain_text('已生效')
        expect(page.locator('#quota-heading')).to_contain_text('团队版')
        expect(page.get_by_test_id('effective-quotas')).to_contain_text('20000')
        expect(page.get_by_test_id('billing-console')).to_have_count(0)
        sizes=status.evaluate('node => [node.clientWidth, node.scrollWidth]')
        assert sizes[0]+1>=sizes[1] and sizes[0]>0
        status.screenshot(path=str(root/f'offline-license-{width}.png'))
        observations.append({'width':width,'status':'active','effective_plan':'team','status_width':sizes})
        page.close()
    browser.close()
(root/'license-browser.json').write_text(json.dumps({'passed':True,'observations':observations,'model_calls':0},indent=2),encoding='utf8')
"""


def verify(config, database, browser_python=None, dist=None):
    api = IsolatedServer('live-j03-offline-license', app='license_fixture:app', database_url=database)
    deployment = uuid4()
    path, key_path = api.root/'license.json', api.root/'issuer.pem'
    private, passphrase = api.root/'issuer-private.pem', api.root/'issuer-password'
    env = {'VIBEFLOW_OFFLINE_LICENSE_BUNDLE_PATH':str(path),
           'VIBEFLOW_OFFLINE_LICENSE_PUBLIC_KEY_PATH':str(key_path),
           'VIBEFLOW_OFFLINE_LICENSE_DEPLOYMENT':str(deployment),
           'VIBEFLOW_JWT_SECRET':uuid4().hex}
    fixture = api.root/'license_fixture.py'

    def write_fixture():
        source_code = 'import os\nos.environ.update('+repr(env)+')\nlive_request_limit=2\n'+FIXTURE
        if dist is not None:
            source_code += ('\nfrom fastapi.staticfiles import StaticFiles\n'
                            +f'app.mount("/",StaticFiles(directory={str(dist)!r},html=True))\n')
        fixture.write_text(source_code,encoding='utf8')

    write_fixture()
    report = {'passed':False,'model':config['model'],'mock_llm_calls':0,'checks':[]}
    records_path = api.root/'upstream.jsonl'

    def command(*args, capture=False):
        return subprocess.run([sys.executable,'-m','app.services.offline_license_admin',*map(str,args)],
                              cwd=SERVER, check=True, timeout=30, capture_output=capture)

    try:
        issuer.write_new(passphrase, secrets.token_urlsafe(32).encode(), private=True)
        auth = ['--private-key',private,'--passphrase-file',passphrase]
        command('generate-key','--public-key',key_path,*auth)
        fingerprint = command('device',capture=True).stdout.decode().strip()
        assert fingerprint == licenses.device()
        key = serialization.load_pem_private_key(private.read_bytes(), passphrase.read_bytes())
        trusted = key_path.read_bytes()
        assert b'ENCRYPTED PRIVATE KEY' in private.read_bytes()
        print('J03_LICENSE_EVIDENCE', api.root, flush=True)
        with api:
            api.create_case('Verified offline grant')
            account = api.call('GET','/org/mine')
            org = account['org']['id']
            assert account['plan']['code']=='free' and account['offline_license']['status']=='invalid'
            asyncio.run(for_test_limits(api.env['VIBEFLOW_DATABASE_URL']))
            billing = api.call('GET',f'/org/{org}/billing')
            assert billing['provider']=='offline' and billing['effective_plan']=='free'
            api.call('POST',f'/org/{org}/billing:mock-checkout',
                {'plan_code':'team','confirmed':True}, expect=409)
            profile = api.call('POST','/provider-profiles', {'provider':'deepseek','name':'Real DeepSeek',
                'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})['id']

            def model(expect=200):
                return api.call('POST','/llm/translate',{'provider_profile_id':profile,
                    'text':'The inlet speed is 9 metres per second.','target':'zh'},expect=expect)

            def check(state, plan='free'):
                info = api.call('GET','/org/mine')
                assert info['offline_license']['status']==state and info['plan']['code']==plan, info['offline_license']
                report['checks'].append(state)

            model(429)
            assert load_report(records_path)==[]
            valid = api.root/'issued.json'
            command('issue','--deployment',deployment,'--org-id',org,'--plan','pro','--device',fingerprint,
                    '--days',10,'--output',valid,*auth)
            payload = valid.read_bytes()
            grant, revocations = licenses.validated_bundle(payload,trusted)
            assert revocations.sequence==1
            install(path,payload)
            check('active','pro')
            assert api.call('GET',f'/org/{org}/members')['seats']['limit']==10
            translated = model()
            assert translated['ok'] and '9' in translated['translation']
            assert len([r for r in load_report(records_path) if r.get('status')==200])==1

            tampered = json.loads(payload)
            tampered['grant']['payload']['plan']='team'
            install(path,licenses.canonical(tampered))
            check('invalid')
            model(429)

            substitute_key = Ed25519PrivateKey.generate()
            wrong_issuer = issuer.issue(substitute_key,deployment=deployment,org_id=org,plan='team',
                device=fingerprint,days=10)
            install(path,wrong_issuer)
            check('invalid')
            wrong_account = issuer.issue(key,deployment=deployment,org_id=org+1,plan='team',
                device=fingerprint,days=10)
            install(path,wrong_account)
            check('unassigned')
            expired = issuer.issue(key,deployment=deployment,org_id=org,plan='team',
                device=fingerprint,days=1,at=int(time.time())-86500)
            install(path,expired)
            check('expired')
            model(429)

            replacement = api.root/'replacement.json'
            foreign = secrets.token_hex(32)
            command('issue','--deployment',deployment,'--org-id',org,'--plan','pro','--device',foreign,
                    '--days',10,'--previous',valid,'--output',replacement,*auth)
            moved, sequence = licenses.validated_bundle(replacement.read_bytes(),trusted)
            assert moved.serial!=grant.serial and grant.serial in sequence.serials and sequence.sequence==2
            install(path,replacement.read_bytes())
            check('other_device')
            model(429)
            revoked = api.root/'revoked.json'
            command('revoke','--previous',valid,'--output',revoked,*auth)
            install(path,revoked.read_bytes())
            check('revoked')
            assert api.call('GET',f'/org/{org}/members')['seats']['limit']==2
            model(429)
            signed_new = issuer.issue(key,deployment=deployment,org_id=org,plan='team',device=fingerprint,
                days=10,previous=replacement.read_bytes())
            updated, carried = licenses.validated_bundle(signed_new,trusted)
            assert carried.sequence==3 and {grant.serial,moved.serial}.issubset(carried.serials)
            install(path,signed_new)
            check('active','team')
            assert api.call('GET',f'/org/{org}/members')['seats']['limit']==50
            assert updated.device==fingerprint and model()['ok']
        env['VIBEFLOW_OFFLINE_LICENSE_MIN_REVOCATION_SEQUENCE']='3'
        write_fixture()
        with api:
            check('active','team')
            install(path,payload)
            check('stale_revocations')
            install(path,signed_new)
            check('active','team')
            report['checks'].append('trusted revocation floor and paid plan persist after API restart')
            if browser_python is not None:
                subprocess.run([str(browser_python),'-c',EDGE],input=json.dumps(
                    {'root':str(api.root),'url':api.base,'token':api.token}),text=True,check=True,timeout=150)
                report['checks'].append('Edge desktop and mobile show the signed entitlement')
        receipts = load_report(records_path)
        assert len([r for r in receipts if r.get('status')==200])==2
        report['passed']=True
    finally:
        report['receipts']=load_report(records_path)
        report['database']='postgresql' if database else 'sqlite'
        private.unlink(missing_ok=True)
        passphrase.unlink(missing_ok=True)
        (api.root/'.vault_key').unlink(missing_ok=True)
        (api.root/'license-acceptance.json').write_text(json.dumps(report,indent=2),encoding='utf8')


async def main(args):
    configuration = await source(args.source_profile_id)
    async with disposable_database(args.postgres) as database:
        await asyncio.to_thread(verify, configuration, database, args.browser_python,
                                args.dist.resolve(strict=True) if args.dist else None)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',required=True,type=int)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--browser-python',type=Path)
    parser.add_argument('--dist',type=Path)
    args = parser.parse_args()
    if bool(args.browser_python) != bool(args.dist):
        parser.error('--browser-python and --dist must be used together')
    asyncio.run(main(args))
