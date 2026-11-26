"""Disposable Linux Agent quota verification using only authorized DeepSeek."""
import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack
import ipaddress
import json
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from live_llm_acceptance import source
from linux_terminal_compute_acceptance import account, close_pty, command as send_command, open_pty, output, request
from mixed_load_test_support import API_IMAGE, WEB_IMAGE, Deployment, docker

ROOT = Path(__file__).resolve().parents[2]

CPU = r"""import subprocess, sys
routine = "import time\nstop=time.process_time()+0.72\nwhile time.process_time()<stop: pass"
children = [subprocess.Popen([sys.executable,'-c',routine]) for _ in range(2)]
for child in children: child.wait()
print('CPU_WORK_COMPLETED')
"""
MEMORY = """allocation=bytearray(160*1024**2)
print(len(allocation))
"""
NORMAL = """import os
from pathlib import Path
names=('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET','VIBEFLOW_VAULT_KEY_PATH','VIBEFLOW_FFJ16_TEST_CANARY')
assert not any(name in os.environ for name in names)
print(Path('Ref/proof.txt').read_text().strip())
"""
SINK = """
import select
import socket
import sys
from contextlib import ExitStack

port = int(sys.argv[1])
with ExitStack() as resources:
    servers = []
    for family, addr in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::1')):
        listener = resources.enter_context(socket.socket(family, socket.SOCK_STREAM))
        listener.bind((addr, port))
        listener.listen(4)
        servers.append(listener)
    while True:
        for listener in select.select(servers, [], [], 45)[0]:
            connection, _ = listener.accept()
            with connection:
                connection.sendall(b'owned-network-fixture')
"""


def network_fixture(container):
    ipv4 = docker('inspect','--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',container)
    if not ipaddress.IPv4Address(ipv4).is_private:
        raise ValueError('Network acceptance allows only its disposable private listener')
    port = 37841
    docker('exec','-d',container,'/usr/local/bin/python','-B','-c',SINK,str(port))
    control = """
import socket
import sys

for address in sys.argv[1:]:
    with socket.create_connection((address, 37841), timeout=2) as connection:
        assert connection.recv(128) == b'owned-network-fixture'
print('BOTH_REACHABLE')
"""
    addresses = (ipv4, '::1')
    assert Deployment.wait(lambda: docker('exec',container,'/usr/local/bin/python','-B','-c',
                                           control,*addresses), seconds=12) == 'BOTH_REACHABLE'
    return addresses


def isolated_program(addresses, outside_file):
    return NORMAL + f"""
import errno
import json
import socket
import subprocess
import sys

blocked = []
for address in {addresses!r}:
    try:
        with socket.create_connection((address, 37841), timeout=2):
            raise AssertionError('The owned network fixture was reachable from the shell')
    except OSError as error:
        assert error.errno in {{errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNREFUSED, errno.EADDRNOTAVAIL}}
        blocked.append(errno.errorcode[error.errno])
assert [name for _, name in socket.if_nameindex()] == ['lo']
namespace = Path('/proc/self/ns/net').stat().st_ino
child = subprocess.check_output([sys.executable, '-c',
    'from pathlib import Path; print(Path("/proc/self/ns/net").stat().st_ino)'], text=True).strip()
assert child == str(namespace)
print('AGENT_NETWORK=' + json.dumps({{'namespace':str(namespace), 'blocked':blocked}}))

try:
    Path({outside_file!r}).open('rb').close()
except (FileNotFoundError, PermissionError):
    pass
else:
    raise AssertionError('A file from another test account was visible')
assert not Path('/.old-root').exists()
try:
    Path('/usr/local/share/vibeflow-disposable-readonly-probe').touch(exist_ok=False)
except OSError as error:
    assert error.errno in {{errno.EROFS, errno.EACCES}}
else:
    raise AssertionError('The disposable runtime was writable')
status = dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert status['NoNewPrivs'].strip() == '1'
assert all(int(status[name], 16) == 0 for name in ('CapEff','CapPrm','CapInh','CapBnd','CapAmb'))
proof = Path('Ref/proof.txt').read_text().strip()
Path('/tmp/agent-test-scratch').write_text(proof)
Path('Ref/agent-shell-output.txt').write_text(proof)
print('AGENT_FILESYSTEM=VERIFIED')
"""


def nonroot_gate(container):
    probe = """
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from app.services import agent_shell_linux
from app.services.owned_process import OwnedProcess

async def main():
    root = OwnedProcess()
    root.isolate_linux_network = True

    async def observed(record, init):
        assert os.geteuid() == record['host']['uid'] == init['uid'] == 65534

    root.on_namespace = observed
    with tempfile.TemporaryDirectory(dir='/work/data') as case:
        payload = 'from pathlib import Path; Path("output.txt").write_text("verified"); ' \
                  'print(Path("/proc/self/ns/net").stat().st_ino)'
        argv = ['/usr/bin/unshare','--user','--map-root-user','--mount','--pid','--fork','--kill-child=SIGKILL',
                sys.executable,'-I','-S','-u',agent_shell_linux.__file__,'--workspace',case,'--',
                'python3 -c '+shlex.quote(payload)]
        try:
            await root.prepare(argv, cwd=case, env={'PATH':os.environ['PATH']}, runtime='linux')
            await root.verify_registration()
            await root.release()
            marker = (await asyncio.wait_for(root.proc.stdout.readline(), 5)).decode().strip()
            assert marker == root.record['linux_network']['init']
            assert await root.wait_root_exit(8) == 0
            stop = await root.stop(reason='root_exit', grace_ms=0)
            output = Path(case)/'output.txt'
            assert stop['scope_empty'] and stop['verified']
            assert output.read_text()=='verified' and output.stat().st_uid==65534
            print(json.dumps({'uid':65534, 'output_uid':output.stat().st_uid,
                              'namespace':root.record['linux_network'], 'verified_stop':True}))
        finally:
            await root.abort()

asyncio.run(main())
"""
    return json.loads(docker('exec','--user','65534:65534',container,'/usr/local/bin/python','-B','-c',probe))

async def stream(client, endpoint, path, token, payload):
    headers = {'Authorization':'Bearer '+token}
    async with client.post(endpoint+'/api/v1'+path,json=payload,headers=headers) as response:
        assert response.status==200,(path,response.status)
        frames=[]
        async for line in response.content:
            if line.startswith(b'data: '):
                frames.append(json.loads(line[6:]))
        return frames

async def prepare(client, endpoint, config, label, program, *, owner=None):
    compile(program, f'{label}/Ref/check.py', 'exec')
    token, case = owner if owner is not None else await account(client,endpoint)
    profile = await request(client,endpoint,'POST','/provider-profiles',token=token,json={
        'provider':'deepseek','name':'Real Linux resource test','api_key':config['key'],
        'base_url':config['base_url'],'default_model':config['model']})
    marker='LINUX_AGENT_REAL_'+uuid4().hex
    for name,content in (('check.py',program),('proof.txt',marker+'\n')):
        await request(client,endpoint,'PUT',f'/cases/{case}/file',token=token,
                      json={'path':'Ref/'+name,'content':content})
    conversation=await request(client,endpoint,'POST','/conversations',token=token,json={
        'case_id':case,'title':label,'permission_mode':'approve_writes',
        'provider_profile_id':profile['id'],'model':config['model']})
    base=f"/conversations/{conversation['id']}"
    prompt = ('Use only run_shell_command. Run the already-reviewed local case check with exactly '
              '{"command":"python3 Ref/check.py","timeout_seconds":35}. Call the tool now without any '
              'preliminary file inspection. Wait for approval and summarize its actual result in text.')
    for turn in range(1, 4):
        events = await stream(client, endpoint, base+'/messages', token, {'content':prompt,
                              'provider_profile_id':profile['id'],'model':config['model']})
        choices = [event['tool_call'] for event in events if event['type']=='approval.requested']
        if len(choices) == 1:
            choice, = choices
            if choice['tool'] == 'run_shell_command' and choice['args'] == {
                    'command':'python3 Ref/check.py', 'timeout_seconds':35}:
                break
        for choice in choices:
            await request(client, endpoint, 'POST', base+'/tool-calls/'+choice['id']+':reject', token=token, json={})
        print('AGENT_SELECTION_RETRY', label, turn, flush=True)
    else:
        raise AssertionError(f'The real model did not select the bounded command for {label}')
    approval=base+'/tool-calls/'+choice['id']
    await request(client,endpoint,'GET',approval+'/execution',token=token,expected=404)
    print('AGENT_SELECTED',label,flush=True)
    return {'endpoint':endpoint,'case':case,'token':token,'marker':marker,'approval':approval}

async def approve(client, run, error):
    endpoint,approval,token=(run[k] for k in ('endpoint','approval','token'))
    events=await stream(client,endpoint,approval+':approve',token,{})
    outcomes=[e for e in events if e['type']=='tool.result']
    assert len(outcomes)==1, [e['type'] for e in events]
    result,=outcomes
    assert result['approved'] is True
    if error=='unavailable':
        assert result['ok'] is False and result.get('code') == 'agent_compute_unverified',result
        await request(client,endpoint,'GET',approval+'/execution',token=token,expected=404)
        return {'tool_ok':False,'request_reserved':False,'reason':'agent_compute_unverified'}
    record=await request(client,endpoint,'GET',approval+'/execution',token=token)
    if error:
        assert result['ok'] is False and result.get('code') == 'agent_resource_limit',result
        assert record['phase']=='failed' and record['error']=='NativeComputeLimit',record
    else:
        assert result['ok'] is True and run['marker'] in result['result'],result
        assert record['phase']=='succeeded' and record['error'] is None,record
    assert record['cleanup_verified'] is True
    child,=record['children']['children'].values()
    stop=child['stop']
    assert stop['verified'] and stop['scope_empty'] and stop['reason']==(error or 'root_exit'),record
    network = child['identity']['linux_network']
    assert network['host'] != network['init']
    if not error:
        witness, = [line for line in result['result'].splitlines() if line.startswith('AGENT_NETWORK=')]
        observation = json.loads(witness.partition('=')[2])
        assert observation['namespace']==network['init'] and len(observation['blocked'])==2, observation
        run['network'] = observation
        assert 'AGENT_FILESYSTEM=VERIFIED' in result['result']
        async with client.get(endpoint+f"/api/v1/cases/{run['case']}/raw",
                              headers={'Authorization':'Bearer '+token},
                              params={'path':'Ref/agent-shell-output.txt'}) as reply:
            assert reply.status == 200 and await reply.text() == run['marker']
    return record

async def verify(endpoints,config,receipt,addresses):
    quota,legacy,unavailable=endpoints
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90),trust_env=False) as client:
        outside_token, outside_case = await account(client, quota)
        outside = await request(client,quota,'GET',f'/cases/{outside_case}',token=outside_token)
        control_path = 'Ref/disposable-other-account-marker.txt'
        await request(client,quota,'PUT',f'/cases/{outside_case}/file',token=outside_token,
                      json={'path':control_path,'content':'other account control'})
        outside_file = '/work/data/files/' + outside['storage_prefix'].rstrip('/') + '/' + control_path
        cases=('cpu','memory','ordinary','legacy','unavailable')
        programs=(CPU,MEMORY,isolated_program(addresses[0],outside_file),
                  isolated_program(addresses[1],outside_file),NORMAL)
        targets=(quota,quota,quota,legacy,unavailable)
        requests=dict(zip(cases,await asyncio.gather(*(prepare(client,endpoint,config,case,script)
                                      for case,script,endpoint in zip(cases,programs,targets)))))
        rows={}
        rows['cpu'],rows['ordinary']=await asyncio.gather(approve(client,requests['cpu'],'cpu_limit'),
                                                         approve(client,requests['ordinary'],None))
        rows['memory'],rows['legacy'],rows['unavailable']=await asyncio.gather(
            approve(client,requests['memory'],'memory_limit'),approve(client,requests['legacy'],None),
            approve(client,requests['unavailable'],'unavailable'))
        for label in ('cpu','memory','ordinary'):
            child,=rows[label]['children']['children'].values()
            usage=child['stop']['linux_compute_usage']
            assert usage['launch_nonce']==child['identity']['launch_nonce']
            assert usage['scope']['policy']=={'cpu_limit_ns':10**9,'memory_limit_bytes':64*1024**2}
            assert child['identity']['namespace_init']['pid_namespace']!=child['identity']['host']['pid_namespace']
            if label=='cpu':
                assert usage['usage']['cpu_used_ns']>=10**9
            elif label=='memory':
                assert usage['usage']['memory_oom_kills']>=1
            else:
                assert usage['violation'] is None and usage['usage']['cpu_used_ns']>0
        leg,=rows['legacy']['children']['children'].values()
        assert 'linux_compute_usage' not in leg['stop']
        cpu=requests['cpu']; other=requests['ordinary']
        assert rows['cpu']['id']!=rows['ordinary']['id']
        peer=await request(client,legacy,'GET',cpu['approval']+'/execution',token=cpu['token'])
        assert peer['children']==rows['cpu']['children'] and peer['cleanup_verified'] is True
        await request(client,quota,'GET',cpu['approval']+'/execution',token=other['token'],expected=404)
        receipt['cases']=rows
        receipt['ambient_credentials_absent']=['ordinary','legacy']
        receipt['network']={label:requests[label]['network'] for label in ('ordinary','legacy')}
        receipt['network_positive_controls']={'ipv4':True,'ipv6':True}
        async with client.get(quota+f'/api/v1/cases/{outside_case}/raw',
                              headers={'Authorization':'Bearer '+outside_token},params={'path':control_path}) as reply:
            assert reply.status == 200 and await reply.text() == 'other account control'
        receipt['filesystem'] = {'writable_case_readback':['ordinary','legacy'], 'other_account_file_hidden':True,
                                 'runtime_readonly':True,'private_scratch':True,'capabilities_empty':True}
        receipt['other_account_status']=404
        receipt['peer_receipt_equal']=True

async def check_home_terminal(client, endpoint, token, check, case=0):
    async with AsyncExitStack() as stack:
        socket, active = await open_pty(stack, client, endpoint, token, case)
        assert active['type'] == 'ready', active
        await send_command(socket, check+""" && printf '\n%s%s\n' 'HOME_' 'CHECK_OK'""")
        await output(socket, rb'HOME_CHECK_OK')
        return await close_pty(client, endpoint, token, active)


async def verify_s3(endpoints, config, receipt, addresses, *, home_reader=None):
    writer, reader = endpoints
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90),trust_env=False) as client:
        program = isolated_program(addresses[0], '/work/data/s3-local-outside-marker')
        owner = None
        if home_reader is not None:
            owner = await account(client,writer)
            marker = 'pty'+uuid4().hex
            agent_value = 'agent'+uuid4().hex
            fixture = receipt['home'] = {}
            fixture['first_pty'] = await check_home_terminal(client, writer, owner[0], f"""
test "$HOME" = /home/user && test "$PWD" != "$HOME" &&
test "$HISTFILE" = /tmp/.bash_history &&
printf '%s' {marker} > "$HOME/from-pty"
""".strip(), case=owner[1])
            other, _ = await account(client,writer)
            fixture['other_pty'] = await check_home_terminal(client, reader, other, """
test "$PWD" = "$HOME" && test ! -e "$HOME/from-pty" &&
test ! -e /work/shell-state/users && test "$(ls -1 /home)" = user &&
printf '%s' other-account > "$HOME/from-pty"
""".strip())
            program += f"""
assert Path.home() == Path('/home/user')
assert (Path.home()/'from-pty').read_text() == {marker!r}
assert list(Path('/home').iterdir()) == [Path.home()]
assert not Path('/work/shell-state/users').exists()
(Path.home()/'from-agent').write_text({agent_value!r})
"""
        command = await prepare(client,writer,config,'Agent S3 staging write',program,owner=owner)
        record = await approve(client,command,None)
        peer = await request(client,reader,'GET',command['approval']+'/execution',token=command['token'])
        assert peer['children']==record['children'] and peer['cleanup_verified']
        async with client.get(reader+f"/api/v1/cases/{command['case']}/raw",
                              headers={'Authorization':'Bearer '+command['token']},
                              params={'path':'Ref/agent-shell-output.txt'}) as reply:
            assert reply.status==200 and await reply.text()==command['marker']
        receipt.update(s3_write_read_on_cold_peer=True, s3_request=record)
        if home_reader is not None:
            check = f"""
test "$PWD" = "$HOME" && test "$HOME" = /home/user &&
test "$(cat from-pty)" = {marker} && test "$(cat from-agent)" = {agent_value}
""".strip()
            child, = record['children']['children'].values()
            assert child['identity']['host']['uid'] == child['identity']['namespace_init']['uid'] == 65534
            fixture['peer_pty'] = await check_home_terminal(client, reader, owner[0], check)
            before = await asyncio.to_thread(docker, 'inspect', '-f', '{{.State.StartedAt}}', home_reader)
            await asyncio.to_thread(docker, 'restart', home_reader)
            after = await asyncio.to_thread(docker, 'inspect', '-f', '{{.State.StartedAt}}', home_reader)
            assert before != after
            restarted = 'http://' + await asyncio.to_thread(docker, 'port', home_reader, '8000/tcp')
            assert restarted.startswith('http://127.0.0.1:')
            fixture['published_port_changed'] = restarted != reader
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90), trust_env=False) as fresh:
                try:
                    async with asyncio.timeout(30):
                        while True:
                            try:
                                async with fresh.get(restarted+'/api/health') as response:
                                    if response.status == 200 and (await response.json())['ok']:
                                        break
                            except aiohttp.ClientError:
                                pass
                            await asyncio.sleep(.25)
                except TimeoutError as error:
                    raise AssertionError('Restarted peer API did not become healthy') from error
                fixture['restarted_pty'] = await check_home_terminal(fresh, restarted, owner[0], check)
            fixture.update(api_restarted=True, cross_account=True, native_uid=65534,
                           pty_to_agent=True, agent_to_peer_pty=True, case_staging_separate=True)
            print('AGENT_SHARED_HOME_VERIFIED', flush=True)


def main(args):
    source_profile = asyncio.run(source(args.source_profile_id))
    origin = urlsplit(source_profile['base_url'])
    if origin.scheme != 'https' or origin.netloc != 'api.deepseek.com':
        raise ValueError('Linux Agent acceptance requires the authorized official DeepSeek profile')
    output=Path(tempfile.mkdtemp(prefix='j16-agent-linux-',dir=ROOT/'tmp'))
    print('AGENT_COMPUTE_EVIDENCE',output,flush=True)
    original=set(docker('ps','--no-trunc','-q').splitlines())
    result={'passed':False,'simulated_model_calls':0,'upstream':[]}
    try:
        with ExitStack() as stack:
            deployment=Deployment(stack,output,args.api_image,WEB_IMAGE)
            docker('exec',deployment.worker,'/usr/local/bin/python','-B','-c',
                   'from cryptography.fernet import Fernet; from pathlib import Path; '
                   'Path("/work/data/live-vault").write_bytes(Fernet.generate_key()); '
                   'Path("/work/data/llm-allowance").write_text("25")')
            deployment.env['VIBEFLOW_VAULT_KEY_PATH']='/work/data/live-vault'
            deployment.flags.extend(('-e','VIBEFLOW_VAULT_KEY_PATH'))
            if args.storage == 's3':
                deployment.configure_s3(output)
                print('AGENT_AUTHENTICATED_S3_READY', flush=True)
            if args.shared_home:
                state = deployment.volume('shell-state', '/work/shell-state')
                docker('run', '--rm', '--network', 'none', '--pull=never', '--mount', state, '--mount', deployment.data,
                       '--entrypoint', '/usr/bin/install', args.api_image, '-d', '-o', '65534', '-g', '65534',
                       '/work/shell-state', '/work/data/files')
                deployment.runtime.extend(('--mount',state))
                deployment.env['VIBEFLOW_LINUX_SHELL_STATE_ROOT']='/work/shell-state'
                deployment.flags.extend(('-e','VIBEFLOW_LINUX_SHELL_STATE_ROOT'))
            base=tuple(deployment.runtime)
            ipv6_loopback = ('--sysctl','net.ipv6.conf.lo.disable_ipv6=0')
            variants=((deployment.a,1,64,('--cgroupns','private','--cap-add','SYS_ADMIN',*ipv6_loopback)),
                      (deployment.b,0,0,ipv6_loopback), (deployment.c,1,64,('--cgroupns','private',)))
            if args.shared_home:
                variants=((deployment.a,0,0,ipv6_loopback), (deployment.b,0,0,ipv6_loopback))
            services=[]
            for name,seconds,memory,cap in variants:
                # Replicas share case files, so all variants use the same service UID.
                deployment.runtime[:]=base if args.shared_home else [
                    '0:0' if entry=='65534:65534' else entry for entry in base]
                deployment.runtime.extend((*cap,'-e',f'VIBEFLOW_AGENT_SHELL_LINUX_CPU_LIMIT_SECONDS={seconds}',
                    '-e',f'VIBEFLOW_AGENT_SHELL_LINUX_MEMORY_LIMIT_MB={memory}',
                    '-e','VIBEFLOW_FFJ16_TEST_CANARY=isolated-fixture-payload'))
                if args.storage == 's3' and name == deployment.b:
                    cache = '/work/data/files:rw,size=256m'
                    if args.shared_home:
                        cache += ',uid=65534,gid=65534'
                    deployment.runtime.extend(('--tmpfs',cache))
                identifier,endpoint=deployment.api(name)
                services.append((name,identifier,endpoint))
            try:
                addresses = [network_fixture(container) for _,container,_ in services[:2]]
                print('AGENT_NETWORK_FIXTURES_READY', flush=True)
                result['nonroot_gate'] = nonroot_gate(services[1][1])
                print('AGENT_NETWORK_NONROOT_VERIFIED', flush=True)
                endpoints = [endpoint for _,_,endpoint in services]
                if args.storage == 's3':
                    docker('exec',services[0][1],'/usr/bin/touch','/work/data/s3-local-outside-marker')
                    asyncio.run(verify_s3(endpoints[:2],source_profile,result,addresses,
                                          home_reader=services[1][1] if args.shared_home else None))
                    docker('exec',services[0][1],'/usr/local/bin/python','-B','-c',
                           'from pathlib import Path; assert not list(Path("/work/data/files/.staging").glob("job-*"))')
                    result['remaining_s3_stages'] = 0
                else:
                    asyncio.run(verify(endpoints,source_profile,result,addresses))
                for _,identifier,_ in services:
                    empty=docker('exec',identifier,'/usr/local/bin/python','-B','-c',
                               'from pathlib import Path; assert not any(p.name.startswith("vibeflow-compute-") '
                               'for p in Path("/sys/fs/cgroup").rglob("*")); print("EMPTY")')
                    assert empty=='EMPTY'
                    docker('exec',identifier,'/usr/bin/test','!','-e','/tmp/agent-test-scratch')
                result['remaining_compute_groups']=0
                result['model']=source_profile['model']
                result['passed']=True
            finally:
                for name,identifier,_ in services:
                    rows=docker('exec',identifier,'/bin/sh','-c',
                                f'if test -f /work/data/upstream-{name}.jsonl; then cat /work/data/upstream-{name}.jsonl; fi')
                    result['upstream'].extend(json.loads(line) for line in rows.splitlines())
    except Exception as error:
        result['passed']=False
        result['failure']=str(error).replace(source_profile['key'],'[REDACTED]')[:850]
        result['error_type']=type(error).__name__
        print(result['error_type'],result['failure'],flush=True)
        raise RuntimeError(result['failure']) from None
    finally:
        result['original_services']=original==set(docker('ps','--no-trunc','-q').splitlines())
        result['actual_model_http_200']=sum(r['kind']=='http' and r['status']==200 for r in result['upstream'])
        minimum_requests = 2 if args.storage == 's3' else 5
        result['passed']=result['passed'] and result['original_services'] and result['actual_model_http_200']>=minimum_requests
        data=json.dumps(result,indent=2,ensure_ascii=False)
        assert source_profile['key'] not in data
        (output/'agent-compute.json').write_text(data+'\n',encoding='utf-8')
        print('AGENT_COMPUTE_RESULT',result['passed'],result['actual_model_http_200'],result['original_services'],flush=True)
    assert result['passed']

if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--api-image', default=API_IMAGE)
    parser.add_argument('--storage', choices=('fs','s3'), default='fs')
    parser.add_argument('--shared-home', action='store_true', help='Verify account homes with a non-root API and authenticated S3')
    args = parser.parse_args()
    if args.shared_home and args.storage != 's3':
        parser.error('--shared-home requires the S3 acceptance scenario')
    main(args)
