"""Exercise queued/running API loss on the disposable PG/S3 restore stack."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import secrets
import shlex
import subprocess
import tempfile
from threading import Barrier
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[2]
PROJECT = 'vf-restore-20261118'
SERVICES = ('postgres', 'redis', 'seaweedfs', 'api', 'web', 'arq-worker')


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=150).strip()


def name(service):
    return PROJECT + '-' + service


def field(service, template):
    return docker('inspect', '--format', template, name(service))


def wait(check, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.5)
    raise TimeoutError(check.__name__)


REDIS_DELIVERY = """
import asyncio, json, sys
from arq import create_pool
from arq.jobs import Job
from arq.worker import func
from app.services.distributed_queue import redis_settings, run_delivery
from app.services.distributed_worker import create_worker

async def main():
    operation, attempt, job = sys.argv[1:]
    pool = await create_pool(redis_settings)
    try:
        if operation == 'original':
            item = await Job('vf-delivery-'+attempt, pool).info()
            print(json.dumps(bool(item and item.function=='run_delivery' and item.args==(int(job),))))
            return
        queue = 'vf-cancel-'+attempt
        message = await pool.enqueue_job('run_delivery', int(job), _job_id=queue+'-replay', _queue_name=queue)
        assert message is not None
        # Use the production delivery function and worker. Keep just this
        # isolated message's result long enough to verify the actual decision.
        worker = create_worker(queue_name=queue, burst=True, poll_delay=.1, max_jobs=1,
                               functions=[func(run_delivery,keep_result=60)])
        try:
            await asyncio.wait_for(worker.async_run(),30)
            receipt = await message.result_info()
            assert receipt and receipt.success and receipt.args==(int(job),)
            assert receipt.result=='already_owned_or_terminal' and receipt.queue_name==queue
            print(json.dumps({'transport_id':message.job_id,'outcome':receipt.result,'job_id':int(job)}))
        finally:
            await worker.close()
    finally:
        await pool.aclose()
asyncio.run(main())
"""


def cancel_queued(request, prefix):
    marker='/tmp/vf-cancel-'+uuid.uuid4().hex
    body={'command':'touch '+marker,'runtime':'windows','input_paths':['Ref/input.txt']}
    job=request('POST',prefix+'/jobs',headers={'Idempotency-Key':uuid.uuid4().hex},json=body)
    jid=job['id']
    queued=request('GET',f'/jobs/{jid}')
    assert queued['status']=='queued'
    aid=str(uuid.UUID(queued['attempt']['id']))
    def delivery(operation):
        raw=docker('exec',name('api'),'python','-c',REDIS_DELIVERY,operation,aid,str(jid))
        return json.loads(raw.splitlines()[-1])
    wait(lambda:delivery('original'))
    assert request('POST',f'/jobs/{jid}:cancel')['ok']
    replay=delivery('replay')
    return {'job_id':jid,'attempt_id':aid,'marker':marker,'canonical_delivery_observed':True,'redelivery':replay}


def verify_cancelled(request, cid, evidence):
    jid=evidence['job_id']
    aid=evidence['attempt_id']
    job=request('GET',f'/jobs/{jid}')
    assert job['status']=='cancelled' and job['attempt']['id']==aid and job['attempt']['phase']=='cancelled'
    assert not job['attempt']['process_identity']
    for service in ('api','arq-worker'):
        docker('exec',name(service),'test','!','-e',evidence['marker'])
    sql=f"SELECT seq,type,coalesce(payload_json::jsonb->>'status','') FROM durable_events WHERE case_id={int(cid)} AND attempt_id='{aid}' ORDER BY seq"
    events=docker('exec',name('postgres'),'psql','-U','vibeflow','-d','vibeflow','-tAc',sql).splitlines()
    assert any(v.endswith('|attempt.state_changed|cancelled') for v in events),events
    assert not any('|attempt.launch_' in v or v.endswith(('|claimed','|running')) for v in events),events
    count=docker('exec',name('postgres'),'psql','-U','vibeflow','-d','vibeflow','-tAc',
                 f'SELECT count(*) FROM job_attempts WHERE job_id={int(jid)}')
    assert count=='1'
    return {**evidence,'events':events,'attempts':1,'terminal_status':job['status'],'process_started':False}


def overlapping(first, second):
    gate = Barrier(2)

    def send(operation):
        gate.wait(timeout=10)
        started = time.monotonic_ns()
        reply = operation()
        return {'reply': reply, 'start_ns': started, 'end_ns': time.monotonic_ns()}

    with ThreadPoolExecutor(max_workers=2) as senders:
        futures = [senders.submit(send, op) for op in (first, second)]
        replies = [future.result() for future in futures]
    assert max(r['start_ns'] for r in replies) < min(r['end_ns'] for r in replies), replies
    return replies


def simultaneous(submit):
    replies = overlapping(submit, submit)
    for row in replies:
        row['id'] = row.pop('reply')['id']
    assert replies[0]['id'] == replies[1]['id'], replies
    return replies


def race_editors(request, client, prefix, cid):
    path = prefix + '/caseDict'
    original = request('GET', path)
    before = request('GET', prefix+'/events')['total']

    def edit(label):
        content = original['content'] + '\n// editor ' + label + '\n'
        body = {'content': content, 'expected_revision': original['dict_revision'],
                'expected_sha256': original['content_sha256']}
        response = client.put('/api/v1'+path, json=body)
        return {'status': response.status_code, 'body': response.json(),
                'sha256': hashlib.sha256(content.encode()).hexdigest()}

    replies = overlapping(lambda: edit('one'), lambda: edit('two'))
    winner, loser = sorted((row['reply'] for row in replies), key=lambda r: r['status'])
    assert [winner['status'], loser['status']] == [200, 409], replies
    revision = original['dict_revision'] + 1
    conflict = loser['body']['detail']
    assert conflict['code'] == 'revision_conflict' and conflict['current_revision'] == revision, replies
    assert winner['body']['dict_revision'] == revision and winner['body']['content_sha256'] == winner['sha256']
    current = request('GET', path)
    assert current['dict_revision'] == revision and hashlib.sha256(current['content'].encode()).hexdigest() == winner['sha256']
    mirror = docker('exec', name('postgres'), 'psql', '-U', 'vibeflow', '-d', 'vibeflow', '-tAc',
                    f'SELECT dict_revision FROM cases WHERE id={int(cid)}')
    timeline = request('GET', prefix+'/events')
    assert int(mirror) == revision and timeline['total'] == before + 1
    event = timeline['events'][0]
    assert (event['type'], event['revision']) == ('caseDict.updated', revision)
    chain = request('GET', prefix+'/provenance')
    assert chain['chain_valid']
    provenance = chain['events'][0]
    assert provenance['revision'] == revision and provenance['casedict_sha256'] == winner['sha256']
    return {'replies': replies, 'revision': revision, 'pg_revision': int(mirror),
            'event_id': event['event_id'], 'provenance_sha256': provenance['casedict_sha256']}


def verify_workflow_replay(request, client, workspace):
    case = request('POST', f'/workspaces/{workspace}/cases', json={'name': 'Concurrent geometry export'})
    prefix = f"/cases/{case['id']}"
    script = b'import cadquery as cq\ndef build_geometry(ctx):\n    return {"box": cq.Workplane("XY").box(2, 3, 4)}\n'
    uploaded = request('POST', prefix+'/upload', params={'path': 'A-geo/geometry.py'},
                       files={'file': ('geometry.py', script, 'text/plain')})
    assert uploaded['content_sha256'] == hashlib.sha256(script).hexdigest()
    body = {'target': 'geometry', 'expected_revision': request('GET', prefix+'/caseDict')['dict_revision'],
            'parameters': {'geometry_mode': 'export'}, 'budget': {'timeout_seconds': 120}}
    headers = {'Idempotency-Key': uuid.uuid4().hex}

    def submit():
        return request('POST', prefix+'/workflow-runs', json=body, headers=headers)

    replies = simultaneous(submit)
    run_id = replies[0]['id']

    def awaiting_confirmation():
        run = request('GET', '/workflow-runs/'+run_id)
        if run['status'] in ('failed', 'cancelled', 'interrupted'):
            raise RuntimeError((run['status'], run['detail']))
        return run if run['status'] == 'awaiting_confirmation' else None

    run = wait(awaiting_confirmation, timeout=150)
    confirmation = next(s for s in run['steps'] if s['key'] == 'confirm_B')
    edits = race_editors(request, client, prefix, case['id'])
    def confirmation_body(step):
        return {'state': 'B', 'confirmed': True, 'expected_revision': step['confirmation_revision'],
                'workflow_step_id': step['id'], 'expected_step_version': step['version']}
    stale = client.post('/api/v1'+prefix+'/caseDict:advance', json=confirmation_body(confirmation))
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'revision_conflict'
    assert request('GET', prefix+'/caseDict')['workflow_state'] == 'A'
    assert request('GET', '/workflow-runs/'+run_id)['status'] == 'awaiting_confirmation'
    refreshed = request('POST', f"/workflow-runs/{run_id}/steps/{confirmation['id']}:refresh-confirmation",
                        json={'expected_version': confirmation['version']})
    updated = next(s for s in refreshed['steps'] if s['key'] == 'confirm_B')
    assert updated['confirmation_revision'] == edits['revision'] and updated['version'] > confirmation['version']
    request('POST', prefix+'/caseDict:advance', json=confirmation_body(updated))
    assert request('GET', prefix+'/caseDict')['workflow_state'] == 'B'
    assert submit()['id'] == run_id
    final = request('GET', '/workflow-runs/'+run_id)
    assert final['status'] == 'succeeded' and all(s['status'] == 'succeeded' for s in final['steps'])
    assert [r['id'] for r in request('GET', prefix+'/workflow-runs')] == [run_id]
    geometry = next(s for s in final['steps'] if s['key'] == 'geometry')
    attempt, = geometry['attempts']
    publication, = request('GET', prefix+'/geometry/publications')
    assert attempt['job_status'] == 'succeeded' and publication['id'] == geometry['publication']['id']
    assert publication['id'] == next(s for s in run['steps'] if s['key']=='geometry')['publication']['id']
    aid = str(uuid.UUID(attempt['id']))
    job = request('GET', f"/jobs/{attempt['job_id']}")
    assert job['attempt']['id'] == aid and job['status'] == 'succeeded'
    stop = job['attempt']['process_stop']
    assert stop['verified'] and stop['scope_empty'] and job['log_backlog'].strip()
    case_jobs = docker('exec', name('postgres'), 'psql', '-U', 'vibeflow', '-d', 'vibeflow', '-tAc',
                       f"SELECT id FROM jobs WHERE case_id={int(case['id'])} ORDER BY id")
    assert case_jobs.splitlines() == [str(job['id'])], case_jobs
    sql = ("SELECT payload_json::jsonb->>'status' FROM durable_events "
           f"WHERE attempt_id='{aid}' AND type='attempt.state_changed' ORDER BY seq")
    states = docker('exec', name('postgres'), 'psql', '-U', 'vibeflow', '-d', 'vibeflow', '-tAc', sql).splitlines()
    assert states.count('claimed') == states.count('running') == states.count('succeeded') == 1, states
    artifact = client.get('/api/v1'+prefix+'/raw', params={'path': 'A-geo/exports/box.step'})
    artifact.raise_for_status()
    assert artifact.content.startswith(b'ISO-10303-21;') and b'END-ISO-10303-21;' in artifact.content
    return {'case_id': case['id'], 'concurrent_requests': replies, 'replayed_run_id': run_id,
            'job_id': attempt['job_id'], 'attempt_id': aid, 'publication_id': publication['id'],
            'log_tail_sha256': hashlib.sha256(job['log_backlog'].encode()).hexdigest(),
            'scope_empty': stop['scope_empty'], 'case_job_ids': [job['id']],
            'editor_race': edits, 'stale_confirmation': stale.json()['detail'], 'old_revision': confirmation['confirmation_revision'],
            'refreshed_revision': updated['confirmation_revision'], 'confirmed_state': 'B',
            'states': states, 'workflow_status': final['status'], 'step_bytes': len(artifact.content),
            'step_sha256': hashlib.sha256(artifact.content).hexdigest()}


def main():
    for service in SERVICES:
        assert field(service, '{{index .Config.Labels "com.docker.compose.project"}}') == PROJECT
        assert field(service, '{{.State.Running}}') == 'false', 'Use idle stopped restore stack'
    bindings=json.loads(field('web','{{json .HostConfig.PortBindings}}'))
    assert bindings['80/tcp']==[{'HostIp':'127.0.0.1','HostPort':'57710'}]
    folder = Path(tempfile.mkdtemp(prefix='queued-restart-', dir=ROOT/'tmp'))
    print(folder, flush=True)
    result = {'passed': False, 'checks': [], 'real_model_calls': 0, 'mock_llm_calls': 0}
    def save(filename, value):
        (folder/filename).write_text(json.dumps(value, indent=2), 'utf8')
    try:
        docker('start', *(name(s) for s in SERVICES[:3]))
        for service in SERVICES[:3]:
            wait(lambda: field(service, '{{.State.Health.Status}}') == 'healthy')
        docker('start', name('api'), name('web'))
        assert docker('port', name('web'), '80/tcp') == '127.0.0.1:57710'
        with httpx.Client(base_url='http://127.0.0.1:57710', trust_env=False, timeout=40) as client:
            def healthy():
                try:
                    response = client.get('/api/health')
                    return response.status_code == 200 and response.json()['ok']
                except httpx.HTTPError:
                    return False

            def request(method, path, **kwargs):
                until = time.monotonic()+40
                while True:
                    response = client.request(method, '/api/v1'+path, **kwargs)
                    if response.status_code != 409:
                        break
                    detail = response.json().get('detail')
                    if isinstance(detail,dict) and detail.get('code')=='storage_quota_busy' and time.monotonic()<until:
                        time.sleep(.3)
                        continue
                    raise RuntimeError((method,path,detail))
                response.raise_for_status()
                return response.json()

            def kill_restart():
                old = field('api','{{.State.StartedAt}}')
                docker('kill',name('api'))
                assert field('api','{{.State.Running}}') == 'false'
                docker('start',name('api'))
                wait(healthy)
                new = field('api','{{.State.StartedAt}}')
                assert old != new
                result.setdefault('API_lifetimes',[]).append([old,new])

            wait(healthy)
            login = request('POST','/auth/register',json={'name':'Queue recovery','email':secrets.token_hex(12)+'@example.com','password':secrets.token_hex(24)})
            client.headers['Authorization']='Bearer '+login['token']
            workspace = request('GET','/workspaces')[0]['id']
            cid = request('POST',f'/workspaces/{workspace}/cases',json={'name':'Queued API restart'})['id']
            prefix = f'/cases/{cid}'
            proof = secrets.token_hex(24)
            request('PUT', prefix+'/file', json={'path':'Ref/input.txt','content':proof})
            cancelled=cancel_queued(request,prefix)
            save('cancelled-redelivery.json',cancelled)
            # Witnesses are private to this idle test worker. Exclusive creation
            # fails on a second actual start, including a start in a new runDir.
            witness='/tmp/vf-restart-'+secrets.token_hex(12)
            payload = f"""from pathlib import Path
import hashlib, time
p=Path({witness!r})
with p.open('x') as stream:
    stream.write(hashlib.sha256(Path('Ref/input.txt').read_bytes()).hexdigest())
end=time.monotonic()+100
while not p.with_suffix('.release').exists():
    if time.monotonic()>end: raise TimeoutError('release')
    time.sleep(.15)
print('RECOVERED '+p.read_text(), flush=True)
"""
            body={'command':'python -u -c '+shlex.quote(payload),'runtime':'windows','timeout_seconds':135,'input_paths':['Ref/input.txt']}
            headers={'Idempotency-Key':uuid.uuid4().hex}
            def submit():
                return request('POST',prefix+'/jobs',headers=headers,json=body)
            replies=simultaneous(submit)
            jid=replies[0]['id']
            save('concurrent-job.json',replies)
            def job():
                return request('GET',f'/jobs/{jid}')
            queued=job()
            aid=str(uuid.UUID(queued['attempt']['id']))
            assert queued['status']=='queued' and submit()['id']==jid
            result.update(case_id=cid,job_id=jid,attempt_id=aid)
            save('before-api-loss.json',queued)
            kill_restart()
            recovered=job()
            assert recovered['status']=='queued' and recovered['attempt']['id']==aid and submit()['id']==jid
            result['checks'].append('queued_job_and_idempotency_key_survive_API_process_death')
            docker('start',name('arq-worker'))
            digest=hashlib.sha256(proof.encode()).hexdigest()
            def started():
                return docker('exec',name('arq-worker'),'python','-c',
                    f"from pathlib import Path;p=Path('{witness}');print(p.read_text() if p.exists() else '')")==digest
            wait(started)
            running=job()
            assert running['status']=='running' and running['attempt']['id']==aid
            save('running.json',running)
            worker=field('arq-worker','{{.State.StartedAt}}')
            kill_restart()
            continued=job()
            assert field('arq-worker','{{.State.StartedAt}}')==worker
            assert continued['status']=='running' and continued['attempt']['id']==aid
            assert continued['attempt']['process_identity']==running['attempt']['process_identity']
            save('after-running-restart.json', continued)
            docker('exec',name('arq-worker'),'touch',witness+'.release')
            def finished():
                value=job()
                if value['status'] in ('cancelled','failed','interrupted'): raise RuntimeError(value['status'])
                return value if value['status']=='succeeded' and not value.get('completion_pending') and (value['attempt'].get('process_stop') or {}).get('verified') else None
            terminal=wait(finished)
            save('terminal.json',terminal)
            assert terminal['attempt']['id']==aid and terminal['attempt']['process_stop']['scope_empty']
            assert ('RECOVERED '+digest) in terminal['log_backlog']
            assert submit()['id']==jid
            raw=client.get('/api/v1'+prefix+'/raw',params={'path':'Ref/input.txt'})
            assert raw.status_code==200 and raw.content==proof.encode()
            save('cancelled-redelivery.json', verify_cancelled(request,cid,cancelled))
            result['checks'].append('cancelled_queued_job_rejects_real_Redis_redelivery_without_process_start')
            sql=f"SELECT seq,type,coalesce(payload_json::jsonb->>'status',''),coalesce(payload_json::jsonb->>'reason','') FROM durable_events WHERE case_id={int(cid)} AND attempt_id='{aid}' ORDER BY seq"
            events=docker('exec',name('postgres'),'psql','-U','vibeflow','-d','vibeflow','-tAc',sql).splitlines()
            save('events.json',events)
            states=[row.split('|')[2] for row in events if row.split('|')[1]=='attempt.state_changed']
            warnings=[row.split('|')[3] for row in events if row.split('|')[1]=='job.recovery_required']
            assert all(reason=='unclaimed_delivery' for reason in warnings),warnings
            assert all(row.split('|')[1]!='attempt.owner_exited' for row in events),events
            assert states.count('claimed')==states.count('running')==states.count('succeeded')==1,states
            result['checks']+=['same_live_worker_and_process_identity_survive_second_API_death','single_claim_and_execution_verified_log_input_and_empty_scope']
            save('concurrent-workflow.json', verify_workflow_replay(request, client, workspace))
            result['checks'] += ['concurrent_geometry_requests_completed_one_workflow_attempt_and_STEP_publication',
                                 'same_revision_edits_commit_one_S3_file_PG_revision_and_audit_record',
                                 'stale_confirmation_requires_explicit_refresh_before_advancing_stage']
            result['passed']=True
    except Exception as exc:
        result['failure'] = str(exc)[:1000]
        raise
    finally:
        save('result.json',result)
        docker('stop',*(name(s) for s in reversed(SERVICES)))
        print(json.dumps(result))


if __name__=='__main__':
    main()
