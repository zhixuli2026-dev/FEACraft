"""Verify account admission across Linux replicas, worker and actual DeepSeek Agent calls."""

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack
import json
from pathlib import Path
import tempfile
import traceback

import aiohttp

from live_llm_acceptance import source
from live_llm_agent_shell_linux_compute_acceptance import prepare, stream
from linux_openfoam_isolation_acceptance import finished
from linux_terminal_compute_acceptance import account, close_pty, command, open_pty, output, request
from mixed_load_test_support import API_IMAGE, WEB_IMAGE, Deployment, docker


ROOT = Path(__file__).resolve().parents[2]
CODE = 'linux_account_execution_limit'
GRID = '''<?xml version="1.0"?>
<VTKFile type="PolyData" version="0.1" byte_order="LittleEndian">
  <PolyData>
    <Piece NumberOfPoints="3" NumberOfVerts="0" NumberOfLines="0" NumberOfStrips="0" NumberOfPolys="1">
      <Points><DataArray type="Float32" NumberOfComponents="3" format="ascii">0 0 0  1 0 0  0 1 0</DataArray></Points>
      <Polys>
        <DataArray type="Int32" Name="connectivity" format="ascii">0 1 2</DataArray>
        <DataArray type="Int32" Name="offsets" format="ascii">3</DataArray>
      </Polys>
    </Piece>
  </PolyData>
</VTKFile>
'''
PROGRAM = '''from pathlib import Path
print(Path('Ref/proof.txt').read_text().strip())
'''
BIBLIOGRAPHY = {'bibtex':'@article{sample, author={Alice Sample}, title={Verified document}, '
                        'year={2020}, journal={Sample Journal}}','keys':['sample'],'preset':'ieee'}


async def submit(client, endpoint, owner, command_text):
    token, case = owner
    return await request(client, endpoint, 'POST', f'/cases/{case}/jobs', token=token,
                         json={'command': command_text, 'timeout_seconds': 90})


def denied(message):
    assert message['type'] == 'error' and message['code'] == CODE and message['status'] == 429, message


async def approve(client, run, *, blocked):
    events = await stream(client, run['endpoint'], run['approval']+':approve', run['token'], {})
    result, = (event for event in events if event['type']=='tool.result')
    assert result['approved'] is True
    endpoint = run['approval']+'/execution'
    if blocked:
        assert not result['ok'] and result['code']==CODE, result
        await request(client, run['endpoint'], 'GET', endpoint, token=run['token'], expected=404)
        return {'code': CODE, 'execution_reserved': False}
    assert result['ok'] and run['marker'] in result['result'], result
    record = await request(client, run['endpoint'], 'GET', endpoint, token=run['token'])
    child, = record['children']['children'].values()
    assert record['phase']=='succeeded' and child['stop']['verified'] and child['stop']['scope_empty'], record
    return {'id': record['id'], 'stop': child['stop']}


async def verify_metered_usage(client, api, peer, token, other, stopped, evidence):
    usage_route = '/runtime/execution-usage'
    owner, foreign = await asyncio.gather(request(client,peer,'GET',usage_route,token=token,params={'days':1}),
                                         request(client,api,'GET',usage_route,token=other[0],params={'days':1}))
    expected = {
        'terminal': [evidence['terminal_cleanup']['linux_compute_usage'], stopped['stop']['linux_compute_usage']],
        'visualization': [evidence['renderer_cleanup']['linux_compute_usage']],
        'agent_shell': [evidence['agent_allowed']['stop']['linux_compute_usage']],
        'native_document': [evidence['citation_compute']],
        'job': [evidence['cancelled_worker_job']['compute']],
    }
    for kind, samples in expected.items():
        record = owner['by_kind'][kind]
        assert record['scopes']==record['verified_stops']==record['cpu_samples']==record['memory_samples']==len(samples),record
        assert record['cpu_used_ns']==sum(sample['usage']['cpu_used_ns'] for sample in samples)
        assert record['max_execution_memory_peak_bytes']==max(sample['usage']['memory_peak_bytes'] for sample in samples)
    flat = [sample['usage'] for samples in expected.values() for sample in samples]
    assert owner['total']['cpu_used_ns']==sum(sample['cpu_used_ns'] for sample in flat)
    assert owner['total']['max_execution_memory_peak_bytes']==max(sample['memory_peak_bytes'] for sample in flat)
    assert owner['total']['scopes']==6
    foreign_sample = evidence['independent_job_compute']['usage']
    assert foreign['total']['scopes']==foreign['by_kind']['job']['scopes']==1
    assert foreign['total']['cpu_used_ns']==foreign_sample['cpu_used_ns']
    assert foreign['total']['max_execution_memory_peak_bytes']==foreign_sample['memory_peak_bytes']
    evidence['account_usage'] = {'owner':owner,'other':foreign}


async def verify(api, peer, profile, scratch, evidence, *, postgres):
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(aiohttp.ClientSession(trust_env=False,
                                               timeout=aiohttp.ClientTimeout(total=80)))
        owner, other = await asyncio.gather(account(client, api), account(client, peer))
        token, case = owner
        path = '/cases/'+str(case)
        workspace, = await request(client,api,'GET','/workspaces',token=token)
        second_case = (await request(client,peer,'POST',f'/workspaces/{workspace["id"]}/cases',token=token,
                                     json={'name':'Separate case for cross-replica admission'}))['id']
        grid = 'D-post/account-limit.vtp'
        await request(client, api, 'PUT', path+'/file', token=token, json={'path':grid,'content':GRID})
        viz_body = {'file_path':grid,'engine':'paraview'}

        clients = await asyncio.gather(open_pty(stack,client,api,token,case),
                                       open_pty(stack,client,peer,token,second_case))
        ready = [(host,socket,text) for host,(socket,text) in zip((api,peer),clients) if text['type']=='ready']
        (winner, socket, active), = ready
        _, denied_text = next((connection, text) for connection,text in clients if text['type']!='ready')
        denied(denied_text)
        await command(socket, "printf 'CROSS_%s\\n' REPLICA_OK")
        await output(socket, br'[\r\n]CROSS_REPLICA_OK\r?\n')
        evidence['simultaneous_terminals'] = {'admitted':1,'denied':1,'session':active['session'],
                                              'cases':[case,second_case]}

        rejected = await request(client,peer,'POST',path+'/viz:sessions',token=token,expected=429,json=viz_body)
        assert rejected['detail']['code']==CODE
        citation_path = f'/cite/cases/{case}/format'
        rejected = await request(client,peer,'POST',citation_path,token=token,expected=429,json=BIBLIOGRAPHY)
        assert rejected['detail']['code']==CODE
        evidence['citation_blocked'] = rejected['detail']['code']
        queued = await submit(client, peer, owner, "printf 'ACCOUNT_WORKER_STARTED\\n'; sleep 60")
        route = f'/jobs/{queued["id"]}'
        assert queued['attempt']['phase']=='queued',queued
        independent = await submit(client, api, other, "printf 'INDEPENDENT_ACCOUNT_FINISHED\\n'")
        independent = await finished(client,peer,other[0],independent['id'],scratch)
        assert 'INDEPENDENT_ACCOUNT_FINISHED' in independent['log_backlog']
        waiting = await request(client,peer,'GET',route,token=token)
        assert waiting['attempt']['phase']=='queued' and waiting['attempt']['process_identity'] is None, waiting
        evidence['queued_before_release'] = queued['id']
        evidence['independent_account_job'] = independent['id']
        if evidence['metered']:
            evidence['independent_job_compute'] = independent['attempt']['linux_compute_usage']
        terminal = await close_pty(client,winner,token,active)
        evidence['terminal_cleanup'] = terminal['stop']

        async with asyncio.timeout(35):
            while True:
                running = await request(client,peer,'GET',route,token=token)
                attempt = running['attempt']
                evidence['worker_after_terminal_stop'] = {'phase':attempt['phase'],
                                                          'process_registered':attempt['process_identity'] is not None}
                if attempt['phase']=='running' and attempt['process_identity']:
                    break
                assert running['ended_at'] is None,running
                await asyncio.sleep(.25)
        _, text = await open_pty(stack,client,api,token,second_case)
        denied(text)
        assert (await request(client,peer,'POST',route+':cancel',token=token))['ok']
        cancelled = await finished(client,api,token,queued['id'],scratch,expected='cancelled')
        stop = cancelled['attempt']['process_stop']
        assert stop['verified'] and stop['scope_empty']
        evidence['cancelled_worker_job'] = {'id':queued['id'],'stop':stop}
        if evidence['metered']:
            evidence['cancelled_worker_job']['compute'] = cancelled['attempt']['linux_compute_usage']

        renderer = await request(client,peer,'POST',path+'/viz:sessions',token=token,json=viz_body)
        assert renderer['phase']=='active' and renderer['ready'] is not None,renderer
        _, text = await open_pty(stack,client,api,token,case)
        denied(text)
        blocked = await prepare(client,api,profile,'Quota occupied by ParaView',PROGRAM,owner=owner)
        evidence['agent_blocked'] = await approve(client,blocked,blocked=True)
        await request(client,api,'DELETE',f'/viz:sessions/{renderer["id"]}',token=token)
        renderer = await request(client,peer,'GET',f'/viz:sessions/{renderer["id"]}',token=token)
        assert renderer['phase']=='closed' and renderer['stop']['verified'] and renderer['stop']['scope_empty'],renderer
        evidence['renderer_cleanup'] = renderer['stop']

        allowed = await prepare(client,api,profile,'Quota released by ParaView',PROGRAM,owner=owner)
        evidence['agent_allowed'] = await approve(client,allowed,blocked=False)
        citation = await request(client,peer,'POST',citation_path,token=token,json=BIBLIOGRAPHY)
        assert 'Verified document' in citation['html'] and 'Sample' in citation['html']
        evidence['citation_after_release'] = True
        _, resumed = await open_pty(stack,client,peer,token,case)
        assert resumed['type']=='ready',resumed
        stopped = await close_pty(client,peer,token,resumed)
        evidence['reused_after_agent_stop'] = resumed['session']
        if evidence['metered']:
            row = await asyncio.to_thread(docker,'exec',postgres,'psql','-U','postgres','-d','replicas','-Atc',
                f"SELECT children_json FROM native_requests WHERE case_id={int(case)} AND kind='report.prepare'")
            child, = json.loads(row)['children'].values()
            assert child['purpose']=='pandoc_cite' and child['stop']['verified']
            evidence['citation_compute'] = child['stop']['linux_compute_usage']
            await verify_metered_usage(client,api,peer,token,other,stopped,evidence)


def main(args):
    profile = asyncio.run(source(args.source_profile_id))
    scratch = Path(tempfile.mkdtemp(prefix='j16-account-capacity-', dir=ROOT/'tmp'))
    print('ACCOUNT_CAPACITY_EVIDENCE',scratch,flush=True)
    baseline = set(docker('ps','-q','--no-trunc').splitlines())
    evidence = {'passed':False,'storage':args.storage,'provider':'api.deepseek.com',
                'model':profile['model'],'limit':1,'metered':args.metered,'simulated_model_calls':0,'upstream':[]}
    try:
        with ExitStack() as stack:
            deployment = Deployment(stack,scratch,args.api_image,WEB_IMAGE)
            docker('stop',deployment.worker)
            if args.storage=='s3':
                deployment.configure_s3(scratch)
            env = {'VIBEFLOW_LINUX_ACCOUNT_MAX_ACTIVE_EXECUTIONS':'1',
                   'VIBEFLOW_VAULT_KEY_PATH':'/work/data/live-vault'}
            if args.metered:
                deployment.runtime[:] = ['0:0' if entry=='65534:65534' else entry for entry in deployment.runtime]
                deployment.runtime.extend(('--cgroupns','private','--cap-add','SYS_ADMIN'))
                for kind in ('EXECUTION','TERMINAL_SESSION','VIZ_SESSION','AGENT_SHELL'):
                    env[f'VIBEFLOW_{kind}_LINUX_CPU_LIMIT_SECONDS'] = '30'
                    env[f'VIBEFLOW_{kind}_LINUX_MEMORY_LIMIT_MB'] = '768'
            deployment.env.update(env)
            deployment.flags.extend(flag for name in env for flag in ('-e',name))
            deployment.worker = deployment.start_worker(deployment.label+'-limited-worker')
            docker('exec',deployment.worker,'/usr/local/bin/python','-B','-c',
                   'from cryptography.fernet import Fernet; from pathlib import Path; '
                   'Path("/work/data/live-vault").write_bytes(Fernet.generate_key()); '
                   'Path("/work/data/llm-allowance").write_text("12")')
            deployment.runtime.extend(('--sysctl','net.ipv6.conf.lo.disable_ipv6=0'))
            api_container, api = deployment.api(deployment.a)
            peer_container, peer = deployment.api(deployment.b)
            try:
                asyncio.run(verify(api,peer,profile,scratch,evidence,postgres=deployment.label+'-postgres'))
                evidence['passed'] = True
            finally:
                for name,container in ((deployment.a,api_container),(deployment.b,peer_container)):
                    rows = docker('exec',container,'/bin/sh','-c',
                                  f'if test -f /work/data/upstream-{name}.jsonl; then cat /work/data/upstream-{name}.jsonl; fi')
                    evidence['upstream'].extend(json.loads(line) for line in rows.splitlines())
    except Exception as error:
        last = traceback.extract_tb(error.__traceback__)[-1]
        detail = str(error).replace(profile['key'],'[REDACTED]')[:1200]
        evidence['failure'] = f'{type(error).__name__} at {Path(last.filename).name}:{last.lineno}: {detail}'
    finally:
        evidence['original_services'] = baseline==set(docker('ps','-q','--no-trunc').splitlines())
        evidence['actual_model_http_200'] = sum(row['kind']=='http' and row['status']==200 for row in evidence['upstream'])
        evidence['passed'] = evidence['passed'] and evidence['original_services'] and evidence['actual_model_http_200']>=2
        (scratch/'result.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
        print('ACCOUNT_CAPACITY_RESULT',evidence['passed'],evidence['actual_model_http_200'],flush=True)
    assert evidence['passed'], evidence.get('failure')


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',required=True,type=int)
    parser.add_argument('--storage',required=True,choices=('fs','s3'))
    parser.add_argument('--metered',action='store_true',help='Verify the account totals against live Linux cgroups')
    parser.add_argument('--api-image',default=API_IMAGE)
    main(parser.parse_args())
