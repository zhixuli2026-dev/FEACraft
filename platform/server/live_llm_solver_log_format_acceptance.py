"""Verify actual Windows job summaries and incremental logs across API restart."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sys
import time
from urllib.parse import urlencode
import uuid

from live_llm_job_repeat_acceptance import main, put, write
from live_llm_publication_restart_acceptance import FIXTURE, reboot


LEGACY = '''
from fastapi import Depends
from app.db import get_db
from app.deps import get_current_user
from app.models import Job
from app.routers.workspaces import get_owned_case

@app.post('/api/v1/testing/legacy-summary/{case_id}')
async def legacy_summaries(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    await get_owned_case(case_id,user,db)
    rows = [Job(case_id=case_id,type='solve',command='historical job',status='failed',log_s3_key='',
        parsed_summary=value) for value in ('{"times":[NaN]}','{"times":[1e999]}','{"times":[0.25]}')]
    db.add_all(rows)
    await db.commit()
    return [row.id for row in rows]
'''


SOLVERS = ('smoothSolver', 'GAMG', 'GAMGPCG', 'GAMGPAI', 'PCG', 'PBiCGStab', 'DIC', 'DILU')
CONVERGED = '\n'.join(f'{solver}: Solving for U.{index}, Initial residual = .1, '
    f'Final residual = {index+1}e-4, No Iterations {index+1}' for index, solver in enumerate(SOLVERS))
MIXED = f'''TimeStep 22: Time = .7
TimeStep 23: Time = damaged
Time = signal-lost
Time = 1e999
Time = .2
Courant Number mean: nan max: 10
Courant Number mean: .12 max: .25
GAMG: Solving for broken, Initial residual = 1e+, Final residual = .1, No Iterations 4
GAMG: Solving for divergent, Initial residual = .1, Final residual = 1e999, No Iterations 4
{CONVERGED}
ExecutionTime = e+ s
ExecutionTime = 2.75 s
Warning: one input diagnostic
'''
FAILED = '''TimeStep 99: Time = 9e999
TimeStep 100: Time = .8
Courant Number mean: 0 max: .5
DILU: Solving for p, Initial residual = 1e-2, Final residual = 2e-4, No Iterations 3
ExecutionTime = .3 s
FOAM FATAL ERROR: isolated solver failure
'''


def scenario(api, *, store=None, **_):
    pids = []
    record = {'storage': 's3' if store else 'fs', 'restart_pids': pids}
    report_path = api.root/'solver-log-format.json'

    def save():
        report_path.write_text(json.dumps(record, indent=2), encoding='utf8')

    def submit(prefix, name, program):
        name = f'Ref/{name}.py'
        write(api,'PUT',prefix+'/file',{'path':name,'content':program})
        key = uuid.uuid4().hex
        until = time.monotonic()+30
        while True:
            reply = api.call('POST',prefix+'/jobs',{'type':'solve', 'runtime':'windows',
                'command':f'"{sys.executable}" "{name}"','timeout_seconds':80},
                expect=(200,409),extra_headers={'Idempotency-Key':key})
            if 'id' in reply:
                return reply['id']
            assert reply['detail']['code'] in {'storage_quota_busy','validation_busy'}, reply['detail']
            if time.monotonic()>=until:
                raise TimeoutError('Solver job admission stayed busy')
            time.sleep(.15)

    def parse(prefix, path, *, expect=200, **query):
        return api.call('GET',prefix+'/logs/parse?'+urlencode({'path':path,**query}),expect=expect)

    workspace = api.call('GET','/workspaces')[0]['id']
    failed_case = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Independent failing log'})['id']
    failed_prefix = f'/cases/{failed_case}'
    normal_id = submit(api.prefix,'mixed-solver-output',f'print({MIXED+"End"!r}, end="")\n')
    failed_id = submit(failed_prefix,'failing-solver-output',f'print({FAILED!r}, end="")\nraise SystemExit(7)\n')
    normal = api.wait_job(normal_id)
    failed = api.wait_job(failed_id,expected='failed')
    valid = normal['parsed_summary']
    broken = failed['parsed_summary']
    record.update(normal_job=normal_id,failed_job=failed_id,successful_summary=valid,failed_summary=broken)
    save()
    assert valid.get('schema')=='vibeflow.solver-log.v1', valid
    assert valid['times']==[.2] and valid['courant']==[[.12,.25]]
    assert valid['summary']['iterations_total']==len(SOLVERS)
    assert valid['summary']['finished_cleanly'] and valid['summary']['execution_time_last_s']==2.75
    assert valid['summary']['warnings']==1 and valid['summary']['fatal_errors']==0
    assert valid['residuals']=={f'U.{i}':[float(f'{i+1}e-4')] for i in range(len(SOLVERS))}
    assert all(valid['residuals_detail'][f'U.{i}'][0]['iterations']==i+1 for i in range(len(SOLVERS)))
    assert failed['exit_code']==7 and broken['times']==[.8] and broken['residuals']=={'p':[.0002]}
    assert broken['summary']['fatal_errors']==1 and not broken['summary']['finished_cleanly']
    assert parse(api.prefix,normal['log_path'])==valid
    assert parse(failed_prefix,failed['log_path'])==broken
    record['exact_job_and_parser_match'] = True

    large_id = submit(api.prefix,'oversized-diagnostic',
        'import sys\nprint("Time = 1")\nprint("x" * (4 * 1024 * 1024 + 7))\n'
        'print("Time = 2")\nsys.stdout.write("End")\n')
    large = api.wait_job(large_id,timeout=120)
    assert large['parsed_summary']['times']==[1,2] and large['parsed_summary']['summary']['finished_cleanly']
    parse(api.prefix,large['log_path'],expect=413)
    record.update(oversized_job=large_id,oversized_summary_kept_other_lines=True,oversized_parse_http=413)

    old = api.call('POST',f'/testing/legacy-summary/{api.case_id}')
    for identifier in old[:2]:
        state = api.call('GET',f'/jobs/{identifier}')
        assert state['parsed_summary'] is None and state['record_issues'][0]['field']=='summary'
    assert api.call('GET',f'/jobs/{old[2]}')['parsed_summary']=={'times':[.25]}
    listed = {job['id']:job for job in api.call('GET',api.prefix+'/jobs')}
    assert all(listed[identifier]['parsed_summary'] is None for identifier in old[:2])
    record['legacy_records_remain_readable'] = True
    save()

    # A cursor handed out before stopping the first API remains valid afterward.
    prefix = 'TimeStep 4: Time = .1\r\n诊断\r\n'
    partial = 'DILU: Solving for p, Initial'
    path = 'Ref/incremental-format.log'
    put(api,path,prefix+partial)
    start = parse(api.prefix,path,incremental='true')
    cursor = start['cursor']
    assert start['times']==[.1] and start['residuals']=={}
    assert cursor['offset']==len(prefix.encode()) and cursor['pending_bytes']==len(partial)
    assert cursor['fingerprint']==hashlib.sha256(prefix.encode()).hexdigest()
    query = {'incremental':'true','offset':cursor['offset'],'fingerprint':cursor['fingerprint']}
    pids.extend(reboot(api))
    assert len(pids)==2 and pids[0]!=pids[1]
    assert api.call('GET',f'/jobs/{normal_id}')['parsed_summary']==valid
    assert api.call('GET',f'/jobs/{failed_id}')['parsed_summary']==broken
    with ThreadPoolExecutor(max_workers=3) as parallel:
        reads = [parallel.submit(parse,api.prefix,path,**query) for _ in range(3)]
        assert all(result.result()['cursor']['unchanged'] for result in reads)
    new_text = prefix+partial+' residual = 1e-2, Final residual = 2e-4, No Iterations 1\r\n'
    new_text += 'Time = 1e999\r\nTime = .2\r\nEnd'
    put(api,path,new_text)
    resumed = parse(api.prefix,path,**query)
    assert resumed['times']==[.2] and resumed['residuals']=={'p':[.0002]}
    assert resumed['cursor']['pending_bytes']==3 and not resumed['summary']['finished_cleanly']
    assert resumed['cursor']['fingerprint']==hashlib.sha256(new_text.encode()[:-3]).hexdigest()
    put(api,path,new_text.replace('TimeStep 4','TimeStep 9'))
    parse(api.prefix,path,expect=409,**query)
    put(api,path,'Time = 0\n')
    parse(api.prefix,path,expect=409,**query)
    parse(api.prefix,path,expect=400,incremental='true',offset=1,fingerprint='invalid')

    outsider = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'password':'isolated-foreign-123','name':'Unrelated log reader'})
    headers = {'Authorization':'Bearer '+outsider['token']}
    api.call('GET',f'/jobs/{normal_id}',expect=404,extra_headers=headers)
    api.call('GET',api.prefix+'/logs/parse?'+urlencode({'path':path}),expect=404,extra_headers=headers)
    record.update(restart_preserves_summaries=True,old_cursor_resumed=True,independent_concurrent_readers=3,
        foreign_http=404,malformed_fingerprint_http=400,rewrites_and_truncations_http=409)
    save()
    return record


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE+LEGACY,store_volume_max=6))
