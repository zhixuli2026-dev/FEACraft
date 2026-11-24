"""Verify signed device transfer with an actual network-disabled Linux API."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import offline_license as licenses, offline_license_admin as issuer
from testsupport import IsolatedServer, SERVER


def worker(data):
    api = IsolatedServer('linux-license', app='license_fixture:app')
    cache = Path('/vf-license-fixture')
    package = cache/'license.json'
    pem = cache/'issuer.pem'
    pem.write_text(data['key'],encoding='ascii')
    assert licenses.device() == data['linux_id'] and data['linux_id'] != data['windows_id']
    env = dict(VIBEFLOW_DATABASE_URL='sqlite+aiosqlite:///'+(cache/'data.sqlite').as_posix(),
               VIBEFLOW_STORAGE_FS_ROOT=str(cache/'files'), VIBEFLOW_CODEX_STATE_ROOT=str(cache/'cli'),
               VIBEFLOW_OFFLINE_LICENSE_BUNDLE_PATH=str(package), VIBEFLOW_OFFLINE_LICENSE_PUBLIC_KEY_PATH=str(pem),
               VIBEFLOW_OFFLINE_LICENSE_DEPLOYMENT=data['deployment'])
    (api.root/'license_fixture.py').write_text(
        'import os\nos.environ.update('+repr(env)+')\nfrom app.main import app\n',encoding='utf8')
    report = {'passed':False,'network':'none','fixture_isolated':True,'model_calls':0}
    try:
        package.write_text(data['initial'],encoding='ascii')
        with api:
            api.create_case('Linux offline transfer')
            initial = api.call('GET','/org/mine')
            assert initial['org']['id']==1
            assert initial['offline_license']['status']=='other_device' and initial['plan']['code']=='free'
            entered = package.with_suffix('.next')
            entered.write_text(data['replacement'],encoding='ascii')
            entered.replace(package)
            replaced = api.call('GET','/org/mine')
            assert replaced['offline_license']['status']=='active' and replaced['plan']['code']=='pro'
            org = api.call('GET','/org/1/members')
            assert org['seats']['limit']==10
            entered.write_text(data['revoked'],encoding='ascii')
            entered.replace(package)
            revoked = api.call('GET','/org/mine')
            assert revoked['offline_license']['status']=='revoked' and revoked['plan']['code']=='free'
            assert api.call('GET','/org/1/members')['seats']['limit']==2
            report.update(passed=True,device_scope=['other_device','active','revoked'],seat_limits=[2,10,2])
    finally:
        (api.root/'license-linux.json').write_text(json.dumps(report,indent=2),encoding='utf8')


def verify(image):
    root = SERVER.parents[1]
    evidence = Path(tempfile.mkdtemp(dir=root/'tmp',prefix='ff-j03-linux-license-'))
    print('J03_LINUX_EVIDENCE',evidence,flush=True)
    pinned = subprocess.run(['docker','image','inspect','--format','{{.Id}}',image],capture_output=True,text=True,
                            check=True,timeout=15).stdout.strip()
    assert re.fullmatch(r'sha256:[0-9a-f]{64}',pinned)
    machine_id = evidence/'linux-system-id'
    # This isolated Linux installation keeps its own machine identity through
    # both container starts. Desktop and the normal WSL distribution are untouched.
    machine_id.write_text(uuid4().hex+'\n',encoding='ascii')
    prefix = 'vf-license-acceptance-'+uuid4().hex[:12]

    def docker(name, command, data=None):
        return subprocess.run(['docker','run','--rm','--pull','never','--name',name,'--network','none',
            '--cap-drop','ALL','-i','-e','PYTHONDONTWRITEBYTECODE=1','--tmpfs','/vf-license-fixture:rw,size=512m',
            '--mount',f'type=bind,src={machine_id},dst=/etc/machine-id,readonly',
            '--mount',f'type=bind,src={SERVER},dst=/source/platform/server,readonly',
            '--mount',f'type=bind,src={evidence},dst=/source/tmp',
            '--workdir','/source/platform/server',pinned,'python',*command],input=data,
            capture_output=True,text=True,check=False,timeout=110)

    report = {'passed':False,'image':pinned,'model_calls':0}
    try:
        identity = docker(prefix+'-device',['-m','app.services.offline_license_admin','device'])
        assert identity.returncode==0 and re.fullmatch(r'[0-9a-f]{64}',identity.stdout.strip())
        linux_id = identity.stdout.strip()
        windows_id = licenses.device()
        key = Ed25519PrivateKey.generate()
        deployment = uuid4()
        params = dict(deployment=deployment,org_id=1,plan='pro',days=10)
        initial = issuer.issue(key,device=windows_id,**params)
        replacement = issuer.issue(key,device=linux_id,previous=initial,**params)
        first,_ = licenses.validated_bundle(initial,issuer.public_pem(key))
        _,revocations = licenses.validated_bundle(replacement,issuer.public_pem(key))
        assert first.serial in revocations.serials and revocations.sequence == 2
        revoked = issuer.revoke(key,replacement)
        payload = json.dumps(dict(key=issuer.public_pem(key).decode(),deployment=str(deployment),
            windows_id=windows_id,linux_id=linux_id,initial=initial.decode(),replacement=replacement.decode(),revoked=revoked.decode()))
        result = docker(prefix+'-api',['offline_license_device_acceptance.py','--container-worker'],payload)
        (evidence/'container.log').write_text(result.stdout+result.stderr,encoding='utf8')
        assert result.returncode==0
        checks = list(evidence.glob('linux-license-*/license-linux.json'))
        assert len(checks)==1 and json.loads(checks[0].read_text(encoding='utf8'))['passed']
        report.update(passed=True,proof=str(checks[0].relative_to(evidence)),initial_serial_revoked=True)
    finally:
        report['isolated_containers_removed']=all(subprocess.run(
            ['docker','container','inspect',prefix+suffix],capture_output=True,timeout=10).returncode!=0
            for suffix in ('-device','-api'))
        (evidence/'transfer-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


if __name__=='__main__':
    if sys.argv[1:]==['--container-worker']:
        worker(json.load(sys.stdin))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--linux-image',required=True)
        args=parser.parse_args()
        verify(args.linux_image)
