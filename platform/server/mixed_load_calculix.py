"""Numerical jobs in the disposable FF-J21 mixed-load deployment."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time
from uuid import uuid4

import numpy as np

from linux_calculix_acceptance import DECK, MATERIAL, displacements, reference
from mixed_load_test_support import docker


INPUT_PATH = 'C-run/mixed-cube.inp'
RESULT_PATH = 'C-run/mixed-cube.frd'
SUBMIT_CONCURRENCY = 4
TEMPORARY_ADMISSION_CODES = {'calculix_seal_busy', 'storage_quota_busy', 'file_sync_busy', 'case_busy'}


def epoch():
    return time.time_ns() // 1_000_000


def timestamp(value):
    observed = datetime.fromisoformat(value)
    return round((observed if observed.tzinfo else observed.replace(tzinfo=timezone.utc)).timestamp() * 1000)


def inputs(load):
    return {INPUT_PATH: DECK.replace(',-1000', f',-{load}'), 'C-run/material.inp': MATERIAL}


async def prepare(client, request, api, account, load):
    route = f"{api}/cases/{account['job_case']}/file"
    for path, content in inputs(load).items():
        await request(client, 'PUT', route, account['token'], json={'path': path, 'content': content})
    account['solver_load'] = load


def check_displacements(data, load):
    measured = displacements(data)
    expected = reference(load)
    if not np.allclose(measured, expected, rtol=5e-5, atol=1e-8):
        raise ValueError('CalculiX displacement differs from the independent stiffness calculation')
    return float(measured[4, 2]), float(np.max(np.abs(measured - expected)))


async def raw(client, address, token, path, reads, responses):
    for retry in range(5):
        async with reads:
            async with client.get(address, headers={'Authorization': 'Bearer ' + token}, params={'path': path}) as reply:
                responses.append(reply.status)
                if reply.status == 200:
                    return await reply.read()
                if reply.status == 429 and retry < 4:
                    detail = (await reply.json()).get('detail', {})
                    if isinstance(detail, dict) and detail.get('code') == 'raw_download_busy':
                        wait = int(reply.headers.get('Retry-After', '1'))
                    else:
                        raise RuntimeError('Unrecognized artifact throttling')
                else:
                    raise RuntimeError(f'CalculiX artifact read returned HTTP {reply.status}')
        await asyncio.sleep(wait)


async def submit_job(client, api, case, token, admissions, row):
    headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': uuid4().hex}
    responses = row['submission_responses'] = []
    retry_until = time.monotonic() + 95
    delay = .2
    while True:
        async with admissions:
            row.setdefault('submitted_ms', epoch())
            async with client.post(f'{api}/cases/{case}/calculix:solve', headers=headers,
                                   json={'input_path': INPUT_PATH}) as reply:
                result = await reply.json()
                if reply.status == 200:
                    responses.append({'http': 200})
                    return result
                detail = result.get('detail') if isinstance(result, dict) else None
                code = detail.get('code') if isinstance(detail, dict) else None
                responses.append({'http': reply.status, 'code': code})
                if reply.status not in {409, 503} or code not in TEMPORARY_ADMISSION_CODES or time.monotonic() >= retry_until:
                    raise RuntimeError(f'CalculiX job submission returned HTTP {reply.status} ({code})')
        await asyncio.sleep(delay)
        delay = min(1.5, delay * 2)


async def wait_job(client, request, api, case, job_id, token, reads):
    while True:
        await asyncio.sleep(1)
        async with reads:
            jobs = await request(client, 'GET', f'{api}/cases/{case}/jobs', token)
        selected = next((job for job in jobs if job['id'] == job_id), None)
        if selected is None:
            raise ValueError('The admitted CalculiX job was not visible to its owner')
        if selected['status'] not in {'queued', 'running'}:
            async with reads:
                return await request(client, 'GET', f'{api}/jobs/{job_id}', token)


async def load(client, request, public, accounts, starts):
    api = public + '/api/v1'
    submissions = asyncio.Semaphore(SUBMIT_CONCURRENCY)
    reads = asyncio.Semaphore(6)

    async def one(account):
        await asyncio.sleep(max(0, (starts - epoch()) / 1000))
        case, token, force = (account[key] for key in ('job_case', 'token', 'solver_load'))
        row = {'case': case, 'load_per_top_node': force, 'scheduled_ms': starts}
        try:
            async with asyncio.timeout(210):
                accepted = await submit_job(client, api, case, token, submissions, row)
                row.update(job=accepted['job_id'], accepted_ms=epoch())
                job = await wait_job(client, request, api, case, row['job'], token, reads)
                attempt = job['attempt']
                row.update(status=job['status'], reason=attempt.get('reason_code'), attempt=attempt['id'])
                if job['status'] != 'succeeded' or job['exit_code'] != 0 or job['completion_pending']:
                    return row
                parent = attempt['process_stop']
                child, = attempt['child_processes']['children'].values()
                if (not parent or not parent['verified'] or not parent['scope_empty']
                        or child['purpose'] != 'calculix_solve' or child['phase'] != 'stopped'
                        or not child['stop']['verified'] or not child['stop']['scope_empty']):
                    raise ValueError('Missing verified CalculiX scope stop')
                row.update(native_reserved_ms=timestamp(child['created_at']), native_stopped_ms=timestamp(child['ended_at']),
                           native_child_id=child['id'],
                           parent_stopped=True, child_stopped=True)
                publication = attempt['outputs']['publication']
                if publication['status'] != 'committed':
                    raise ValueError('CalculiX publication was incomplete')
                row['publication'] = publication['id']
                raw_url = f'{api}/cases/{case}/raw'
                base = '.vibeflow/publications/' + publication['id']
                transfers = row['artifact_read_http'] = []
                manifest = json.loads(await raw(client, raw_url, token, base + '/manifest.json', reads, transfers))
                result = await raw(client, raw_url, token, RESULT_PATH, reads, transfers)
                archived = await raw(client, raw_url, token, base + '/files/' + RESULT_PATH, reads, transfers)
                digest = hashlib.sha256(result).hexdigest()
                if archived != result or digest != manifest['files'][RESULT_PATH]['sha256']:
                    raise ValueError('CalculiX output and immutable publication differ')
                displacement, error = await asyncio.to_thread(check_displacements, result, force)
                row.update(frd_sha256=digest, max_absolute_error=error, top_displacement_z=displacement)
        except Exception as error:
            row['error'] = type(error).__name__
            row['detail'] = str(error)[:240]
        finally:
            row['finished_ms'] = epoch()
        return row

    results = await asyncio.gather(*(one(account) for account in accounts))

    async def verify_neighbor(index):
        peer = results[(index + 1) % len(results)]
        if 'job' not in peer:
            return
        headers = {'Authorization': 'Bearer ' + accounts[index]['token']}
        try:
            async with reads:
                async with client.get(f"{api}/jobs/{peer['job']}", headers=headers) as reply:
                    results[index]['foreign_job_http'] = reply.status
                async with client.get(f"{api}/cases/{peer['case']}/raw", headers=headers,
                                      params={'path': RESULT_PATH}) as reply:
                    results[index]['foreign_result_http'] = reply.status
        except Exception as error:
            results[index]['isolation_error'] = type(error).__name__

    await asyncio.gather(*(verify_neighbor(index) for index in range(len(accounts))))
    return results


def worker_records(deployment):
    code = """
import asyncio,json
from sqlalchemy import select
from app.db import SessionLocal
from app.models import DurableEvent, JobAttempt
async def main():
    async with SessionLocal() as db:
        rows = (await db.execute(select(JobAttempt.id,JobAttempt.job_id,JobAttempt.worker_id,
            JobAttempt.started_at,JobAttempt.ended_at).order_by(JobAttempt.job_id))).all()
        births = (await db.execute(select(DurableEvent.attempt_id,DurableEvent.payload_json,DurableEvent.created_at)
                  .where(DurableEvent.type == 'attempt.child_running'))).all()
    children = {}
    for attempt,payload,start in births:
        children.setdefault(attempt,{})[json.loads(payload)['child_id']] = start.isoformat()
    print(json.dumps([{'job':j,'attempt':attempt,'runner':worker.split(':')[0] if worker else None,
                      'started_at':start.isoformat() if start else None,'ended_at':end.isoformat() if end else None,
                      'child_running_at':children.get(attempt,{})} for attempt,j,worker,start,end in rows]))
asyncio.run(main())
"""
    return json.loads(docker('exec', deployment.worker, '/usr/local/bin/python', '-B', '-c', code, timeout=35))


def first_worker_start(deployment):
    code = """
import asyncio,json,socket,time
from sqlalchemy import select
from app.db import SessionLocal
from app.models import JobAttempt
async def main():
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        async with SessionLocal() as db:
            row = (await db.execute(select(JobAttempt.job_id,JobAttempt.started_at)
                .where(JobAttempt.worker_id.startswith(socket.gethostname() + ':'),JobAttempt.started_at.is_not(None))
                .order_by(JobAttempt.started_at).limit(1))).first()
        if row is not None:
            print(json.dumps({'job':row[0],'started_at':row[1].isoformat()}))
            return
        await asyncio.sleep(.2)
    raise TimeoutError('CalculiX worker did not start an accepted job')
asyncio.run(main())
"""
    return json.loads(docker('exec', deployment.worker, '/usr/local/bin/python', '-B', '-c', code, timeout=65))
