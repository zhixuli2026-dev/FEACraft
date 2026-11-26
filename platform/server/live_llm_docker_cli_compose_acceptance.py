"""Run the formal Docker CLI routes on a disposable Linux production Compose stack."""

import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import tempfile
from traceback import extract_tb
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

from cryptography.fernet import Fernet
import httpx

from app.services import codex_docker, docker_acp
from app.services.docker_cli_state import StateVolume
from live_llm_acceptance import source
from live_llm_acp_platform_acceptance import result as acp_result
from prod_default_storage_acceptance import INFRA, call, docker, ready, register, wait


def prepare(stack, root, args, project, deployment, *, account_limit=0):
    docker('volume', 'create', project+'-cli-state')
    stack.callback(docker, 'volume', 'rm', project+'-cli-state')
    shared = docker('volume', 'inspect', '--format', '{{.Mountpoint}}', project+'-cli-state').stdout.strip()
    assert shared.startswith('/') and shared.endswith('/'+project+'-cli-state/_data')
    docker('run', '--rm', '--pull=never', '--network', 'none', '--mount',
           f'type=volume,source={project}-cli-state,target=/state', '--entrypoint', 'chmod',
           args.api_image, '0700', '/state')
    vault = root/'provider-vault.key'
    vault.write_bytes(Fernet.generate_key())
    empty = root/'empty.env'
    empty.touch()
    manifest = root/'private-compose.json'
    for artifact in (vault, empty, manifest):
        stack.callback(artifact.unlink, missing_ok=True)
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith(('VIBEFLOW_', 'COMPOSE_'))}
    env.update(PG_PASSWORD=secrets.token_hex(24), JWT_SECRET=secrets.token_hex(32), PROVIDER_VAULT_FILE=str(vault),
               VIBEFLOW_ACP_DOCKER_IMAGE=args.worker_image, VIBEFLOW_ACP_DOCKER_DEPLOYMENT=str(deployment),
               VIBEFLOW_DOCKER_CLI_CLIENT=args.docker_client.resolve(strict=True).as_posix(),
               VIBEFLOW_DOCKER_CLI_STATE_DIR=shared, VIBEFLOW_LINUX_ACCOUNT_MAX_ACTIVE_EXECUTIONS=str(account_limit))
    base = ['compose', '--env-file', str(empty), '-p', project]
    recipe = [*base, '-f', str(INFRA/'docker-compose.prod.yml'), '-f', str(INFRA/'docker-compose.docker-cli.yml')]
    settings = json.loads(docker(*recipe, 'config', '--format', 'json', env=env).stdout)
    for role, service in settings['services'].items():
        service['container_name'] = project+'-'+role
    api = settings['services']['api']
    api.pop('build')
    api['image'] = args.api_image
    api['ports'] = ['127.0.0.1::8000']
    assert api['pid'] == 'host'
    assert api['environment']['VIBEFLOW_ACP_DOCKER_ENTRYPOINTS_IN_IMAGE'] == 'true'
    docker_client = api['environment']['VIBEFLOW_DOCKER_CLI_CLIENT']
    assert any(mount['source'] == mount['target'] == shared for mount in api['volumes'])
    assert all(volume['name'].startswith(project+'_') and not volume.get('external')
               for volume in settings['volumes'].values())
    manifest.write_text(json.dumps(settings), encoding='utf8')

    def compose(*command, **options):
        return docker(*base, '-f', str(manifest), *command, env=env, **options)

    stack.callback(compose, 'down', '--volumes', '--remove-orphans')
    compose('up', '-d', '--no-build', 'postgres')
    wait(lambda: docker('inspect', project+'-postgres', '--format', '{{.State.Health.Status}}')
         .stdout.strip() == 'healthy', 'Isolated PostgreSQL was unavailable')
    compose('run', '--rm', '--no-deps', 'api', 'python', '-m', 'alembic', 'upgrade', 'head')
    compose('up', '-d', '--no-build', 'api')
    address = compose('port', 'api', '8000').stdout.strip()
    assert address.startswith('127.0.0.1:')
    client = stack.enter_context(httpx.Client(base_url='http://'+address, timeout=30, trust_env=False))
    wait(lambda: ready(client), 'Linux Compose API did not start')
    version = docker('exec', project+'-api', docker_client, 'version', '--format', '{{.Client.Os}} {{.Server.Os}}').stdout.strip()
    assert version == 'linux linux'
    assert docker('inspect', project+'-api', '--format', '{{.HostConfig.PidMode}}').stdout.strip() == 'host'
    return compose, client, shared


class Api:
    def __init__(self, client):
        self.client = client

    def call(self, method, path, body=None, *, expected=200, **kwargs):
        return call(self.client, method, path, body, expected=expected, **kwargs).json()


def query_fixture(project, query):
    return docker('exec', project+'-postgres', 'psql', '-U', 'vibeflow', '-At', '-c', query).stdout.strip()


def retire_fixture_state(compose, project, deployment, receipt):
    compose('stop', 'api')
    for table, scope in (('acp_turns', 'acp'), ('native_cli_sessions', 'codex')):
        where = " WHERE runtime='docker'" if scope == 'codex' else ''
        for identifier in query_fixture(project, f'SELECT id FROM {table}{where}').splitlines():
            suffix = UUID(identifier).hex
            label = f'label=vibeflow.{scope}.execution={deployment.hex}:{suffix}'
            containers = docker('ps', '-aq', '--no-trunc', '--filter', label).stdout.splitlines()
            relay = docker('volume', 'ls', '--format', '{{.Name}}', '--filter', label).stdout.splitlines()
            if containers or relay:
                receipt.setdefault('fixture_orphans', []).append({'scope': scope, 'containers': containers, 'relay': relay})
            for container in containers:
                docker('rm', '--force', container)
            if relay:
                assert relay == [f'vf-{scope}-{suffix}-relay']
                docker('volume', 'rm', relay[0])
    states = set()
    for row in query_fixture(project, 'SELECT id, created_by FROM acp_threads').splitlines():
        token, user = row.split('|')
        thread = UUID(token)
        states.add(StateVolume(deployment, int(user), thread, uuid5(deployment, thread.hex)))
    for row in query_fixture(project, "SELECT created_by, profile_json FROM native_cli_sessions WHERE runtime='docker'").splitlines():
        user, encoded = row.split('|', 1)
        profile = json.loads(encoded)
        runtime = codex_docker.runtime_from(profile['docker_runtime'])
        assert runtime.deployment == deployment
        states.add(codex_docker.state(runtime, int(user), profile['credential']))
    available = set(docker('volume', 'ls', '--format', '{{.Name}}').stdout.splitlines())
    for state in states:
        if state.name in available:
            state.remove(docker_acp.command)


def verify(root, args, config):
    endpoint = urlsplit(config['base_url'])
    assert endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
    project, deployment = 'vf-f58-compose-'+uuid4().hex[:10], uuid4()
    initial = set(docker('ps', '-q', '--no-trunc').stdout.splitlines())
    receipt = {'passed': False, 'project': project, 'model': config['model'], 'provider': endpoint.hostname,
               'simulated_model_calls': 0, 'checks': [], 'upstream': [], 'stops': []}

    def check(label):
        receipt['checks'].append(label)
        print('CLI_COMPOSE_PASS', label, flush=True)

    def stop_proof(stop, tool, *, interrupted=False):
        assert stop['verified'] and stop['scope_empty'] and set(stop['containers']) == {'worker', 'relay'}
        usage = stop.get('resource_usage')
        if not interrupted or usage is not None:
            assert usage and usage['container_id'] == stop['containers']['worker']['id']
            assert usage['memory_peak_bytes'] > 0
        remaining = set(docker('ps', '-aq', '--no-trunc').stdout.splitlines())
        assert not remaining.intersection(part['id'] for part in stop['containers'].values())
        requests = [event for event in stop['upstream'] if event['method'] == 'POST']
        assert requests and all(row['status'] == 200 for row in requests)
        receipt['upstream'].extend(requests)
        receipt['stops'].append({'tool': tool, 'ids': stop['containers'], 'usage': usage, 'interrupted': interrupted})
        return stop['containers']['worker']['id']

    def sql(query):
        return query_fixture(project, query)

    try:
        with ExitStack() as stack:
            compose, client, shared = prepare(stack, root, args, project, deployment)
            stack.callback(retire_fixture_state, compose, project, deployment, receipt)
            api = Api(client)
            register(client)
            identity = client.headers['Authorization']
            register(client)
            foreign = {'Authorization': client.headers['Authorization']}
            client.headers['Authorization'] = identity
            ws = api.call('GET', '/workspaces')[0]['id']
            case = api.call('POST', f'/workspaces/{ws}/cases', {'name': 'Linux Docker CLI acceptance'})['id']
            profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Official DeepSeek',
                               'base_url': 'https://api.deepseek.com', 'default_model': config['model'],
                               'api_key': config['key']})['id']
            file_route, input_file = f'/cases/{case}/file', 'ref-cli-proof.txt'
            marker = 'VF_LINUX_CLI_'+secrets.token_hex(8)
            api.call('PUT', file_route, {'path': input_file, 'content': marker})

            def conversation(tool, mode='ask'):
                return api.call('POST', '/conversations', {'case_id': case, 'title': tool,
                                'provider_profile_id': profile, 'model': config['model'], 'permission_mode': mode})['id']

            def submit_acp(harness, cid, prompt, **extra):
                return api.call('POST', f'/acp/conversations/{cid}/{harness}/turns',
                                {'provider_profile_id': profile, 'client_message_id': uuid4().hex,
                                 'content': prompt, **extra}, expected=202)

            def acp_stop(turn):
                return json.loads(sql(f"SELECT stop_json FROM acp_turns WHERE id='{UUID(turn['id'])}'"))

            def settled_acp(tool, turn, phase='completed'):
                assert turn['execution_backend'] == 'docker' and turn['runtime'] == 'linux'
                current, events = acp_result(api, turn, phase)
                stop = acp_stop(turn)
                worker = stop_proof(stop, tool, interrupted=phase == 'cancelled')
                assert current.get('resource_usage') == stop.get('resource_usage')
                return worker, events

            other_file, other_marker = 'ref-opencode-proof.txt', 'VF_OPENCODE_'+secrets.token_hex(8)
            api.call('PUT', file_route, {'path': other_file, 'content': other_marker})
            opened = {}
            for harness, path, expected in (('kimi', input_file, marker), ('opencode', other_file, other_marker)):
                cid = conversation(harness)
                turn = submit_acp(harness, cid, f'Use the file read tool to read /workspace/case/{path}. '
                                  'Reply with the exact marker it holds and remember that marker.', case_files=[path])
                opened[harness] = cid, turn, expected
            for harness, (cid, turn, expected) in opened.items():
                api.call('GET', '/acp/turns/'+turn['id'], expected=404, headers=foreign)
                worker, events = settled_acp(harness, turn)
                reply = ''.join(e['text'] for e in events if e['type'] == 'agent_message_chunk')
                assert expected in reply and ({marker, other_marker}-{expected}).isdisjoint(reply.split())
                assert any(e['type'] == 'tool_call' and e.get('kind') == 'read' for e in events)
                opened[harness] = cid, turn, worker, expected
            check('Kimi and OpenCode read isolated mounted case snapshots and release their containers')

            draft_name, draft_body = 'approved.txt', 'VF_LINUX_DRAFT_'+secrets.token_hex(8)
            draft_cid = conversation('Kimi draft')
            draft = submit_acp('kimi', draft_cid,
                               f'Use one file write tool to write exactly {draft_body} to /workspace/{draft_name}. '
                               'Call the tool now and do not retry on rejection.',
                               case_files=[input_file], allow_drafts=True)
            def wait_approval(route):
                cursor = 0

                def draft_approval():
                    nonlocal cursor
                    page = api.call('GET', route+f'/events?after={cursor}&limit=100')
                    for event in page['events']:
                        cursor = event['seq']
                        if event['type'] == 'permission':
                            return event
                    if not page['has_more'] and page['turn']['phase'] not in ('admitted', 'running'):
                        raise AssertionError(('Kimi stopped before draft approval', page['turn']['phase'],
                                              page['turn'].get('error')))
                    return None

                return wait(draft_approval, 'Kimi did not request draft approval')

            route = '/acp/turns/'+draft['id']
            permission = wait_approval(route)
            assert permission['path'] == draft_name
            ids = docker('ps', '--filter', 'label=vibeflow.acp.execution='+deployment.hex+':'+UUID(draft['id']).hex,
                         '--filter', 'name=-worker', '-q', '--no-trunc').stdout.splitlines()
            assert len(ids) == 1
            sandbox = json.loads(docker('inspect', ids[0]).stdout)[0]
            flags = sandbox['HostConfig']
            mount = next(m for m in sandbox['Mounts'] if m['Destination'] == '/workspace')
            assert mount['Source'].startswith(shared + '/.private-cli/') and mount['RW']
            assert sandbox['Config']['User'] == '65534:65534' and flags['NetworkMode'] == 'none'
            assert flags['PidMode'] != 'host'
            assert flags['ReadonlyRootfs'] and flags['PidsLimit'] == 64 and flags['Memory'] == 768*1024**2
            assert all(m['Destination'] not in ('/var/run/docker.sock', '/vf-runtime/entry.py') for m in sandbox['Mounts'])
            before = docker('exec', project+'-api', 'stat', '-c', '%a', mount['Source']+'/..',
                            check=False)
            assert before.returncode == 0 and before.stdout.strip() == '700'
            probe = docker('exec', project+'-api', 'test', '!', '-e', mount['Source']+'/'+draft_name, check=False)
            assert probe.returncode == 0
            approval = route+'/approvals/'+permission['approval_id']
            api.call('POST', approval, {'decision': 'approve'}, expected=404, headers=foreign)
            assert api.call('POST', approval, {'decision': 'approve'}) == api.call('POST', approval, {'decision': 'approve'})
            _, events = settled_acp('kimi', draft)
            assert any(e['type'] == 'draft' and e['path'] == draft_name and e['text'].strip() == draft_body for e in events)
            stat = docker('exec', project+'-api', 'stat', '-c', '%u', mount['Source']+'/'+draft_name).stdout.strip()
            assert stat == '65534'
            assert api.call('GET', file_route+'?path='+input_file)['content'] == marker
            api.call('POST', route+'/drafts:publish', {'draft': draft_name, 'path': input_file})
            assert api.call('GET', file_route+'?path='+input_file)['content'].strip() == draft_body
            check('The non-root Kimi container writes only after approval and publishes through the case API')

            def open_codex(cid):
                session = api.call('POST', '/codex/sessions', {'runtime': 'docker', 'case_id': case, 'conversation_id': cid,
                                   'provider_profile_id': profile, 'provider_wire_api': 'responses'})
                assert session['runtime'] == 'docker' and session['phase'] == 'active'
                return session

            def submit_codex(session, prompt, *, resume=False, expected=200):
                return api.call('POST', '/codex/sessions/'+session['id']+'/turns',
                                {'content': prompt, 'resume': resume, 'client_message_id': uuid4().hex,
                                 'timeout_seconds': 120, 'model_options': {'effort': 'low'}}, expected=expected)

            def run_shell(session, command, *, resume=False):
                return submit_codex(session, f'Use the shell tool to run exactly this command in /workspace: {command}. '
                                    'Use default permissions and confirm only after actual execution.', resume=resume)

            def settled_codex(turn):
                def codex_terminal():
                    state = api.call('GET', '/codex/turns/'+turn['id'])
                    if state['phase'] in ('completed', 'cancelled', 'failed', 'interrupted', 'recovery_required'):
                        return state
                    return None

                state = wait(codex_terminal, 'Codex did not finish', seconds=140)
                assert state['cleanup_verified'], state
                session = api.call('GET', '/codex/sessions/'+turn['session_id'])
                stop_proof(session['stop'], 'codex', interrupted=state['phase'] == 'interrupted')
                return state, session

            def case_text(path, *, expected=200):
                data = api.call('GET', file_route+'?path='+path, expected=expected)
                return data.get('content', '').strip()

            def reconnect():
                rebound = compose('port', 'api', '8000').stdout.strip()
                assert rebound.startswith('127.0.0.1:')
                recovered = stack.enter_context(httpx.Client(base_url='http://'+rebound, timeout=30, trust_env=False,
                                                              headers={'Authorization': identity}))
                wait(lambda: ready(recovered), 'Linux API did not return after restart')
                return Api(recovered)

            codex_cid = conversation('Codex writable case', 'approve_writes')
            session = open_codex(codex_cid)
            target = 'ref-codex-written.txt'
            turn = run_shell(session, f'cp {input_file} {target}')
            first_codex, _ = settled_codex(turn)
            assert first_codex['phase'] == 'completed' and case_text(target) == draft_body
            check('Codex executes a real file copy in the writable container and publishes the file')

            compose('restart', 'api')
            api = reconnect()
            for harness, (cid, original, original_worker, expected) in opened.items():
                repeated = submit_acp(harness, cid, 'Repeat the exact first marker from this conversation. Use no tools.',
                                      thread_id=original['thread_id'])
                worker, events = settled_acp(harness, repeated)
                assert worker != original_worker and repeated['thread_id'] == original['thread_id']
                assert expected in ''.join(e['text'] for e in events if e['type'] == 'agent_message_chunk')
            check('Both ACP threads resume in new worker containers after the Linux API restarts')

            if args.faults:
                common = 'ref-cli-shared.txt'
                api.call('PUT', file_route, {'path': common, 'content': 'initial shared file'})
                inputs = ((input_file, draft_body), (other_file, other_marker))
                contenders = [(open_codex(conversation(f'Concurrent Codex {n}', 'full_access')), source_path,
                               f'ref-concurrent-{n}.txt', expected) for n, (source_path, expected) in enumerate(inputs)]

                def workspace(session):
                    worker = session['process']['containers']['worker']
                    mounts = json.loads(docker('inspect', worker, '--format', '{{json .Mounts}}').stdout)
                    bind = next(mount for mount in mounts if mount['Destination'] == '/workspace')
                    assert bind['RW'] and bind['Source'].startswith(shared+'/.private-cli/')
                    return bind['Source']

                stage_a, stage_b = [workspace(item[0]) for item in contenders]
                assert stage_a != stage_b
                for stage in (stage_a, stage_b):
                    assert docker('exec', project+'-api', 'cat', stage+'/'+common).stdout.strip() == 'initial shared file'
                submitted = [run_shell(session, f'cp {path} {common} && cp {path} {own}')
                             for session, path, own, _ in contenders]
                winners = []
                receipt['concurrent_publication'] = []
                for trial, contender in zip(submitted, contenders, strict=True):
                    outcome, current_session = settled_codex(trial)
                    receipt['concurrent_publication'].append({'phase': outcome['phase'], 'turn_error': outcome.get('error'),
                                                              'session_error': current_session.get('error')})
                    if outcome['phase'] == 'completed':
                        winners.append(contender)
                    else:
                        assert outcome['phase'] == 'failed'
                        assert current_session['error'] == 'DockerCasePublicationConflict', receipt['concurrent_publication']
                assert len(winners) == 1, receipt['concurrent_publication']
                winner, = winners
                loser, = [item for item in contenders if item != winner]
                assert case_text(common) == case_text(winner[2]) == winner[3], 'The successful batch differs from its staged inputs'
                case_text(loser[2], expected=404)
                loser_stage = '/codex/sessions/'+loser[0]['id']+'/staging'
                receipt_stage = api.call('GET', loser_stage)
                assert receipt_stage['retained'] and {change['path'] for change in receipt_stage['changes']} == {common, loser[2]}, receipt_stage
                assert all(api.call('GET', loser_stage+'?path='+path)['content'].strip() == loser[3]
                           for path in (common, loser[2]))
                api.call('GET', loser_stage, expected=404, headers=foreign)
                check('Concurrent writable snapshots publish one whole batch and retain the conflicting batch')

                abandoned = 'abandoned.txt'
                paused = submit_acp('kimi', draft_cid,
                                    f'Call the Write tool once to write TEST_INTERRUPTION to /workspace/{abandoned}. '
                                    'Use the file tool and wait for approval.', thread_id=draft['thread_id'],
                                    case_files=[input_file])
                paused_route = '/acp/turns/'+paused['id']
                pending = wait_approval(paused_route)
                assert pending['path'] == abandoned
                acp_scope = docker('ps', '--filter', 'label=vibeflow.acp.execution='+deployment.hex+':'+UUID(paused['id']).hex,
                                   '-q', '--no-trunc').stdout.splitlines()
                assert len(acp_scope) == 2
                acp_mounts = json.loads(docker('inspect', *acp_scope).stdout)
                acp_input, = [m['Source'] for c in acp_mounts for m in c['Mounts'] if m['Destination'] == '/workspace/case']
                acp_work, = [m['Source'] for c in acp_mounts for m in c['Mounts'] if m['Destination'] == '/workspace']

                session = open_codex(codex_cid)
                private_work = workspace(session)
                partial = 'ref-interrupted-private.txt'
                handoff_marker = 'VF_HANDOFF_'+secrets.token_hex(8)
                interrupted = submit_codex(session,
                    f'Use the shell tool to run exactly this command in /workspace: cp {input_file} {partial} && sleep 50. '
                    f'Use default permissions. The marker for later handoff is {handoff_marker}.', resume=True)

                def copied():
                    result = docker('exec', project+'-api', 'cat', private_work+'/'+partial, check=False)
                    return result.returncode == 0 and result.stdout.strip() == draft_body

                wait(copied, 'Codex did not write its private work copy before interruption', seconds=75)
                case_text(partial, expected=404)
                before = api.call('GET', '/codex/turns/'+interrupted['id'])
                assert before['phase'] == 'running' and before['thread_id'] == first_codex['thread_id']
                original_scopes = set(acp_scope) | set(session['process']['containers'].values())
                assert original_scopes <= set(docker('ps', '-q', '--no-trunc').stdout.splitlines())
                container, pid = docker('inspect', project+'-api', '--format', '{{.Id}} {{.State.Pid}}').stdout.split()
                assert int(pid) > 1
                life = json.loads(sql(f"SELECT owner_identity_json FROM native_cli_sessions WHERE id='{UUID(session['id'])}'"))
                assert life['pid'] == int(pid)
                docker('kill', '--signal', 'KILL', container)
                assert docker('inspect', container, '--format', '{{.State.ExitCode}}').stdout.strip() == '137'
                assert original_scopes <= set(docker('ps', '-aq', '--no-trunc').stdout.splitlines())
                after_exit = json.loads(docker('inspect', *sorted(original_scopes)).stdout)
                receipt['api_crash'] = {'exit_code': 137, 'owner_pid': int(pid),
                                       'original_scopes': {c['Id']: c['State']['Status'] for c in after_exit}}
                compose('start', 'api')
                api = reconnect()

                settled_acp('kimi', paused, 'cancelled')
                assert acp_stop(paused)['reason'] == 'owner_exited'
                for path in (acp_input, acp_work+'/'+abandoned):
                    assert docker('exec', project+'-api', 'test', '!', '-e', path, check=False).returncode == 0
                route = '/codex/turns/'+interrupted['id']+':reconcile'
                api.call('POST', route, expected=404, headers=foreign)
                recovered = api.call('POST', route)
                assert recovered['execution_lock_recovered'] and recovered['turn']['phase'] == 'interrupted'
                assert api.call('POST', route)['execution_lock_recovered']
                _, interrupted_session = settled_codex(interrupted)
                assert interrupted_session['stop']['reason'] == 'owner_exited'
                assert original_scopes.isdisjoint(docker('ps', '-aq', '--no-trunc').stdout.splitlines())
                case_text(partial, expected=404)
                assert api.call('GET', '/codex/sessions/'+session['id']+'/staging?path='+partial)['content'].strip() == draft_body
                receipt['api_crash'].update(scopes_removed=sorted(original_scopes), approval_unwritten=True,
                                            interrupted_stage_retained=True)
                check('Linux SIGKILL recovery removes both original scopes and retains only the private Codex write')

                resumed = submit_acp('kimi', draft_cid,
                                      f'Repeat the exact content of the previously approved {draft_name}. Use no tools.',
                                      thread_id=draft['thread_id'])
                _, events = settled_acp('kimi', resumed)
                assert resumed['thread_id'] == paused['thread_id']
                assert draft_body in ''.join(e['text'] for e in events if e['type'] == 'agent_message_chunk')
                diverged = open_codex(codex_cid)
                denied = submit_codex(diverged, 'Check whether the native history can resume.', resume=True, expected=409)
                assert denied['detail']['code'] == 'input_stale'
                closed = api.call('DELETE', '/codex/sessions/'+diverged['id'])
                assert closed['cleanup_verified'] and not any(event['method'] == 'POST' for event in closed['stop']['upstream'])
                branch = api.call('POST', f'/conversations/{codex_cid}/handoff',
                                  {'before_answer_turn_id': interrupted['id']})
                assert branch['id'] != codex_cid
                new_session = open_codex(branch['id'])
                new_turn = submit_codex(new_session, 'Use only the text handoff. Reply with the exact marker left for later handoff. No tools.')
                finished, _ = settled_codex(new_turn)
                assert finished['phase'] == 'completed' and finished['thread_id'] != first_codex['thread_id']
                stream = call(api.client, 'GET', '/codex/turns/'+new_turn['id']+'/events').text
                events = [json.loads(line[5:]) for line in stream.splitlines() if line.startswith('data:')]
                assert any(event['type'] == 'context.handoff' for event in events)
                assert handoff_marker in ''.join(event['payload']['text'] for event in events if event['type'] == 'text.delta')
                check('Kimi restores its native thread and Codex continues through a verified text handoff after SIGKILL')
            receipt['passed'] = True
    except BaseException as error:
        receipt['error'] = str(error).replace(config['key'], '[REDACTED]')[:600]
        frame = extract_tb(error.__traceback__)[-1]
        receipt['error_location'] = f'{Path(frame.filename).name}:{frame.lineno}'
        raise RuntimeError(f'{type(error).__name__} at {receipt["error_location"]}: {receipt["error"]}') from None
    finally:
        receipt['original_services'] = initial == set(docker('ps', '-q', '--no-trunc').stdout.splitlines())
        receipt['passed'] = receipt['passed'] and receipt['original_services'] and not receipt.get('fixture_orphans')
        report = json.dumps(receipt, indent=2, ensure_ascii=False)
        assert config['key'] not in report
        (root/'cli-compose-result.json').write_text(report+'\n', encoding='utf8')
    assert receipt['passed']
    print('CLI_COMPOSE_RESULT', len(receipt['checks']), 'CHECKS', len(receipt['upstream']), 'REAL_HTTP_200', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--api-image', required=True)
    parser.add_argument('--worker-image', required=True)
    parser.add_argument('--docker-client', type=Path, required=True)
    parser.add_argument('--faults', action='store_true', help='Also exercise concurrent writeback and API SIGKILL recovery')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='f58-linux-compose-', dir=INFRA.parents[1]/'tmp'))
    print('CLI_COMPOSE_EVIDENCE', root, flush=True)
    verify(root, args, asyncio.run(source(args.source_profile_id)))
