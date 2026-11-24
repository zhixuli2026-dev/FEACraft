"""Verify real recomputation and legacy migration during a large caseDict read."""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from casedict_fallback_capacity_acceptance import read_settled
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


SIZE = 40 * 1024 * 1024
PATH = '0-caseDict/caseDict'
TIMELINE = '0-caseDict/case-events.jsonl'
PROVENANCE = '0-caseDict/provenance.jsonl'
OBSERVER = '''
from contextlib import closing
import json
from pathlib import Path
import time

from app import storage
from app.services.case_events import CASE_EVENTS_PATH
from app.services.file_read import FileRead

root = Path(__file__).parent
original_chunks = FileRead.chunks
armed = {}

def chunks(reader,*args,**kwargs):
    prefix = armed.get('prefix')
    selected = prefix and (reader.location['Key']==storage._s3_key(prefix,CASE_EVENTS_PATH) if reader.remote
                           else reader.path==storage._fs_path(prefix,CASE_EVENTS_PATH))
    with closing(original_chunks(reader,*args,**kwargs)) as source:
        for block in source:
            if selected and reader.size==40*1024*1024:
                witness = root/(armed['action']+'-reading.json')
                if not witness.is_file():
                    temporary = witness.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'size':reader.size,'received':len(block)}),encoding='utf8')
                    temporary.replace(witness)
                    deadline = time.monotonic()+20
                    while not (root/(armed['action']+'-release')).exists():
                        if time.monotonic()>=deadline:
                            raise TimeoutError('Independent writer did not complete during real history read')
                        time.sleep(.02)
            yield block
FileRead.chunks = chunks

@app.middleware('http')
async def see_reset(request,call_next):
    action = armed.get('action')
    if action and request.method=='POST' and request.url.path.endswith(':'+action):
        armed['requests'] += 1
        if armed['requests']==2:
            (root/(action+'-parallel-request')).touch()
    return await call_next(request)

@app.post('/api/v1/fixture/reset-history/{case_id}')
async def history(case_id:int,body:dict,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    assert body['action'] in {'recompute','migrate'}
    case = await get_owned_case(case_id,user,db,write=True)
    size = 40*1024*1024
    head, tail = b'{"data":"', b'"}'+bytes((10,))
    data = head+b'w'*(size-len(head)-len(tail))+tail
    storage.put_bytes(case.s3_prefix,CASE_EVENTS_PATH,data)
    armed.update(prefix=case.s3_prefix,action=body['action'],requests=0)
    return {'sha256':hashlib.sha256(data).hexdigest(),'size':size}
'''

EXIT_OBSERVER = '''
import os
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import SessionLocal
from app.models import Case
from app.services import workflow_reset

original_creation = workflow_reset.create_run
original_commit = AsyncSession.commit

async def exit_at_commit(case_id, run_id, cut):
    async with SessionLocal() as db:
        case = await db.get(Case,case_id)
        stored = await db.get(WorkflowRun,run_id)
        files = {path:hashlib.sha256(storage.get_bytes(case.s3_prefix,path)).hexdigest()
                 for path in ('0-caseDict/caseDict','0-caseDict/case-events.jsonl','0-caseDict/provenance.jsonl')}
        proof = {'pid':os.getpid(),'case_id':case_id,'run_id':run_id,'revision':case.dict_revision,
                 'state':case.workflow_state,'run_committed':stored is not None,'hashes':files}
    (root/(cut+'-exit.json')).write_text(json.dumps(proof),encoding='utf8')
    os._exit(79 if cut=='before-commit' else 80)

async def create(db,case,*args,**kwargs):
    run,reset = await original_creation(db,case,*args,**kwargs)
    armed = root/'arm-reset-exit'
    if armed.is_file():
        cut = armed.read_text(encoding='utf8')
        armed.unlink()
        if cut=='before-commit':
            await exit_at_commit(case.id,run.id,cut)
        db.info['workflow_reset_exit'] = (case.id,run.id,cut)
    return run,reset
workflow_reset.create_run = create

async def commit(db):
    boundary = db.info.pop('workflow_reset_exit',None)
    await original_commit(db)
    if boundary is not None:
        await exit_at_commit(*boundary)
AsyncSession.commit = commit
'''


def legacy_fixture():
    # Only the real old native job fixture is used. No live model module imports.
    path = SERVER/'live_llm_workflow_plan_migration_acceptance.py'
    for node in ast.parse(path.read_text(encoding='utf8')).body:
        if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='FIXTURE'
                                              for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Legacy workflow fixture is missing')


def until(check, description, seconds=60):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        value = check()
        if value:
            return value
        time.sleep(.08)
    raise TimeoutError(description)


def wait_flow(api, identifier, phase):
    route = '/workflow-runs/'+identifier
    def completed():
        flow = api.call('GET',route)
        if flow['status'] in {'failed','cancelled','interrupted'} and flow['status']!=phase:
            raise AssertionError(flow)
        return flow if flow['status']==phase else None
    return until(completed,'Workflow did not reach '+phase,seconds=90)


def wait_writer(api, identifier):
    def settled():
        job = api.call('GET',f'/jobs/{identifier}')
        if job['status']=='succeeded':
            return bool(job['ended_at'] and not job['completion_pending']
                        and job['attempt']['process_stop']['verified'])
        if job['status']=='failed':
            return api.call('GET',f'/jobs/{identifier}/operations')['reprepare']['available']
        return False
    until(settled,f'Native writer {identifier} did not stop and reconcile')


def source_case(api, action):
    case = api.create_case('Real '+action+' with history')
    script = 'import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(2,2,2)}\n'
    authored = api.put('A-geo/geometry.py',script)
    preview_id = authored.get('auto_preview_job')
    if preview_id is None:
        preview_id = api.call('POST',api.prefix+'/geometry:preview',{})['job_id']
    api.wait_job(preview_id)
    wait_writer(api,preview_id)
    if action=='recompute':
        saved = read_settled(api,api.prefix+'/caseDict')
        parent = api.call('POST',api.prefix+'/workflow-runs',{
            'target':'geometry','expected_revision':saved['dict_revision']})
        ready = wait_flow(api,parent['id'],'awaiting_confirmation')
        step = next(s for s in ready['steps'] if s['key']=='confirm_B')
        api.call('POST',api.prefix+'/caseDict:advance',{'state':'B','confirmed':True,
            'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],
            'expected_step_version':step['version']})
        source = wait_flow(api,parent['id'],'succeeded')
    else:
        api.advance('B')
        api.put('Ref/legacy.py','print("LEGACY_NATIVE_RESET_EXIT",flush=True)\nraise SystemExit(23)\n')
        parent = api.call('POST',api.prefix+'/testing/legacy-workflow',{})
        job = api.wait_job(parent['job_id'],expected='failed')
        assert job['exit_code']==23 and job['attempt']['process_stop']['verified']
        assert 'LEGACY_NATIVE_RESET_EXIT' in job['log_backlog']
        source = wait_flow(api,parent['run_id'],'failed')
    for job in api.call('GET',api.prefix+'/jobs'):
        wait_writer(api,job['id'])
    return case, source


def payload(api, action, source, revision):
    proposal = {'expected_version':source['version'],'expected_revision':revision,
                'confirmed_reset':True,'target':'mesh'}
    if action=='migrate':
        observed = api.call('GET','/workflow-runs/'+source['id']+'/migration')
        assert observed['current_stage']=='B' and observed['current_revision']==revision
        proposal.update(target='geometry',expected_event_sequence=observed['current_event_sequence'],
            expected_source_plan_sha256=observed['source_plan_sha256'],run_dir='C-run/case-1',
            parameters={'geometry_mode':'export'},budget={'timeout_seconds':120,'max_log_bytes':16777216})
    return proposal


def exercise(api, action):
    case, source = source_case(api,action)
    initial = read_settled(api,api.prefix+'/caseDict')
    assert initial['workflow_state']=='B'
    workspace = api.call('GET','/workspaces')[0]['id']
    other = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Independent '+action})
    seed = api.call('POST',f'/fixture/reset-history/{case["id"]}',{'action':action})
    source_route = '/workflow-runs/'+source['id']
    route = source_route+':'+action
    key = uuid.uuid4().hex
    original = payload(api,action,source,initial['dict_revision'])
    def submit():
        return api.call('POST',route,original,extra_headers={'Idempotency-Key':key})

    witness = api.root/(action+'-reading.json')
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(submit)
        try:
            def started():
                if pending.done():
                    raise AssertionError(('Reset did not reach real history',pending.result()))
                return witness.is_file()
            until(started,'Real history was not read')
            reading = json.loads(witness.read_text(encoding='utf8'))
            assert reading['size']==seed['size']==SIZE and 0<reading['received']<SIZE
            with sqlite3.connect(api.root/'test.sqlite',timeout=.3) as db:
                db.execute('BEGIN IMMEDIATE')
            data = {'path':'Ref/during-reset.txt','content':'Independent same-owner save while resetting'}
            other_route = f'/cases/{other["id"]}'
            api.call('PUT',other_route+'/file',data)
            assert api.call('GET',other_route+'/raw?path='+data['path'],raw=True)==data['content'].encode()
            repeat = workers.submit(submit)
            until((api.root/(action+'-parallel-request')).is_file,'Parallel same-key request did not reach API',seconds=5)
        finally:
            (api.root/(action+'-release')).touch()
        result, duplicate = pending.result(45), repeat.result(45)

    identifier = result['id']
    assert duplicate['id']==identifier and identifier!=source['id']
    assert len(api.call('GET',api.prefix+'/workflow-runs'))==2
    expected = initial['dict_revision']+1
    dictionary = read_settled(api,api.prefix+'/caseDict')
    assert dictionary['dict_revision']==expected and dictionary['workflow_state']=='A'
    timeline = api.call('GET',api.prefix+'/raw?path='+TIMELINE,raw=True)
    assert hashlib.sha256(timeline[:SIZE]).hexdigest()==seed['sha256']
    event, = [json.loads(raw) for raw in timeline[SIZE:].splitlines()]
    assert event['type']=='caseDict.updated' and event['revision']==expected
    assert event['previous_state']=='B' and event['workflow_state']=='A'
    provenance = api.call('GET',api.prefix+'/raw?path='+PROVENANCE,raw=True)
    entry = json.loads(provenance.splitlines()[-1])
    assert entry['revision']==expected and entry['casedict_sha256']==hashlib.sha256(dictionary['content'].encode()).hexdigest()
    field = 'recomputation_reset' if action=='recompute' else 'legacy_migration_reset'
    assert result[field]['from_revision']==initial['dict_revision'] and result[field]['to_revision']==expected
    assert submit()['id']==identifier and api.call('GET',source_route)==source
    assert api.call('GET',api.prefix+'/raw?path='+TIMELINE,raw=True)==timeline
    return {'action':action,'root':str(api.root),'source':source['id'],'run':identifier,
            'actual_history_read':reading,'sqlite_writer_available':True,'independent_case_saved':True,
            'concurrent_same_key_returned_one_run':True,'source_preserved':True,
            'revision':expected,'timeline_sha256':hashlib.sha256(timeline).hexdigest()}


def hashes(api):
    return {path:hashlib.sha256(api.call('GET',api.prefix+'/raw?path='+path,raw=True)).hexdigest()
            for path in (PATH,TIMELINE,PROVENANCE)}


def crash_and_read(api, action, cut):
    _case, source = source_case(api,action)
    before = read_settled(api,api.prefix+'/caseDict')
    original_files = hashes(api)
    source_route = '/workflow-runs/'+source['id']
    endpoint = source_route+':'+action
    body = payload(api,action,source,before['dict_revision'])
    key = {'Idempotency-Key':uuid.uuid4().hex}
    (api.root/'arm-reset-exit').write_text(cut,encoding='utf8')
    try:
        api.call('POST',endpoint,body,extra_headers=key)
    except OSError:
        pass
    else:
        raise AssertionError('Reset completed despite the owned API exit')
    expected_commit = cut=='after-commit'
    assert api.proc.wait(timeout=15)==(80 if expected_commit else 79)
    before_pid = api.proc.pid
    proof = json.loads((api.root/(cut+'-exit.json')).read_text(encoding='utf8'))
    assert proof['run_committed']==expected_commit and proof['state']==('A' if expected_commit else 'B')
    revision = before['dict_revision']+int(expected_commit)
    assert proof['revision']==revision and all(proof['hashes'][p]!=original_files[p] for p in original_files)
    api.proc = subprocess.Popen(api.proc.args,cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)

    def health():
        try:
            return api.call('GET','/api/health',api_prefix=False)['ok']
        except OSError:
            return False
    until(health,'Cold API did not become ready',seconds=35)
    assert api.proc.pid!=before_pid
    state = read_settled(api,api.prefix+'/caseDict')
    assert state['dict_revision']==revision and state['workflow_state']==proof['state']
    assert hashes(api)==(proof['hashes'] if expected_commit else original_files)
    runs = api.call('GET',api.prefix+'/workflow-runs')
    assert len(runs)==1+int(expected_commit) and api.call('GET',source_route)==source
    if expected_commit:
        result = api.call('POST',endpoint,body,extra_headers=key)
        assert result['id']==proof['run_id']
        assert hashes(api)==proof['hashes']
    else:
        retried = payload(api,action,source,revision)
        result = api.call('POST',endpoint,retried,extra_headers=key)
        assert result['id']!=proof['run_id']
        assert api.call('GET',api.prefix+'/caseDict')['dict_revision']==revision+1
    return {'root':str(api.root),'action':action,'cut':cut,'observed':proof,
            'actual_exited_launcher':before_pid,'new_launcher':api.proc.pid,'continued_run':result['id']}


def run(backend, report, cuts):
    context = (ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
        volume_max=24,authenticated=True) if backend=='s3' else nullcontext())
    with context as store:
        api = IsolatedServer('workflow-reset-capacity-'+backend,app='workflow_reset_fixture:app')
        env = 'import os\nos.environ.update('+repr(store.settings())+')\n' if store is not None else ''
        fixture = api.root/'workflow_reset_fixture.py'
        fixture.write_text(env+'from app.main import app\n'+legacy_fixture()+OBSERVER+EXIT_OBSERVER,encoding='utf8')
        with api:
            if cuts:
                action = 'recompute' if backend=='fs' else 'migrate'
                for cut in ('before-commit','after-commit'):
                    report.append({'backend':backend,**crash_and_read(api,action,cut)})
                    print('WORKFLOW_RESET_EXIT_PASS',backend,action,cut,flush=True)
            else:
                for action in ('recompute','migrate'):
                    report.append({'backend':backend,**exercise(api,action)})
                    print('WORKFLOW_RESET_CAPACITY_PASS',backend,action,flush=True)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=('fs','s3','all'),default='all')
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--crash',action='store_true')
    args = parser.parse_args()
    enforce([Path(__file__)])
    report = {'passed':False,'actual_model_calls':0,'simulated_model_calls':0,'runs':[]}
    try:
        for backend in ('fs','s3') if args.backend=='all' else (args.backend,):
            run(backend,report['runs'],args.crash)
        report['passed'] = True
    finally:
        args.report.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
