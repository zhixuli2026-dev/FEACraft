"""Exercise the shared Linux account limit with actual Docker CLI model turns."""
import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from traceback import extract_tb
from uuid import UUID, uuid4

import httpx

from live_llm_acceptance import source
from live_llm_acp_platform_acceptance import result as acp_result
from live_llm_docker_cli_compose_acceptance import Api, prepare, query_fixture, retire_fixture_state
from prod_default_storage_acceptance import INFRA, docker, register, wait


LIMIT = 'linux_account_execution_limit'


def denied(response):
    assert response['detail']['code'] == LIMIT and response['detail']['limit'] == 1, response


def account(api, profile):
    register(api.client)
    workspace, = api.call('GET','/workspaces')
    cases = [api.call('POST',f'/workspaces/{workspace["id"]}/cases',{'name':f'CLI quota case {i}'})['id']
             for i in (1, 2)]
    credential = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Official DeepSeek',
                          'base_url':profile['base_url'],'default_model':profile['model'],'api_key':profile['key']})['id']
    return cases, credential


def conversation(api, case, title, credential, profile):
    return api.call('POST','/conversations',{'case_id':case,'title':title,'provider_profile_id':credential,
                    'model':profile['model'],'permission_mode':'ask'})['id']


def stop_proof(record, evidence, label, *, used_model=True):
    assert record['verified'] and record['scope_empty'] and set(record['containers'])=={'worker','relay'}, record
    remaining = set(docker('ps','-aq','--no-trunc').stdout.splitlines())
    assert remaining.isdisjoint(value['id'] for value in record['containers'].values())
    requests = [row for row in record['upstream'] if row['method']=='POST']
    if used_model:
        assert requests and all(row['status']==200 and row['complete'] for row in requests), requests
        usage = record['resource_usage']
        assert usage['container_id']==record['containers']['worker']['id']
        assert usage['cpu_usage_usec']>0 and usage['memory_peak_bytes']>0
    evidence['upstream'].extend(requests)
    evidence['stops'][label] = {'verified':True,'containers':record['containers'],
                                'usage':record.get('resource_usage')}


def approval_request(api, route):
    cursor = 0

    def permission():
        nonlocal cursor
        page = api.call('GET',route+'/events',params={'after':cursor,'limit':100})
        for event in page['events']:
            cursor = event['seq']
            if event['type']=='permission':
                return event
        assert page['turn']['phase'] in {'admitted','running'} or page['has_more'], page['turn']
        return None

    return wait(permission,'Kimi did not ask for write approval',seconds=100)


def verify_account_usage(api, peer, evidence):
    mine = api.call('GET','/runtime/execution-usage',params={'days':1})
    other = peer.call('GET','/runtime/execution-usage',params={'days':1})
    stops = evidence['stops']
    mine_samples = [stops[name]['usage'] for name in ('codex','codex_reopened','kimi')]
    peer_sample = stops['opencode_independent_account']['usage']
    assert mine['by_kind']['codex']['scopes']==2 and mine['by_kind']['acp']['scopes']==1
    job = mine['by_kind']['job']
    assert (job['scopes'],job['verified_stops'],job['cpu_samples'],job['memory_samples'])==(1,1,0,0),job
    assert job['cpu_used_ns'] is None and job['max_execution_memory_peak_bytes'] is None
    assert mine['total']['cpu_used_ns']==1000*sum(sample['cpu_usage_usec'] for sample in mine_samples)
    assert mine['total']['max_execution_memory_peak_bytes']==max(sample['memory_peak_bytes'] for sample in mine_samples)
    assert other['total']['scopes']==other['by_kind']['acp']['scopes']==1
    assert other['total']['cpu_used_ns']==1000*peer_sample['cpu_usage_usec']
    assert other['total']['max_execution_memory_peak_bytes']==peer_sample['memory_peak_bytes']
    evidence['account_usage'] = {'owner':mine,'other':other}


def verify(api, peer, profile, project, evidence):
    (first, second), credential = account(api,profile)
    (other_case, _), other_credential = account(peer,profile)
    codex_cid = conversation(api,first,'Codex reservation',credential,profile)
    kimi_cid = conversation(api,second,'Kimi approval',credential,profile)
    peer_cid = conversation(peer,other_case,'Independent OpenCode',other_credential,profile)
    codex = {'runtime':'docker','provider_profile_id':credential,'provider_wire_api':'responses'}
    request_path = f'/codex/conversations/{codex_cid}/session-requests'
    requested = {**codex,'purpose':'chat','client_request_id':uuid4().hex}
    reservation = api.call('POST',request_path,requested)
    assert reservation['phase']=='admitted' and reservation['session'] is None,reservation
    assert api.call('POST',request_path,requested)['id']==reservation['id']
    evidence['startup_request'] = reservation['id']

    second_request_path = f'/codex/conversations/{kimi_cid}/session-requests'
    denied(api.call('POST',second_request_path,{**requested,'client_request_id':uuid4().hex},expected=429))
    evidence['second_startup_rejected_at'] = api.call('GET','/codex/session-requests/'+reservation['id'])['phase']

    kimi_path = f'/acp/conversations/{kimi_cid}/kimi/turns'
    draft = 'account-proof.txt'
    draft_marker = 'KIMI_CAPACITY_'+uuid4().hex
    kimi_body = {'provider_profile_id':credential,'client_message_id':uuid4().hex,'allow_drafts':True,
                 'content':f'Use one file write tool to write exactly {draft_marker} to /workspace/{draft}. '
                           'Call the tool now and do not retry on rejection.'}
    def ready_session():
        row = api.call('GET','/codex/session-requests/'+reservation['id'])
        if row['phase'] in {'admitted','starting'}:
            return None
        assert row['phase']=='completed' and row['session']['phase']=='active', (
            row['phase'],row['error'],(row['session'] or {}).get('error'))
        return row['session']

    session = wait(ready_session,'Codex session did not initialize',seconds=105)
    assert session['id']==reservation['id'] and session['runtime']=='docker'
    denied(api.call('POST',kimi_path,kimi_body,expected=429))
    denied(api.call('POST','/codex/sessions',{**codex,'case_id':second,'conversation_id':kimi_cid},expected=429))
    job = api.call('POST',f'/cases/{second}/jobs',{'command':"printf 'ACCOUNT_SLOT_RELEASED\\n'",'timeout_seconds':30})
    assert job['attempt']['phase']=='queued',job

    other_marker = 'OPENCODE_CAPACITY_'+uuid4().hex
    other_file = 'ref-account-proof.txt'
    peer.call('PUT',f'/cases/{other_case}/file',{'path':other_file,'content':other_marker})
    other = peer.call('POST',f'/acp/conversations/{peer_cid}/opencode/turns',{
        'provider_profile_id':other_credential,'client_message_id':uuid4().hex,'case_files':[other_file],
        'content':f'Use the read tool on /workspace/case/{other_file} and reply with the exact marker.'},expected=202)
    _, events = acp_result(peer,other,'completed')
    assert other_marker in ''.join(e['text'] for e in events if e['type']=='agent_message_chunk')
    stop = json.loads(query_fixture(project,f"SELECT stop_json FROM acp_turns WHERE id='{UUID(other['id'])}'"))
    stop_proof(stop,evidence,'opencode_independent_account')
    job_path = f'/jobs/{job["id"]}'
    assert api.call('GET',job_path)['attempt']['phase']=='queued'
    print('CLI_ACCOUNT_PASS independent account ran OpenCode with Codex holding the first slot',flush=True)

    session_path = '/codex/sessions/'+session['id']
    reply_marker = 'CODEX_CAPACITY_'+uuid4().hex
    turn = api.call('POST',session_path+'/turns',{'content':f'Reply with exactly {reply_marker}. No tools.',
                    'client_message_id':uuid4().hex,'timeout_seconds':120,'model_options':{'effort':'low'}})

    def codex_done():
        row = api.call('GET','/codex/turns/'+turn['id'])
        if row['phase'] not in {'completed','cancelled','failed','interrupted','recovery_required'}:
            return None
        assert row['phase']=='completed' and row['cleanup_verified'],row
        return row

    wait(codex_done,'Codex did not complete a real model turn',seconds=140)
    history = api.client.get('/api/v1/codex/turns/'+turn['id']+'/events')
    history.raise_for_status()
    events = [json.loads(line[5:]) for line in history.text.splitlines() if line.startswith('data:')]
    assert reply_marker in ''.join(event['payload']['text'] for event in events if event['type']=='text.delta')
    stop_proof(api.call('DELETE',session_path)['stop'],evidence,'codex')

    def job_done():
        row = api.call('GET',job_path)
        if row['ended_at'] is None:
            return None
        stop = row['attempt']['process_stop']
        assert row['status']=='succeeded' and row['exit_code']==0 and stop['verified'] and stop['scope_empty'], (
            row['status'],row['attempt']['reason_code'],row['log_backlog'][-480:])
        assert 'ACCOUNT_SLOT_RELEASED' in row['log_backlog']
        return row['id']

    evidence['queued_job_ran_after_codex'] = wait(job_done,'Queued job remained blocked after Codex exited')

    kimi = api.call('POST',kimi_path,kimi_body,expected=202)
    kimi_route = '/acp/turns/'+kimi['id']
    approval = approval_request(api,kimi_route)
    assert approval['path']==draft
    denied(api.call('POST',request_path,{**requested,'client_request_id':uuid4().hex},expected=429))
    evidence['codex_rejected_while_kimi_approval_pending'] = True
    api.call('POST',kimi_route+'/approvals/'+approval['approval_id'],{'decision':'approve'})
    _, events = acp_result(api,kimi,'completed')
    assert any(event['type']=='draft' and event['path']==draft and event['text'].strip()==draft_marker for event in events)
    stop = json.loads(query_fixture(project,f"SELECT stop_json FROM acp_turns WHERE id='{UUID(kimi['id'])}'"))
    stop_proof(stop,evidence,'kimi')
    reopened = api.call('POST','/codex/sessions',{**codex,'case_id':first,'conversation_id':codex_cid})
    assert reopened['phase']=='active' and reopened['id']!=session['id']
    stop_proof(api.call('DELETE','/codex/sessions/'+reopened['id'])['stop'],evidence,'codex_reopened',used_model=False)
    verify_account_usage(api,peer,evidence)


def main(args):
    profile = asyncio.run(source(args.source_profile_id))
    root = Path(tempfile.mkdtemp(prefix='j16-cli-account-capacity-',dir=INFRA.parents[1]/'tmp'))
    print('CLI_ACCOUNT_EVIDENCE',root,flush=True)
    project, deployment = 'vf-cli-account-'+uuid4().hex[:10], uuid4()
    baseline = set(docker('ps','-q','--no-trunc').stdout.splitlines())
    evidence = {'passed':False,'limit':1,'model':profile['model'],'provider':'api.deepseek.com',
                'upstream':[],'stops':{},'simulated_model_calls':0}
    try:
        with ExitStack() as stack:
            compose, client, _ = prepare(stack,root,args,project,deployment,account_limit=1)
            stack.callback(retire_fixture_state,compose,project,deployment,evidence)
            peer = stack.enter_context(httpx.Client(base_url=client.base_url,timeout=30,trust_env=False))
            verify(Api(client),Api(peer),profile,project,evidence)
            evidence['passed'] = True
    except Exception as error:
        frame = extract_tb(error.__traceback__)[-1]
        evidence['failure'] = (f'{type(error).__name__} at {Path(frame.filename).name}:{frame.lineno}: '
                               +str(error).replace(profile['key'],'[REDACTED]')[:700])
    finally:
        evidence['original_services'] = baseline==set(docker('ps','-q','--no-trunc').stdout.splitlines())
        evidence['actual_model_http_200'] = sum(row['status']==200 for row in evidence['upstream'])
        evidence['passed'] = evidence['passed'] and evidence['original_services'] and not evidence.get('fixture_orphans')
        (root/'result.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
    assert evidence['passed'],evidence.get('failure') or evidence.get('fixture_orphans')
    print('CLI_ACCOUNT_RESULT',evidence['passed'],evidence['actual_model_http_200'],flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--api-image',required=True)
    parser.add_argument('--worker-image',required=True)
    parser.add_argument('--docker-client',type=Path,required=True)
    main(parser.parse_args())
