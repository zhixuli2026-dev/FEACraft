"""Exercise the production default Compose storage on isolated local Docker volumes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from cryptography.fernet import Fernet
import httpx


SERVER = Path(__file__).resolve().parent
PLATFORM = SERVER.parent
INFRA = PLATFORM / 'infra'
TARGET = 'Ref/default-storage.txt'


def docker(*args, env=None, timeout=150, check=True):
    result = subprocess.run(['docker', *args], cwd=INFRA, env=env, capture_output=True,
                            encoding='utf8', errors='replace', timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'Docker {args[0]} failed with status {result.returncode}')
    return result


def wait(probe, description, seconds=85):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        found = probe()
        if found is not None and found is not False:
            return found
        time.sleep(.25)
    raise TimeoutError(description)


def image_with_current_sources(root, base, image):
    with tempfile.TemporaryDirectory(prefix='sources-', dir=root) as scratch:
        stage = Path(scratch)
        shutil.copytree(SERVER/'app', stage/'app', ignore=shutil.ignore_patterns(
            '__pycache__','*.pyc','.env','.env.*','.vault_key','*.key','*.pem'))
        shutil.copytree(SERVER/'migrations', stage/'migrations', ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for name in ('alembic.ini', 'requirements.txt'):
            shutil.copyfile(SERVER/name,stage/name)
        (stage/'check_requirements.py').write_text(
            'from importlib.metadata import version\nfrom pathlib import Path\n'
            'from packaging.requirements import Requirement\n'
            'for line in Path("requirements.txt").read_text().splitlines():\n'
            '    spec = line.split("#", 1)[0].strip()\n'
            '    if not spec:\n        continue\n'
            '    requirement = Requirement(spec)\n'
            '    if requirement.marker and not requirement.marker.evaluate():\n        continue\n'
            '    installed = version(requirement.name)\n'
            '    assert requirement.specifier.contains(installed), (requirement.name, installed, spec)\n'
            '    print(requirement.name, installed)\n',encoding='utf8')
        (stage/'Dockerfile').write_text(
            'FROM '+base+'\nCOPY app ./app\nCOPY migrations ./migrations\n'
            'COPY alembic.ini requirements.txt ./\nCOPY check_requirements.py /tmp/check_requirements.py\n'
            'RUN python /tmp/check_requirements.py && python -m pip check\n',encoding='utf8')
        build = docker('build','--pull=false','--network=none','--tag',image,str(stage),timeout=240,check=False)
        (root/'image-build.log').write_text(build.stdout+build.stderr,encoding='utf8')
        assert build.returncode==0,'Failed to assemble API from existing runtime layers and verify dependencies'


def default_compose(root, project, image, web_image):
    empty = root/'empty.env'
    empty.touch()
    key = root/'provider-vault.key'
    key.write_bytes(Fernet.generate_key())
    isolated = {name:value for name,value in os.environ.items()
                if not name.upper().startswith(('VIBEFLOW_','COMPOSE_'))}
    isolated.update(PG_PASSWORD=secrets.token_hex(24),JWT_SECRET=secrets.token_hex(32),
                    PROVIDER_VAULT_FILE=str(key))
    command = ['compose','--env-file',str(empty),'-p',project]
    configuration = docker(*command,'-f',str(INFRA/'docker-compose.prod.yml'),
                           'config','--format','json',env=isolated)
    rendered = json.loads(configuration.stdout)
    services = rendered['services']
    assert set(services)=={'postgres','api','web'},list(services)
    settings = services['api']['environment']
    assert settings['VIBEFLOW_STORAGE_BACKEND']=='fs' and settings['VIBEFLOW_ALLOW_PARTIAL_S3']=='false'
    assert settings['VIBEFLOW_DISTRIBUTED_JOBS']=='false' and settings['VIBEFLOW_STORAGE_FS_ROOT']=='/data/files'
    assert any(row['source']=='casedata' and row['target']=='/data/files' for row in services['api']['volumes'])
    for name, service in services.items():
        service['container_name']=project+'-'+name
    for name, tag in (('api',image),('web',web_image)):
        services[name].pop('build')
        services[name]['image']=tag
    services['web']['ports']=['127.0.0.1::80']
    assert all(row['name'].startswith(project+'_') and not row.get('external')
               for row in rendered['volumes'].values())
    path = root/'compose.json'
    path.write_text(json.dumps(rendered),encoding='utf8')
    return [*command,'-f',str(path)], isolated, (path,key,empty)


def call(client, method, path, body=None, *, expected=200, **options):
    response = client.request(method,'/api/v1'+path,json=body,**options)
    assert response.status_code==expected,(method,path,response.status_code,response.text[:500])
    return response


def register(client):
    credentials = {'email':'default-'+secrets.token_hex(8)+'@example.com','password':secrets.token_hex(20)}
    result = call(client,'POST','/auth/register',{**credentials,'name':'Default production storage'}).json()
    client.headers['Authorization']='Bearer '+result['token']
    return credentials


def ready(client):
    try:
        response = client.get('/api/health',timeout=2)
        return response.status_code==200 and response.json().get('ok') is True
    except (httpx.HTTPError, ValueError):
        return False


def wait_job(client, identifier, expected):
    def check():
        job = call(client,'GET',f'/jobs/{identifier}').json()
        if job['status'] in {'queued','running','cancelling'} or job.get('completion_pending'):
            return None
        assert job['status']==expected,(job['status'],job.get('error'),job.get('log_backlog'))
        stop = (job.get('attempt') or {}).get('process_stop')
        if stop and stop['verified']:
            return {'id':identifier,'status':job['status'],'verified_stop':True,
                    'worker':job['attempt']['process_identity']['worker_id']}
        return None
    return wait(check,'Production local job did not settle')


def verify(root, base, web_image):
    project = 'vf-j29-'+secrets.token_hex(6)
    image = 'vibeflow/j29-acceptance:'+project
    report = {'passed':False,'project':project,'storage':'fs','checks':[],'actual_model_calls':0,
              'simulated_llm_calls':0}
    secrets_paths, command, env = (),None,None

    def save():
        (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')

    def check(name):
        report['checks'].append(name)
        save()
        print('PROD_DEFAULT_STORAGE_OK',name,flush=True)

    def compose(*args, **options):
        return docker(*command,*args,env=env,**options)

    try:
        image_with_current_sources(root,base,image)
        command,env,secrets_paths = default_compose(root,project,image,web_image)
        check('rendered_default_has_only_postgres_api_web_and_persistent_fs')
        compose('up','-d','--no-build','postgres')

        def postgres_healthy():
            result = docker('inspect',project+'-postgres','--format','{{.State.Health.Status}}')
            return result.stdout.strip()=='healthy'
        wait(postgres_healthy,'Isolated PostgreSQL unavailable')
        compose('run','--rm','--no-deps','api','python','-m','alembic','upgrade','head')
        compose('up','-d','--no-build','api','web',timeout=200)
        binding = compose('port','web','80').stdout.strip()
        assert binding.startswith('127.0.0.1:'),binding
        report['binding']=binding
        container = project+'-api'
        with httpx.Client(base_url='http://'+binding,timeout=45,trust_env=False) as client:
            wait(lambda:ready(client),'Production default API unavailable through Nginx')
            page = client.get('/')
            assert page.status_code==200 and 'text/html' in page.headers['content-type']
            assert 'Content-Security-Policy' in page.headers
            credentials = register(client)
            workspace = call(client,'GET','/workspaces').json()[0]['id']
            case = call(client,'POST',f'/workspaces/{workspace}/cases',{'name':'Default production case'}).json()
            case_id = case['id']
            prefix = f'/cases/{case_id}'
            path = call(client,'GET',prefix).json()['storage_prefix']+'/'+TARGET
            body = secrets.token_hex(1024*1024+17)
            digest = hashlib.sha256(body.encode()).hexdigest()
            call(client,'PUT',prefix+'/file',{'path':TARGET,'content':body})
            raw = call(client,'GET',prefix+'/raw',params={'path':TARGET})
            assert len(raw.content)==len(body) and hashlib.sha256(raw.content).hexdigest()==digest
            physical = docker('exec',container,'sha256sum','/data/files/'+path).stdout.split()[0]
            assert physical==digest
            report['file']={'size':len(body),'sha256':digest,'volume_verified':True}
            check('real_web_and_postgres_account_with_file_in_named_volume')

            with httpx.Client(base_url='http://'+binding,timeout=20,trust_env=False) as other:
                register(other)
                call(other,'GET',prefix+'/raw',expected=404,params={'path':TARGET})
                call(other,'PUT',prefix+'/file',{'path':TARGET,'content':'foreign'},expected=404)
            check('other_account_cannot_read_or_write_persisted_case')

            def submit(source):
                return call(client,'POST',prefix+'/jobs',{'command':'python -u -c '+shlex.quote(source),
                            'runtime':'windows','timeout_seconds':65}).json()['id']
            job = submit("from pathlib import Path;import hashlib;"
                         "print('DEFAULT_STORAGE_SHA='+hashlib.sha256(Path('Ref/default-storage.txt').read_bytes()).hexdigest(),flush=True)")
            finished = wait_job(client,job,'succeeded')
            assert 'DEFAULT_STORAGE_SHA='+digest in call(client,'GET',f'/jobs/{job}').json()['log_backlog']
            hostname = docker('inspect',container,'--format','{{.Config.Hostname}}').stdout.strip()
            assert finished['worker'].startswith(hostname+':'),finished
            cancelled = submit("import time;print('DEFAULT_CANCEL_READY',flush=True);time.sleep(50)")
            wait(lambda:'DEFAULT_CANCEL_READY' in call(client,'GET',f'/jobs/{cancelled}').json()['log_backlog'],
                 'Native cancellation job did not start',seconds=50)
            sibling = call(client,'POST',f'/workspaces/{workspace}/cases',{'name':'Concurrent default storage'}).json()['id']
            sibling_route = f'/cases/{sibling}'
            sibling_path = 'Ref/concurrent.txt'
            call(client,'PUT',sibling_route+'/file',{'path':sibling_path,'content':'saved while native job is running'})
            assert call(client,'GET',f'/jobs/{cancelled}').json()['status']=='running'
            call(client,'POST',f'/jobs/{cancelled}:cancel',{})
            report['jobs']=[finished,wait_job(client,cancelled,'cancelled')]
            check('real_local_jobs_read_default_volume_and_cancel_without_redis')

            blocked = compose('run','--rm','--no-deps','-e','VIBEFLOW_STORAGE_BACKEND=s3',
                              '-e','VIBEFLOW_DATABASE_URL=sqlite+aiosqlite:////data/files/startup-blocked.sqlite',
                              'api',check=False,timeout=75)
            assert blocked.returncode!=0 and 'VIBEFLOW_ALLOW_PARTIAL_S3=true' in blocked.stderr
            assert 'Application startup failed' in blocked.stderr
            docker('exec',container,'test','!','-e','/data/files/startup-blocked.sqlite')
            assert ready(client)
            check('unacknowledged_s3_fails_before_creating_sqlite_database')

            before = docker('inspect',container,'--format','{{.Id}}').stdout.strip()
            compose('up','-d','--no-build','--no-deps','--force-recreate','api')
            after = docker('inspect',container,'--format','{{.Id}}').stdout.strip()
            assert before!=after
            # Nginx resolves its upstream at process startup.
            compose('restart','web')
            new_binding = compose('port','web','80').stdout.strip()
            assert new_binding.startswith('127.0.0.1:'),new_binding
            client.base_url = 'http://'+new_binding
            report['binding_after_recreate'] = new_binding
            wait(lambda:ready(client),'Recreated default API unavailable')
            client.headers.pop('Authorization')
            token = call(client,'POST','/auth/login',credentials).json()['token']
            client.headers['Authorization']='Bearer '+token
            assert hashlib.sha256(call(client,'GET',prefix+'/raw',params={'path':TARGET}).content).hexdigest()==digest
            assert call(client,'GET',f'/jobs/{job}').json()['status']=='succeeded'
            assert call(client,'GET',f'/jobs/{cancelled}').json()['status']=='cancelled'
            assert call(client,'GET',sibling_route+'/raw',params={'path':sibling_path}).content==b'saved while native job is running'
            report['api_recreated']=True
            check('new_container_retains_account_file_and_both_job_states')
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:900]}
        raise
    finally:
        if command is not None:
            # Only this randomly named project's containers, network and volumes.
            down = compose('down','--volumes',timeout=120,check=False)
            report['isolated_compose_removed']=down.returncode==0
            if down.returncode==0:
                for candidate in secrets_paths:
                    candidate.unlink(missing_ok=True)
        removed = docker('image','rm',image,check=False,timeout=30)
        report['disposable_image_removed']=removed.returncode==0
        save()
    assert report['isolated_compose_removed'] and report['disposable_image_removed']


if __name__=='__main__':
    sys.path.insert(0,str(PLATFORM/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-image',required=True)
    parser.add_argument('--web-image',required=True)
    args = parser.parse_args()
    enforce([Path(__file__)])
    for image in (args.api_image,args.web_image,'postgres:16-alpine'):
        docker('image','inspect',image)
    root = Path(tempfile.mkdtemp(prefix='prod-default-storage-',dir=PLATFORM.parent/'tmp'))
    print('PROD_DEFAULT_STORAGE_EVIDENCE',root,flush=True)
    verify(root,args.api_image,args.web_image)
