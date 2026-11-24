"""Actual submission storage scopes, native tools and dispatch; no model calls."""
import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from urllib.parse import quote

import httpx
from testsupport import IsolatedServer, SERVER
from publication_read_capacity_checks import FIXTURE
from mixed_read_capacity_checks import postgres
from professional_launch_smoke import settled, calculix_deck

OBSERVE = '''
from app import storage
from app.services import job_execution,step_handlers,job_delivery,jobrunner,document_admission,owner_storage_budget,file_transactions,synctex_queries
def record(stage,require):
 pool=owner_storage_budget.current()
 if require and storage._use_s3():assert pool is not None and not pool.closed,stage
 if not require:assert pool is None,stage
 with (root/'boundaries.jsonl').open('a') as stream:
  stream.write(json.dumps({'stage':stage,'owner':pool.ticket['user_id'] if pool else None})+'\\n')
submit=job_execution.submit
async def submitting(db,**kwargs):
 record('submit_enter',True)
 result=await submit(db,**kwargs)
 record('submit_exit',True)
 return result
job_execution.submit=submitting
finish=file_transactions.finish_committed
async def finishing(*args,**kwargs):
 record('file_cleanup_enter',True)
 result=await finish(*args,**kwargs)
 record('file_cleanup_exit',True)
 return result
file_transactions.finish_committed=finishing
def handoff(module,name):
 original=getattr(module,name)
 async def wrapped(*args,**kwargs):
  record(name,False)
  return await original(*args,**kwargs)
 setattr(module,name,wrapped)
for module,name in ((step_handlers,'launch'),(job_delivery,'deliver'),(jobrunner,'start_job'),
                    (document_admission,'complete'),(synctex_queries,'execute')):
 handoff(module,name)
from app.models import Workspace,Membership
from app.routers.workspaces import get_owned_case
quota_original=quota.quotas_for
async def configured_quota(db,user):
 value=await quota_original(db,user)
 path=root/'quota-limits.json'
 limits=json.loads(path.read_text()) if path.exists() else {}
 return {**value,'storage_mb':limits[str(user.id)]} if str(user.id) in limits else value
quota.quotas_for=configured_quota
@app.post('/api/v1/fixture/share/{case_id}/{member_id}')
async def share(case_id:int,member_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db);ws=await db.get(Workspace,case.workspace_id)
  assert ws.user_id==user.id
  org=await quota.ensure_user_org(db,user)
  db.add(Membership(org_id=org.id,user_id=member_id,role='member'));await db.commit()
  return {'workspace_id':ws.id,'org_id':org.id}
'''

ROUTES = [
 ('/geometry:preview', {}), ('/geometry:export', {}),
 ('/calculix:solve', {'input_path': 'C-run/missing.inp'}),
 ('/su2:solve', {'config_path': 'C-run/missing.cfg'}),
 ('/paraview:run', {'script_path': 'D-post/missing.py'}),
 ('/mpi:run', {'run_dir': 'C-run/missing'}),
 ('/postprocess:run', {'run_dir': 'C-run/missing'}),
 ('/report:compose', {}), ('/report:build', {}),
 ('/ppt:build', {'slides': [{'title': 'Actual submission'}]}),
 ('/pdf:number', {'input': 'E-report/missing.pdf'}),
 ('/latex:build', {'main': 'E-report/latex/missing.tex'}),
 ('/synctex:query', {'pdf': 'E-report/missing.pdf', 'page': 1, 'x': 50, 'y': 50}),
 ('/udf:build', {'name': 'missingFunction'}),
 ('/udf:integrate', {'name': 'missingFunction', 'run_dir': 'C-run/missing'}),
 ('/flotherm:convert', {'ir_path': 'Ref/missing.json'}),
 ('/flotherm:build-library', {'source_dir': 'Ref/missing'}),
 ('/jobs', {'command': 'echo REAL_SUBMISSION', 'type': 'shell'}),
 ('/jobs:batch', {'commands': ['echo REAL_BATCH'], 'type': 'shell'}),
]


def verify(store=None, database_url=None, group='admission'):
    api = IsolatedServer('submission-capacity', app='submission_fixture:app', database_url=database_url)
    if store:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    (api.root / 'submission_fixture.py').write_text('import os\nos.environ.update(' +
        repr(store.settings() if store else {}) + ')\n' + FIXTURE + OBSERVE, encoding='utf8')
    result = {'passed': False, 'storage': 's3' if store else 'fs', 'group': group,
              'database': 'postgresql' if database_url else 'sqlite', 'llm_calls': 0, 'checks': []}
    def save():
        (api.root / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    def check(name, **fields):
        result['checks'].append({'name': name, **fields}); save()
        print('SUBMISSION_PASS ' + name, flush=True)
    def credit():
        state = api.call('GET', '/fixture/credit')
        if store:
            assert state['rows'] and all(r['state'] == 'settled' and r['receipt']['closed'] for r in state['rows']), state
    def done(identifier):
        value = settled(api, identifier, timeout=240)
        (api.root / f'job-{identifier}.json').write_text(json.dumps(value, indent=2), encoding='utf8')
        assert value['status'] == 'succeeded', value
        credit()
        return value
    def raw(path):
        return api.call('GET', api.prefix + '/raw?path=' + quote(path, safe=''), raw=True)
    def launch(path, body, name, *, verify=None):
        key = uuid.uuid4().hex
        started = api.call('POST', api.prefix + path, body, extra_headers={'Idempotency-Key': key})
        job = done(started.get('job_id', started.get('id')))
        if verify:
            verify()
        replay = api.call('POST', api.prefix + path, body, extra_headers={'Idempotency-Key': key})
        assert replay.get('job_id', replay.get('id')) == job['id'], replay
        credit(); check(name, job_id=job['id'], replay=True)
        return started
    print('SUBMISSION_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            api.create_case('Submission ' + group)
            if group == 'admission':
                assert store is not None, 'Quota refusal group requires real S3'
                before = api.call('GET', '/fixture/credit?measure=true')['bytes']
                (api.root / 'quota-zero').touch()
                try:
                    for path, body in ROUTES:
                        api.call('POST', api.prefix + path, body, expect=429)
                        credit(); check('zero_quota' + path)
                    measured = api.call('GET', '/fixture/credit?measure=true')['bytes']
                    assert measured == before, {'before': before, 'after': measured}
                    assert api.call('GET', api.prefix + '/jobs') == []
                finally:
                    (api.root / 'quota-zero').unlink()
                check('all_refusals_no_physical_growth_or_jobs')
                for path, body, status in [
                    ('/calculix:solve', {'input_path': 'missing.inp'}, 404),
                    ('/su2:solve', {'config_path': 'missing.cfg'}, 404),
                    ('/paraview:run', {'script_path': 'missing.py'}, 404),
                    ('/latex:build', {'main': 'E-report/missing.tex'}, 404),
                    ('/pdf:number', {'input': 'E-report/missing.pdf'}, 404),
                    ('/udf:build', {'name': 'missingFunction'}, 404),
                    ('/flotherm:convert', {'ir_path': 'Ref/missing.json'}, 404),
                ]:
                    api.call('POST', api.prefix + path, body, expect=status)
                    credit(); check('missing_input_settles' + path)
            elif group == 'compose':
                original = '# Original report\n\nExact previous bytes.\n'
                api.put('E-report/report.md', original)
                revision = api.call('GET', api.prefix + '/caseDict')['dict_revision']
                before = api.call('GET', api.prefix + '/jobs')
                for body in ({'overwrite': False}, {'overwrite': True, 'expected_revision': revision + 1},
                             {'overwrite': True, 'expected_sha256': '0' * 64}):
                    api.call('POST', api.prefix + '/report:compose', body, expect=409)
                    credit()
                assert api.call('GET', api.prefix + '/jobs') == before
                check('compose_conflicts_no_submission_and_settled_capacity')
                body = {'overwrite': True, 'expected_revision': revision,
                        'expected_sha256': hashlib.sha256(original.encode()).hexdigest()}
                headers = {'Idempotency-Key': 'actual-compose'}
                draft = api.call('POST', api.prefix + '/report:compose', body, extra_headers=headers)
                done(draft['job_id'])
                assert draft['publication']['applied'] and len(draft['chapters']) == 9
                assert raw(draft['backup']) == original.encode()
                assert raw('E-report/report.md') == draft['content'].encode()
                check('actual_composition_wait_result_and_exact_backup', job_id=draft['job_id'])
                replay = api.call('POST', api.prefix + '/report:compose', body, extra_headers=headers)
                assert replay['job_id'] == draft['job_id'] and replay['publication']['applied']
                credit(); check('compose_replay_after_original_hash_changed')
                second = launch('/report:compose?wait=false', {'output': 'E-report/report.generated.md'},
                                'actual_independent_composition_channel')
                document = api.call('GET', api.prefix + '/report/compose-result?publication_id=' + second['publication_id'])
                assert document['publication']['applied']
                assert raw('E-report/report.generated.md') == document['content'].encode()
                assert raw('E-report/report.md') == draft['content'].encode()
            elif group == 'sharing':
                owner = api.call('GET', '/auth/me')['id']
                api.put('Ref/input.txt', 'SHARED_SEAL_PROOF\n')
                registration = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com',
                    'name': 'Submit collaborator', 'password': 'isolated-test-123'})
                def limits(value):
                    (api.root / 'quota-limits.json').write_text(json.dumps(value))
                with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + registration['token']},
                                  trust_env=False, timeout=120) as member:
                    response = member.get('/auth/me'); response.raise_for_status(); member_id = response.json()['id']
                    sharing = api.call('POST', f'/fixture/share/{api.case_id}/{member_id}')
                    api.call('PUT', f'/workspaces/{sharing["workspace_id"]}/sharing',
                             {'org_id': sharing['org_id'], 'role': 'member', 'enabled': True})
                    limits({member_id: 0})
                    body = {'command': 'type Ref\\input.txt', 'type': 'shell', 'input_paths': ['Ref/input.txt']}
                    response = member.post(api.prefix + '/jobs', json=body, headers={'Idempotency-Key': 'shared-seal'})
                    response.raise_for_status(); identifier = response.json()['id']
                    job = done(identifier)
                    assert b'SHARED_SEAL_PROOF' in raw(job['log_path'])
                    check('member_zero_quota_actual_seal_and_read_owned_input', job_id=identifier, owner=owner, member=member_id)
                    response = member.post(api.prefix + '/jobs', json=body, headers={'Idempotency-Key': 'shared-seal'})
                    response.raise_for_status(); assert response.json()['id'] == identifier
                    credit(); check('shared_submission_replay')
                    before = api.call('GET', '/fixture/credit?measure=true')['bytes']
                    limits({owner: 0})
                    response = member.post(api.prefix + '/jobs', json=body)
                    assert response.status_code == 429, response.text
                    credit()
                    assert api.call('GET', '/fixture/credit?measure=true')['bytes'] == before
                    limits({}); check('owner_zero_quota_refuses_member_before_seal_growth')
                events = [json.loads(line) for line in (api.root / 'boundaries.jsonl').read_text().splitlines()]
                assert all(row['owner'] == (owner if store else None) for row in events if row['stage'].startswith('submit'))
                check('observed_shared_owner_submission_scope')
            else:
                launch('/jobs', {'command': 'echo REAL_SUBMISSION', 'type': 'shell'}, 'actual_shell_submission')
                batch = api.call('POST', api.prefix + '/jobs:batch', {
                    'commands': ['echo REAL_BATCH_ONE', 'echo REAL_BATCH_TWO'], 'max_concurrent': 1,
                    'type': 'shell'}, expect=202, extra_headers={'Idempotency-Key': 'actual-batch'})
                for job in batch['jobs']:
                    done(job['id'])
                replay = api.call('POST', api.prefix + '/jobs:batch', {
                    'commands': ['echo REAL_BATCH_ONE', 'echo REAL_BATCH_TWO'], 'max_concurrent': 1,
                    'type': 'shell'}, expect=202, extra_headers={'Idempotency-Key': 'actual-batch'})
                assert replay['id'] == batch['id'] and replay['counts']['succeeded'] == 2, replay
                credit(); check('actual_batch_commit_cleanup_dispatch_and_replay')
                api.create_case('Native geometry')
                prepared = api.put('A-geo/geometry.py', "import cadquery as cq\ndef build_geometry(ctx):\n    return cq.Workplane('XY').box(1, 2, 3)\n")
                done(prepared['auto_preview_job'])
                launch('/geometry:preview', {}, 'actual_cadquery_preview')
                launch('/geometry:export', {}, 'actual_cadquery_export')
                scene = api.call('GET', api.prefix + '/geometry/scene')
                assert scene['objects']
                api.create_case('Native report')
                api.put('E-report/report.md', '# Actual admission\n\nNative Pandoc output.\n')
                launch('/report:build', {}, 'actual_pandoc_preparation_and_publication',
                       verify=lambda: assert_html(raw('E-report/build/report.html')))
                launch('/ppt:build', {'title': 'Actual presentation', 'slides': [{'title': 'Measured', 'text': 'Real file'}]},
                       'actual_ppt_publication', verify=lambda: assert_zip(raw('E-report/build/vibeflow.pptx')))
                api.create_case('Native latex')
                api.put('E-report/latex/main.tex', '\\documentclass{article}\n\\begin{document}\nActual capacity handoff.\n\\end{document}\n')
                latex = launch('/latex:build', {'recipe': 'pdflatex'}, 'actual_latex_native_preparation_and_publication',
                               verify=lambda: assert_pdf(raw('E-report/latex/build/main.pdf')))
                launch('/pdf:number', {'input': 'E-report/latex/build/main.pdf', 'output': 'E-report/numbered.pdf'},
                       'actual_pdf_number_publication', verify=lambda: assert_pdf(raw('E-report/numbered.pdf')))
                query = api.call('POST', api.prefix + '/synctex:query', {
                    'pdf': 'E-report/latex/build/main.pdf', 'publication_id': latex['publication_id'],
                    'page': 1, 'x': 150, 'y': 100})
                assert query['page'] == 1
                credit(); check('actual_synctex_native_handoff')
                api.create_case('Native CalculiX')
                api.put('C-run/model.inp', calculix_deck())
                launch('/calculix:solve', {'input_path': 'C-run/model.inp'}, 'actual_calculix_owned_solve')
                assert b'DISP' in raw('C-run/model.frd')
                events = [json.loads(line) for line in (api.root / 'boundaries.jsonl').read_text().splitlines()]
                assert {'submit_enter', 'submit_exit', 'file_cleanup_enter', 'file_cleanup_exit',
                        'launch', 'deliver', 'start_job', 'complete', 'execute'} <= {row['stage'] for row in events}
                check('actual_scope_observations_submission_cleanup_native_dispatch', events=len(events))
            result['passed'] = True; save()
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error)[:3000]}; save()
        print('SUBMISSION_FAILED ' + json.dumps(result['failure']), flush=True)
        return 1
    print('SUBMISSION_OK ' + str(api.root / 'checks.json'), flush=True)
    return 0


def assert_html(data):
    assert b'Actual admission' in data


def assert_zip(data):
    assert data.startswith(b'PK')


def assert_pdf(data):
    assert data.startswith(b'%PDF')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', choices=('sqlite', 'postgresql'), default='sqlite')
    parser.add_argument('--storage', choices=('fs', 's3'), default='s3')
    parser.add_argument('--group', choices=('admission', 'native', 'sharing', 'compose'), default='admission')
    args = parser.parse_args()
    if args.storage == 's3':
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed') as store:
            code = asyncio.run(postgres(store, args.group, verify)) if args.database == 'postgresql' else verify(store, group=args.group)
    else:
        code = asyncio.run(postgres(None, args.group, verify)) if args.database == 'postgresql' else verify(group=args.group)
    sys.exit(code)
