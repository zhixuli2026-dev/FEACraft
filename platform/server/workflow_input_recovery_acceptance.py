"""Actual API/CadQuery recovery; explicit historical metadata faults, no inference.

Requires a reviewed source manifest. Optional browser work uses the same actual
API and real built UI; the harness never supplies an LLM or CLI reply.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid

import httpx
from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]
APP = '''import json,os,uuid
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Case,Job,JobAttempt,JobExecutionHead,WorkflowRun,WorkflowStep
from app.routers.workspaces import get_owned_case
from app.services import workflow_scheduler as scheduler,process_identity_native
from app.services.durable_events import json_text,now

@app.get('/api/v1/test/workflow-birth')
async def birth():return process_identity_native.observe(os.getpid())

@app.get('/api/v1/test/workflow-terminals')
async def terminals(user=Depends(get_current_user)):
 from app.models import NativeSession
 from app.routers.terminal import _sessions
 from app.services.terminal_records import public
 current=process_identity_native.observe(os.getpid())
 async with SessionLocal() as db:
  return [dict(public(row),owner_available=row.id in _sessions) for row in await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id))
   if json.loads(row.owner_identity_json)==current]

@app.post('/api/v1/test/workflow-source/{case_id}')
async def source(case_id:int,body:dict,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  legacy=body.get('legacy',False)
  from app.services.quota import ensure_user_org
  org=await ensure_user_org(db,user)
  plan=scheduler.make_plan('geometry','A',None if legacy else {'geometry_mode':'export'})
  run=WorkflowRun(id=str(uuid.uuid4()),case_id=case.id,org_id=org.id,created_by=user.id,
   target='geometry',status='interrupted',request_hash='0'*64,expected_revision=case.dict_revision,
   run_dir='C-run/case-1',plan_json=json_text(plan),input_manifest_json=json_text(scheduler.case_snapshot(case)),
   budget_json=json_text({'timeout_seconds':180,'max_log_bytes':1048576}),ended_at=now())
  db.add(run);await db.flush();steps={}
  for item in plan['steps']:
   step=WorkflowStep(run_id=run.id,step_key=item['key'],handler=item['handler'],handler_version=item['version'],
    dependencies_json=json_text(item['dependencies']),confirmation_state=item['confirmation_state'],status='cancelled')
   db.add(step);await db.flush();steps[item['key']]=step
  job=Job(case_id=case.id,type='geometry',command='HISTORICAL_UNSEALED_REQUEST_MUST_NEVER_EXECUTE',status='cancelled')
  db.add(job);await db.flush()
  attempt=JobAttempt(job_id=job.id,root_job_id=job.id,number=1,step_id=steps['geometry'].id,phase='cancelled',
   input_manifest_json='{}',ended_at=now())
  if body.get('unknown'):attempt.worker_identity_json=json_text({'unknown_historical_worker':True})
  db.add(attempt);await db.flush();steps['geometry'].active_attempt_id=attempt.id
  db.add(JobExecutionHead(job_id=job.id,next_number=2,active_attempt_id=attempt.id))
  await db.commit()
  return {'run_id':run.id,'job_id':job.id,'attempt_id':attempt.id,'case_id':case.id}

@app.post('/api/v1/test/workflow-cross-binding/{job_id}/{step_id}')
async def cross_binding(job_id:int,step_id:str,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id));attempt.step_id=step_id
  await db.commit();return {'fault_injected':True}
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def history(path, source):
    with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return {table: [dict(row) for row in db.execute(query, (identifier,))] for table, query, identifier in [
            ('run', 'SELECT * FROM workflow_runs WHERE id=?', source['run_id']),
            ('steps', 'SELECT * FROM workflow_steps WHERE run_id=? ORDER BY id', source['run_id']),
            ('job', 'SELECT * FROM jobs WHERE id=?', source['job_id']),
            ('attempt', 'SELECT * FROM job_attempts WHERE id=?', source['attempt_id'])]}


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256'] == hashlib.sha256(APP.encode()).hexdigest()
    for name, digest in review['source_hashes'].items():
        assert sha(ROOT / name) == digest, name
    api = IsolatedServer('workflow-input-recovery', app='workflow_input_fixture:app')
    (api.root / 'workflow_input_fixture.py').write_text(APP, encoding='utf8')
    report = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0,
        'developer_database_modified': False, 'review_sha256': sha(args.review), 'checks': []}
    entered = False
    handle = preview = preview_handle = preview_log = None
    current_jobs = []
    def save():
        (api.root / 'workflow-result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    def setup(name, **fault):
        workspace = api.call('GET', '/workspaces')[0]
        case = api.call('POST', '/workspaces/' + str(workspace['id']) + '/cases', {'name': name})
        api.case_id = case['id']; api.prefix = '/cases/' + str(case['id'])
        written = api.put('A-geo/geometry.py', 'import cadquery as cq\ndef build_geometry(ctx):\n    return {"actual_current_box": cq.Workplane("XY").box(1,1,1)}\n')
        if written.get('auto_preview_job'):
            identifier = written['auto_preview_job']; current_jobs.append(identifier)
            assert api.wait_job(identifier,timeout=210)['attempt']['process_stop']['verified'] is True
        source = api.call('POST', '/test/workflow-source/' + str(case['id']), fault)
        return dict(source, case_name=name)
    def body_for(context):
        body = {'expected_version': context['run']['version'], 'expected_revision': context['current_revision'],
            'confirmed_reset': True, 'target': 'geometry', 'run_dir': 'C-run/case-1',
            'parameters': {'geometry_mode': 'export', 'report_mode': 'compose', 'solve_mode': 'serial'},
            'budget': {'timeout_seconds': 180, 'max_log_bytes': 1048576}}
        if context['action'] == 'migrate':
            body.update(expected_event_sequence=context['migration']['current_event_sequence'],
                expected_source_plan_sha256=context['migration']['source_plan_sha256'])
        return body
    try:
        api.__enter__(); entered = True
        birth = api.call('GET', '/test/workflow-birth'); handle = ProcessHandle.query(birth); report['API_birth'] = birth
        api.create_case('Workflow recovery seed')
        user = api.call('GET', '/auth/me')
        if not args.browser:
            for legacy in (False, True):
                source = setup('Actual legacy migration' if legacy else 'Actual registered recomputation', legacy=legacy)
                prefix = '/workflow-runs/' + source['run_id']
                original = history(api.root / 'test.sqlite', source)
                plan = api.call('GET', '/jobs/' + str(source['job_id']) + '/input-recovery')
                assert plan['available'] is False and plan['reason']['code'] == 'workflow_recovery_required'
                assert plan['workflow']['run_id'] == source['run_id']
                context = api.call('GET', prefix + '/recovery'); action = 'migrate' if legacy else 'recompute'
                assert context['action'] == action, context
                body = body_for(context); key = str(uuid.uuid4()); headers = {'Idempotency-Key': key}
                assert api.call('POST', prefix + '/recovery-request', {'action': action, 'key': key, 'payload': body}) == {'found': False, 'run': None}
                stale = dict(body, expected_revision=body['expected_revision'] + 1)
                assert api.call('POST', prefix + ':' + action, stale, extra_headers=headers, expect=409)['detail']['code'] in {'revision_conflict','input_stale','state_conflict'}
                assert api.call('POST', prefix + '/recovery-request', {'action': action, 'key': key, 'payload': stale})['found'] is False
                result = api.call('POST', prefix + ':' + action, body, extra_headers=headers)
                actual = next(item for item in result['steps'] if item['key'] == 'geometry')['attempts'][0]['job_id']; current_jobs.append(actual)
                job = api.wait_job(actual, timeout=210)
                assert job['attempt']['process_stop']['verified'] is True
                assert job['attempt']['handler']['key'] == 'geometry.export'
                run_path = job['attempt']['workspace']['run_path']
                manifest = json.loads(api.call('GET', api.prefix + '/raw?path=' + run_path + '/A-geo/geometry_manifest.json', raw=True))
                assert manifest['artifacts']
                replay = api.call('POST', prefix + ':' + action, body, extra_headers=headers); assert replay['id'] == result['id']
                lookup = api.call('POST', prefix + '/recovery-request', {'action': action, 'key': key, 'payload': body}); assert lookup['found'] and lookup['run']['id'] == result['id']
                assert api.call('POST', prefix + '/recovery-request', {'action': action, 'key': key, 'payload': dict(body, target='mesh')}, expect=409)['detail']['code'] == 'idempotency_conflict'
                after = api.call('GET', prefix + '/recovery?limit=1'); assert after['successors']['items'][0]['id'] == result['id']
                assert api.call('GET', '/workflow-runs/' + result['id'] + '/recovery')['source']['id'] == source['run_id']
                assert history(api.root / 'test.sqlite', source) == original
                cancel = api.call('GET', '/workflow-runs/' + result['id'])
                api.call('POST', '/workflow-runs/' + result['id'] + ':cancel', {'expected_version': cancel['version']})
                report['checks'].append({'kind': action, 'source': source, 'target_run': result['id'], 'actual_job': actual,
                    'actual_export': manifest, 'original_history_unchanged': True, 'same_key_same_run': True, 'actual_stop_verified': True})
                save()
            source = setup('Unknown writer must stay blocked', unknown=True)
            original = history(api.root / 'test.sqlite', source); prefix = '/workflow-runs/' + source['run_id']
            context = api.call('GET', prefix + '/recovery'); assert context['action'] is None and context['reason']['code'] == 'recovery_required'
            assert api.call('POST', prefix + ':recompute', body_for(context), extra_headers={'Idempotency-Key': str(uuid.uuid4())}, expect=409)['detail']['code'] == 'recovery_required'
            assert history(api.root / 'test.sqlite', source) == original
            report['checks'].append({'kind': 'unclaimed_unknown_writer_refused', 'source': source})
            foreign = setup('Workflow binding scope target')
            other = api.call('GET', '/workflow-runs/' + foreign['run_id'])
            step = next(row for row in other['steps'] if row['key'] == 'geometry')
            api.call('POST', '/test/workflow-cross-binding/' + str(source['job_id']) + '/' + step['id'], {})
            assert api.call('GET', '/jobs/' + str(source['job_id']) + '/input-recovery')['workflow'] is None
            blocked = api.call('GET', '/workflow-runs/' + foreign['run_id'] + '/recovery')
            assert blocked['action'] is None and blocked['reason']['code'] == 'evidence_invalid' and blocked['run']['steps'] == []
            report['checks'].append({'kind': 'cross_case_attempt_binding_refused'})
            owner = api.token; api.create_case('Independent account for access checks')
            assert api.call('GET', prefix + '/recovery', expect=404)
            assert api.call('POST', prefix + '/recovery-request', {'action': 'recompute', 'key': str(uuid.uuid4()), 'payload': body_for(context)}, expect=404)
            api.token = owner
            report['checks'].append({'kind': 'foreign_account_context_and_lookup_refused'})
        if args.browser:
            source = setup('Browser workflow input recovery')
            original = history(api.root / 'test.sqlite', source)
            with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            web = ROOT / 'platform/web'; preview_log = (api.root / 'web-preview.log').open('w', encoding='utf8')
            preview = subprocess.Popen([shutil.which('node.exe') or 'node', str(web / 'node_modules/vite/bin/vite.js'), 'preview',
                '--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),
                stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            preview_birth = identity.observe(preview.pid); preview_handle = ProcessHandle.query(preview_birth); report['preview_birth'] = preview_birth
            web_url = 'http://127.0.0.1:' + str(port); until = time.monotonic() + 30
            with httpx.Client(timeout=1, trust_env=False) as client:
                while True:
                    assert preview.poll() is None
                    try:
                        if client.get(web_url).status_code == 200: break
                    except httpx.HTTPError: pass
                    assert time.monotonic() < until; time.sleep(.1)
            setup_data = {'root': str(api.root), 'web': web_url, 'email': user['email'], 'source': source}
            (api.root / 'browser-setup.json').write_text(json.dumps(setup_data, indent=2) + '\n', encoding='utf8')
            print('WORKFLOW_RECOVERY_BROWSER_READY ' + str(api.root), flush=True); save()
            done = api.root / 'browser-finished.json'; until = time.monotonic() + 1200
            while not done.exists():
                assert preview.poll() is None and api.proc.poll() is None
                if time.monotonic() >= until: raise TimeoutError('Actual browser acceptance not finished')
                time.sleep(.2)
            browser = json.loads(done.read_text(encoding='utf8')); assert browser['passed']; report['browser'] = browser
            assert history(api.root / 'test.sqlite', source) == original
            context = api.call('GET', '/workflow-runs/' + source['run_id'] + '/recovery')
            assert len(context['successors']['items']) == 1
            target = context['successors']['items'][0]['id']
            actual = api.call('GET', '/workflow-runs/' + target)
            identifiers = [item['job_id'] for step in actual['steps'] for item in step['attempts']]
            assert len(identifiers) == 1
            for identifier in identifiers:
                current_jobs.append(identifier)
                job = api.wait_job(identifier, timeout=210)
                assert job['attempt']['process_stop']['verified'] is True
                assert job['attempt']['handler']['key'] == 'geometry.export'
                run_path = job['attempt']['workspace']['run_path']
                raw = api.call('GET', api.prefix + '/raw?path=' + run_path + '/A-geo/geometry_manifest.json', raw=True)
                assert json.loads(raw)['artifacts']
            report['checks'].append({'kind':'actual_browser_workflow_handoff','target_run':target,
                'actual_jobs':identifiers,'original_history_unchanged':True,'one_successor_only':True})
        report['functional_passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:2200], 'frames': [
            {'file': Path(row.filename).name, 'line': row.lineno, 'function': row.name} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                # Browser-created PTYs use the real owned close path.
                if args.browser:
                    for row in api.call('GET', '/test/workflow-terminals'):
                        if row['cleanup_verified'] is not True and row['owner_available']:
                            api.call('DELETE', '/terminal/sessions/' + row['id'])
                    sessions = api.call('GET', '/test/workflow-terminals')
                    assert all(row['cleanup_verified'] is True for row in sessions)
                    report['actual_owned_terminal_stops'] = sessions
                for identifier in current_jobs:
                    assert api.call('GET', '/jobs/' + str(identifier))['attempt']['process_stop']['verified'] is True
            except Exception as error: report['cleanup_observation_error'] = type(error).__name__
        if preview:
            preview.terminate(); preview.wait(timeout=15)
        if preview_handle:
            preview_handle.wait_exit(10); report['preview_exit_verified'] = True; preview_handle.close()
        if preview_log: preview_log.close()
        if entered: api.__exit__(None if report.get('functional_passed') else RuntimeError, None, None)
        if handle: handle.wait_exit(15); report['API_exit_verified'] = True; handle.close()
        report['passed'] = bool(report.get('functional_passed') and report.get('API_exit_verified')
            and (not args.browser or report.get('preview_exit_verified')) and not report.get('cleanup_observation_error'))
        save()
    print(json.dumps({'root': str(api.root), 'passed': report['passed'], 'failure': report.get('failure'), 'checks': len(report['checks'])}), flush=True)
    if not report['passed']: raise SystemExit(1)


if __name__ == '__main__':
    run()
