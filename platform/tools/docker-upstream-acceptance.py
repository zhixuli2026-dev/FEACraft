"""Actual API container IP replacement; no inference, fabricated service or nginx reload."""
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import httpx

R=Path(__file__).resolve().parents[2]
review=json.loads(Path(sys.argv[1]).read_text('utf8'))
assert review['classification']=='no_llm' and review['entry']=='platform/tools/docker-upstream-acceptance.py'
for name,value in review['sources'].items():assert hashlib.sha256((R/name).read_bytes()).hexdigest()==value,name
D=R/'tmp/docker-deployment-20261117'/('upstream-'+secrets.token_hex(5));D.mkdir()
result={'passed':False,'checks':[],'real_model_calls':0,'mock_llm_calls':0}
compose=['docker','compose','-p','vf-review-20261117','-f',str(R/'tmp/docker-deployment-20261117/compose.json')]
def command(*args):
    p=subprocess.run(args,capture_output=True,text=True,encoding='utf8',timeout=90)
    if p.returncode:raise RuntimeError('Docker operation failed: '+args[1]+' '+p.stderr[:500])
    return p.stdout.strip()
def save(): (D/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf8')
def check(name): result['checks'].append(name);save();print('UPSTREAM_PASS '+name,flush=True)
def identity(name):return json.loads(command('docker','inspect',name,'--format','{"id":"{{.Id}}","started":"{{.State.StartedAt}}"}'))
holder='vf-upstream-ip-'+secrets.token_hex(4)
removed=False
try:
    assert command('docker','exec','vf-review-20261117-postgres','psql','-U','vibeflow','-d','vibeflow','-tAc',
        "SELECT count(*) FROM jobs WHERE status IN ('queued','running')")=='0'
    command('docker','exec','vf-review-20261117-web','nginx','-t')
    before=identity('vf-review-20261117-web');workers=command('docker','top','vf-review-20261117-web','-eo','pid,comm')
    networks=json.loads(command('docker','inspect','vf-review-20261117-api','--format','{{json .NetworkSettings.Networks}}'))
    assert len(networks)==1
    network,details=next(iter(networks.items()));old_ip=details['IPAddress']
    result['old_ip']=old_ip;result['network']=network;result['web_before']=before
    with httpx.Client(base_url='http://127.0.0.1:62883',trust_env=False,timeout=5) as client:
        initial=client.get('/api/health');assert initial.status_code==200 and initial.json()['ok']
        command(*compose,'rm','--stop','--force','api');removed=True
        # Reserve the actual old address to ensure this test exercises a new IP.
        # This container only sleeps; it does not imitate an API or model service.
        command('docker','run','-d','--name',holder,'--network',network,
            '--entrypoint','/bin/sleep','vibeflow/web:dynamic-upstream-20261117','120')
        command(*compose,'up','-d','--no-deps','api');removed=False
        new_networks=json.loads(command('docker','inspect','vf-review-20261117-api','--format','{{json .NetworkSettings.Networks}}'))
        new_ip=new_networks[network]['IPAddress'];assert new_ip and new_ip!=old_ip;result['new_ip']=new_ip
        began=time.monotonic();statuses=[]
        while time.monotonic()-began<75:
            try:
                response=client.get('/api/health');statuses.append(response.status_code)
                if response.status_code==200 and response.json().get('ok'):break
            except (httpx.HTTPError,ValueError):statuses.append('connection-error')
            time.sleep(.5)
        else:raise TimeoutError('Proxy did not recover from actual API IP replacement')
        result['observed_statuses']=statuses;result['recovery_seconds']=round(time.monotonic()-began,2)
        assert identity('vf-review-20261117-web')==before
        assert command('docker','top','vf-review-20261117-web','-eo','pid,comm')==workers
        check('actual_changed_API_IP_recovers_without_nginx_restart_reload_or_worker_replacement')
        prior=R/'tmp/docker-deployment-20261117/reference-doc-e929615c24'
        account=json.loads((prior/'private/account.json').read_text('utf8'))
        auth=client.post('/api/v1/auth/login',json={'email':account['email'],'password':account['password']});auth.raise_for_status()
        client.headers['Authorization']='Bearer '+auth.json()['token']
        receipt=json.loads((prior/'result.json').read_text('utf8'))
        for index,publication in enumerate(receipt['publications']):
            response=client.get(f'/api/v1/cases/{receipt["case_id"]}/report/artifact',params={
                'path':'E-report/build/report.docx','publication_id':publication},timeout=30)
            assert response.status_code==200
            assert hashlib.sha256(response.content).hexdigest()==receipt['artifact_sha256'][index]
        check('authenticated_report_query_parameters_and_both_historical_downloads_survive_replacement')
        result['passed']=True
except BaseException as error:
    result['failure']={'type':type(error).__name__,'detail':str(error)[:700]}
    raise
finally:
    if removed:command(*compose,'up','-d','--no-deps','api')
    subprocess.run(['docker','rm','-f',holder],capture_output=True,timeout=30)
    save()
print(json.dumps({'passed':result['passed'],'evidence':str(D),'checks':len(result['checks'])}),flush=True)
