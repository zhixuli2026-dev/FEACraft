"""Run the installed CLI harness with real DeepSeek and no sandbox network."""

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import time
from uuid import uuid4

from app.services.docker_cli_profile import container_flags
from app.services.docker_cli_state import StateVolume
from live_llm_acceptance import source


SERVER = Path(__file__).resolve().parent
PROJECT = SERVER.parents[1]
WORKER = SERVER / 'live_llm_docker_cli_worker.py'


def review(path):
    reviewed = json.loads(path.read_text(encoding='utf8'))
    entry = Path(__file__).resolve().relative_to(PROJECT).as_posix()
    assert reviewed['classification'] == 'real_llm' and entry in reviewed['sources']
    for relative, expected in reviewed['sources'].items():
        target = (PROJECT/relative).resolve()
        assert target.is_relative_to(PROJECT) and hashlib.sha256(target.read_bytes()).hexdigest() == expected, relative


def docker(*args, stdin=None, timeout=30):
    run = subprocess.run(['docker', *args], input=stdin, capture_output=True, encoding='utf8', timeout=timeout)
    if run.returncode:
        raise RuntimeError(f'Docker {args[0]} returned {run.returncode}: {run.stderr[:250]}')
    return run.stdout.strip()


def create(stack, *flags):
    ident = docker('create', *flags)
    assert re.fullmatch(r'[0-9a-f]{64}', ident)
    stack.callback(docker, 'rm', '--force', ident)
    return ident


def worker_command(image, mode):
    mount = f'type=bind,source={WORKER.as_posix()},target=/tmp-sandbox-worker.py,readonly'
    return ['--mount', mount, '--entrypoint', 'python', image, '-I', '/tmp-sandbox-worker.py', mode]


def relay_log(ident):
    return [json.loads(line) for line in docker('logs', ident).splitlines() if line.startswith('{')]


def wait_until(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if result := predicate():
            return result
        time.sleep(.15)
    raise TimeoutError('Docker relay did not announce readiness')


def check_persistence(stack, image, sandbox_ids, state, first, run, model_responses):
    different_owner = replace(state, user_id=state.user_id+1, generation=uuid4())
    different_owner.provision(docker, image)
    stack.callback(different_owner.remove, docker)
    assert different_owner.name != state.name

    try:
        replace(state, generation=uuid4()).mount(docker)
    except ValueError:
        pass
    else:
        raise AssertionError('State volume attached with a different generation')

    native_id = first['native_thread_id']
    assert native_id and first['reply_marker_sha256']
    before = len(model_responses())
    wrong_container, wrong_owner = run(different_owner, resume_id=native_id)
    assert wrong_owner['exit_code'] != 0 and not wrong_owner['native_thread_id']
    assert len(model_responses()) == before

    waiting = create(stack,*container_flags(),'--mount',state.mount(docker), '--entrypoint','/bin/sleep',image,'60')
    sandbox_ids.append(waiting)
    docker('start',waiting)
    assert docker('inspect','--format','{{.State.Running}}',waiting) == 'true'
    docker('kill','--signal','KILL',waiting)
    assert docker('wait',waiting) == '137'

    resumed_container, resumed = run(state, resume_id=native_id)
    assert resumed['exit_code'] == 0 and resumed['native_thread_id'] == native_id
    assert resumed['resume_without_tools'] and resumed['prior_case_absent']
    assert resumed['reply_marker_sha256'] == first['reply_marker_sha256']
    after = len(model_responses())
    assert after > before
    return {'same_native_thread':True, 'reply_matches':True, 'previous_case_not_mounted':True,
            'new_container':resumed_container, 'other_owner_container':wrong_container,
            'other_owner_refused':True, 'other_owner_model_requests':0,'wrong_generation_refused':True,
            'interrupted_scope_exit':137,'resumed_model_requests':after-before}


def main(image, profile_id, report_path, harness, persistent):
    cfg = asyncio.run(source(profile_id))
    assert cfg['provider'] == 'deepseek' and cfg['base_url'].rstrip('/') in {
        'https://api.deepseek.com', 'https://api.deepseek.com/v1',
    }
    original = docker('ps','--no-trunc','--format','{{.ID}} {{.Names}}').splitlines()
    ident = docker('image','inspect','--format','{{.Id}}',image)
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', ident)
    volume = 'vf-cli-relay-' + secrets.token_hex(12)
    token = secrets.token_hex(32)
    proof = 'VIBE_CASE_' + secrets.token_hex(16)
    sandbox_ids = []
    state = StateVolume(uuid4(), 1, uuid4(), uuid4()) if persistent else None
    result = {'provider':'api.deepseek.com','model':cfg['model'],'harness':harness,'image':ident,'relay_volume':volume,
              'mock_llm_calls':0,'passed':False}
    try:
        with ExitStack() as stack:
            result['offline'] = json.loads(docker('run','--rm',*container_flags(),*worker_command(ident,'probe')))
            docker('volume','create',volume)
            stack.callback(docker,'volume','rm',volume)
            mount = f'type=volume,source={volume},target=/relay'
            docker('run','--rm','--pull=never','--network','none','--user','0:0','--read-only',
                   '--cap-drop','ALL','--cap-add','CHOWN','--mount',mount,'--entrypoint','chown',ident,'65534:65534','/relay')
            gate = create(stack,'--interactive','--pull=never','--network','bridge','--user','65534:65534',
                          '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges=true',
                          '--mount',mount,*worker_command(ident,'relay'))
            sandbox_ids.append(gate)
            relay = subprocess.Popen(['docker','start','-ai',gate], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, encoding='utf8')

            def stop_relay():
                try:
                    docker('stop','--time','1',gate,timeout=15)
                finally:
                    try:
                        relay.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        relay.kill()
                        relay.wait(timeout=5)

            stack.callback(stop_relay)
            with relay.stdin as stream:
                stream.write(json.dumps({'provider_key':cfg['key'], 'capability':token,'model':cfg['model']})+'\n')
            def ready():
                if relay.poll() is not None:
                    raise RuntimeError('Relay exited during initialization')
                return any(row.get('relay') == 'ready' for row in relay_log(gate))
            wait_until(ready)

            if state is not None:
                state.provision(docker, ident)
                stack.callback(state.remove, docker)

            def run(selected, **payload):
                state_mount = ['--mount',selected.mount(docker)] if selected else []
                worker = create(stack,'--interactive',*container_flags(),'--mount',mount+',readonly',
                                *state_mount,*worker_command(ident,harness))
                sandbox_ids.append(worker)
                assert docker('inspect','--format','{{.HostConfig.NetworkMode}}',worker) == 'none'
                result['docker_network_disabled'] = True
                raw = docker('start','-ai',worker,timeout=200,stdin=json.dumps({
                    'capability':token,'model':cfg['model'],'persistent':bool(selected),**payload}))
                assert docker('inspect','--format','{{.State.Status}}',worker) == 'exited'
                return worker, json.loads(raw)

            def model_responses():
                return [row for row in relay_log(gate) if row.get('method') == 'POST']

            try:
                job, result['cli'] = run(state, proof=proof)
                if persistent:
                    result['persistence'] = check_persistence(stack, ident, sandbox_ids, state, result['cli'], run, model_responses)
                    assert result['persistence']['new_container'] != job
            finally:
                result['model_http'] = model_responses()
            result['job_remained_exited'] = (docker('inspect','--format','{{.State.Status}}',job) == 'exited')

            waiting = create(stack,*container_flags(), '--entrypoint','/bin/sleep',ident,'60')
            sandbox_ids.append(waiting)
            docker('start',waiting)
            assert docker('inspect','--format','{{.State.Running}}',waiting) == 'true'
            docker('kill','--signal','KILL',waiting)
            result['cancelled_container_exit'] = int(docker('wait',waiting))
            assert result['cancelled_container_exit'] == 137
            assert result['job_remained_exited'] and len(result['model_http']) >= 2
            assert all(row.get('status') == 200 and row['complete'] for row in result['model_http'])
            assert any(row['stream'] and row['parts'] > 1 for row in result['model_http'])
            assert result['cli']['exit_code'] == 0 and result['cli']['tool_read']
            assert result['cli']['reply_contains_proof'] and result['cli']['input_preserved']
            result['passed'] = True
    finally:
        result['real_model_calls'] = len(result.get('model_http', []))
        result['original_containers_match'] = set(original) == set(docker('ps','--no-trunc','--format','{{.ID}} {{.Names}}').splitlines())
        result['relay_volume_removed'] = volume not in docker('volume','ls','--format','{{.Name}}').splitlines()
        result['sandbox_containers_removed'] = not set(sandbox_ids).intersection(docker('ps','-aq','--no-trunc').splitlines())
        if persistent:
            state_volumes = (state, replace(state, user_id=2))
            outstanding = set(docker('volume','ls','--format','{{.Name}}').splitlines())
            result['test_state_volumes_removed'] = not outstanding.intersection(item.name for item in state_volumes)
            result['passed'] = result['passed'] and result['test_state_volumes_removed']
        result['passed'] = (result['passed'] and result['original_containers_match'] and result['relay_volume_removed']
                            and result['sandbox_containers_removed'])
        encoded = json.dumps(result, indent=2) + '\n'
        assert cfg['key'] not in encoded and token not in encoded and proof not in encoded
        report_path.write_text(encoded, encoding='utf8')
        print('DOCKER_CLI_SANDBOX_EVIDENCE=' + str(report_path), flush=True)
    assert result['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('review', type=Path)
    parser.add_argument('report', type=Path)
    parser.add_argument('--image', default='vibeflow/api:cli-discovery-20261118')
    parser.add_argument('--profile', type=int, default=26)
    parser.add_argument('--harness', choices=('codex','opencode','kimi'), default='codex')
    parser.add_argument('--persistent', action='store_true')
    arguments = parser.parse_args()
    if arguments.persistent and arguments.harness != 'codex':
        parser.error('persistent native resume acceptance uses Codex')
    review(arguments.review)
    main(arguments.image, arguments.profile, arguments.report, arguments.harness, arguments.persistent)
