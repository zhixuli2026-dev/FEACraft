"""Real FS/S3 run derivation, native execution, quota, ownership and recovery."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import uuid
from urllib.parse import quote

import httpx

from testsupport import IsolatedServer, SERVER
from publication_read_capacity_checks import FIXTURE
from mixed_read_capacity_checks import postgres

OBSERVE = '''
from app import storage
from app.models import Workspace,Membership
from app.storage_models import StorageWriteReservation
from app.routers.workspaces import get_owned_case
from app.services import node_storage_budget,remote_write_budget,s3_file_transactions,s3_staging_transactions
quota_original=quota.quotas_for
async def limits(db,user):
 value=await quota_original(db,user)
 path=root/'quota-limits.json'
 configured=json.loads(path.read_text()) if path.exists() else {}
 return {**value,'storage_mb':configured[str(user.id)]} if str(user.id) in configured else value
quota.quotas_for=limits
settings=node_storage_budget.get_settings
def node_settings():
 value=settings()
 return value.model_copy(update={'node_storage_limit_mb':0}) if (root/'node-zero').exists() else value
node_storage_budget.get_settings=node_settings
original_snapshot=s3_file_transactions.snapshot
from contextvars import ContextVar
copying=ContextVar('observed_remote_source',default=False)
original_prepare=s3_file_transactions._prepare_after
def prepare(prefix,path,data,metadata):
 token=copying.set(isinstance(data,s3_file_transactions.RemoteSource))
 try:return original_prepare(prefix,path,data,metadata)
 finally:copying.reset(token)
s3_file_transactions._prepare_after=prepare
def snapshot(prefix,path,**kwargs):
 if copying.get() and path.startswith('C-run/conduction/') and kwargs.get('output') is not None:
  reserved=remote_write_budget._scope.get()
  assert reserved is not None and not reserved.closed and reserved.lease.case.s3_prefix==prefix
  claims=list((storage._fs_root()/'.node-storage-budget').glob('*.json'))
  assert claims,'Remote copy started without a node claim'
  with (root/'copies.jsonl').open('a') as out:
   out.write(json.dumps({'path':path,'limit':kwargs['limit'],'case_id':reserved.lease.case.id,'claims':len(claims)})+'\\n')
 return original_snapshot(prefix,path,**kwargs)
s3_file_transactions.snapshot=snapshot
original_apply=s3_staging_transactions.apply
async def apply(case,document,checkpoint=None):
 result=await original_apply(case,document,checkpoint)
 if (root/'derive-failure').exists() and document['plan'].get('scope','files') in {'files','files_with_modes'}:
  raise OSError('intentional failure after real derived files applied')
 return result
s3_staging_transactions.apply=apply
@app.get('/api/v1/fixture/derive-credits/{case_id}')
async def derive_credits(case_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db,write=False)
  rows=await db.scalars(select(StorageWriteReservation).where(StorageWriteReservation.case_id==case.id))
  return [{'user_id':row.user_id,'state':row.state,'remaining_bytes':row.remaining_bytes} for row in rows]
@app.post('/api/v1/fixture/share/{case_id}/{member_id}')
async def share(case_id:int,member_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db);ws=await db.get(Workspace,case.workspace_id)
  assert ws.user_id==user.id
  org=await quota.ensure_user_org(db,user)
  db.add(Membership(org_id=org.id,user_id=member_id,role='member'));await db.commit()
  return {'workspace_id':ws.id,'org_id':org.id}
'''


def verify(store=None, database_url=None, group='derive'):
    from cae_benchmarks import thermal_files
    api = IsolatedServer('run-derive-capacity', app='derive_fixture:app', database_url=database_url)
    if store:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    (api.root / 'derive_fixture.py').write_text(
        'import os\nos.environ.update(' + repr(store.settings() if store else {}) + ')\n' + FIXTURE + OBSERVE, encoding='utf8')
    result = {'passed': False, 'storage': 's3' if store else 'fs',
              'database': 'postgresql' if database_url else 'sqlite', 'llm_calls': 0, 'checks': []}
    def save():
        (api.root / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    def check(name, **fields):
        result['checks'].append({'name': name, **fields}); save()
        print('RUN_DERIVE_PASS ' + name, flush=True)
    def limits(value):
        (api.root / 'quota-limits.json').write_text(json.dumps(value))
    def credit():
        state = api.call('GET', '/fixture/credit')
        if store:
            assert state['rows'] and all(row['state'] == 'settled' and row['receipt']['closed'] for row in state['rows']), state
    def raw(path):
        return api.call('GET', api.prefix + '/raw?path=' + quote(path, safe=''), raw=True)
    def derive(name, **kwargs):
        return api.call('POST', api.prefix + '/runs:derive', {'base': 'C-run/conduction', 'name': name}, **kwargs)
    print('RUN_DERIVE_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            api.create_case('Real run derivation')
            owner = api.call('GET', '/auth/me')['id']
            inputs = thermal_files()
            for path, content in inputs.items():
                api.put(path, content)
            excluded = ['1/T', '-1/T', '1e+02/T', 'processor0/0/T', 'checkMesh.log', 'case.foam', '.vibeflow/private']
            for path in excluded:
                api.put('C-run/conduction/' + path, 'excluded source material\n')
            before = api.call('GET', '/fixture/credit?measure=true')['bytes']
            limits({owner: 0})
            derive('denied', expect=429)
            credit()
            assert api.call('GET', '/fixture/credit?measure=true')['bytes'] == before
            limits({})
            check('zero_owner_quota_no_growth')
            (api.root / 'node-zero').touch()
            try:
                refused = derive('node-denied', expect=409)
                assert refused['detail']['code'] == 'node_storage_full', refused
            finally:
                (api.root / 'node-zero').unlink()
            credit()
            api.call('GET', api.prefix + '/raw?path=C-run/node-denied/system/controlDict', expect=404)
            check('node_space_refusal_no_public_target')
            saved = derive('copied')
            assert saved['created'] == 'C-run/copied' and saved['files'] == len(inputs), saved
            for path, content in inputs.items():
                assert raw(path.replace('C-run/conduction/', 'C-run/copied/')) == content.encode()
                assert raw(path) == content.encode()
            for path in excluded:
                api.call('GET', api.prefix + '/raw?path=' + quote('C-run/copied/' + path, safe=''), expect=404)
            credit()
            check('actual_copy_exact_inputs_excludes_results_logs_processors')
            derive('copied', expect=409)
            credit()
            api.put('C-run/occupied/foreign.txt', 'must survive')
            derive('occupied', expect=409)
            assert raw('C-run/occupied/foreign.txt') == b'must survive'
            credit()
            check('existing_complete_and_partial_destinations_preserved')
            for name in ['conduction', 'CON', '../escape', 'bad.']:
                derive(name, expect=400)
                credit()
            api.call('POST', api.prefix + '/runs:derive', {'base': 'missing', 'name': 'missing-base'}, expect=400)
            credit()
            check('invalid_names_overlap_and_missing_control_rejected')
            if store:
                (api.root / 'derive-failure').touch()
                try:
                    derive('recovered', expect=500, raw=True)
                finally:
                    (api.root / 'derive-failure').unlink()
                credit()
                recovered = derive('recovered')
                assert recovered['files'] == len(inputs)
                assert raw('C-run/recovered/0/T') == inputs['C-run/conduction/0/T'].encode()
                credit()
                check('actual_uncommitted_remote_copy_recovers_then_retries')
            registered = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com',
                                  'password': 'isolated-test-123', 'name': 'Run collaborator'})
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + registered['token']},
                              trust_env=False, timeout=120) as member:
                response = member.get('/auth/me'); response.raise_for_status(); member_id = response.json()['id']
                sharing = api.call('POST', f'/fixture/share/{api.case_id}/{member_id}')
                api.call('PUT', f'/workspaces/{sharing["workspace_id"]}/sharing',
                         {'org_id': sharing['org_id'], 'role': 'member', 'enabled': True})
                limits({member_id: 0})
                response = member.post(api.prefix + '/runs:derive', json={'base': 'C-run/conduction', 'name': 'member-copy'})
                response.raise_for_status()
                assert response.json()['files'] == len(inputs)
                credit()
                limits({owner: 0})
                response = member.post(api.prefix + '/runs:derive', json={'base': 'C-run/conduction', 'name': 'member-denied'})
                assert response.status_code == 429, response.text
                credit(); limits({})
                check('shared_member_billed_to_workspace_owner', owner=owner, member=member_id)
            # Run the exact downloaded derived input, not a separately generated case.
            local = api.root / 'native-derived'
            for path in inputs:
                relative = path[len('C-run/conduction/'):]
                target = local / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw('C-run/copied/' + relative))
            linux = '/mnt/' + local.drive[0].lower() + local.as_posix()[2:]
            command = 'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd ' + shlex.quote(linux) + ' && blockMesh && checkMesh && laplacianFoam'
            executed = subprocess.run(['wsl.exe', '-d', 'vibeflow-linux', '-u', 'nobody', '--exec', '/usr/bin/bash', '-c', command],
                                      capture_output=True, timeout=90)
            (api.root / 'actual-derived-run.log').write_bytes(executed.stdout)
            (api.root / 'actual-derived-run.stderr').write_bytes(executed.stderr)
            assert executed.returncode == 0 and b'Mesh OK' in executed.stdout and (local / '1/T').exists()
            check('downloaded_derived_inputs_execute_actual_openfoam', output_sha256=hashlib.sha256((local / '1/T').read_bytes()).hexdigest())
            if store:
                copies = [json.loads(line) for line in (api.root / 'copies.jsonl').read_text().splitlines()]
                assert copies and all(row['case_id'] == api.case_id and row['claims'] > 0 for row in copies)
                tickets = api.call('GET',f'/fixture/derive-credits/{api.case_id}')
                assert tickets and all(row['user_id']==owner and row['state']=='settled' and row['remaining_bytes']==0
                                       for row in tickets),tickets
                assert not list((api.root / 'files/.node-storage-budget').glob('*.json'))
                check('streamed_remote_sources_have_owner_and_node_claims', files=len(copies))
            result['passed'] = True; save()
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error)[:2400]}; save()
        print('RUN_DERIVE_FAILED ' + json.dumps(result['failure']), flush=True)
        return 1
    finally:
        (api.root/'derive_fixture.py').unlink(missing_ok=True)
    print('RUN_DERIVE_OK ' + str(api.root / 'checks.json'), flush=True)
    return 0


if __name__ == '__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', choices=('sqlite', 'postgresql'), default='sqlite')
    parser.add_argument('--storage', choices=('fs', 's3'), default='s3')
    args = parser.parse_args()
    if args.storage == 's3':
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed',authenticated=True) as store:
            code = asyncio.run(postgres(store, 'derive', verify)) if args.database == 'postgresql' else verify(store)
    else:
        code = asyncio.run(postgres(None, 'derive', verify)) if args.database == 'postgresql' else verify()
    sys.exit(code)
