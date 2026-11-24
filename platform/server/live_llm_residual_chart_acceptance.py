"""Generate real Windows job logs and verify their chart and parsed summaries."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote, urlencode
import uuid

from live_llm_job_repeat_acceptance import main, put


NAME = '残差曲线验收'
NORMAL = '''Time = 0.1
Courant Number mean: 1e-1 max: 5e-1
smoothSolver: Solving for Ux, Initial residual = 1e-1, Final residual = 1e-2, No Iterations 2
GAMG: Solving for p, Initial residual = 1e-1, Final residual = 1e-3, No Iterations 4
Time = 0.2
DILU: Solving for Ux, Initial residual = 1e-2, Final residual = 1e-4, No Iterations 2
PBiCGStab: Solving for p, Initial residual = 1e-2, Final residual = 1e-3, No Iterations 1
ExecutionTime = 2 s
End
'''
ZERO = '''TimeStep 22: Time = .7
GAMG: Solving for Ux, Initial residual = 0, Final residual = 0, No Iterations 0
Warning: diagnostic warning
FOAM FATAL ERROR: workbench diagnostic fixture
ExecutionTime = 1.25 s
'''
LONG = '''print('Time = 1')
for index in range(5000):
    # Spikes near the middle and at arbitrary indices must survive sampling.
    value = 1e-2 if index == 2221 else 1e-8 if index == 3127 else 1e-5
    print(f'DILU: Solving for p, Initial residual = 1e-2, Final residual = {value:.1e}, No Iterations 1')
print('End')
'''


def scenario(api, *, store=None, **_):
    api.create_case(NAME)
    jobs = {}
    for label, program in [('normal', f'print({NORMAL!r}, end="")\n'),
                           ('zero', f'print({ZERO!r}, end="")\nraise SystemExit(2)\n'),
                           ('long', LONG)]:
        path = 'Ref/residual-'+label+'.py'
        put(api, path, program)
        command = f'"{sys.executable}" "{path}"'
        submitted = api.call('POST',api.prefix+'/jobs',{'type':'solve','command':command,
                              'runtime':'windows','timeout_seconds':80},
                              extra_headers={'Idempotency-Key':uuid.uuid4().hex})
        jobs[label] = api.wait_job(submitted['id'], expected='failed' if label=='zero' else 'succeeded')
    paths = {label:job['log_path'] for label,job in jobs.items()}
    primary = jobs['normal']['parsed_summary']
    failed = jobs['zero']['parsed_summary']
    assert primary['times']==[.1,.2] and primary['summary']['courant_max_last']==.5
    assert primary['summary']['execution_time_last_s']==2 and primary['summary']['finished_cleanly']
    assert primary['residuals']=={'Ux':[.01,.0001],'p':[.001,.001]}
    assert failed['times']==[.7] and failed['residuals']=={'Ux':[0]}
    assert failed['summary']['fatal_errors']==1 and failed['summary']['warnings']==1
    assert not failed['summary']['finished_cleanly']
    assert jobs['long']['parsed_summary']['summary']['iterations_total']==5000
    profile = dict(base=api.base,token=api.token,case=NAME,output=str(api.root),
        normal_encoded_path=quote(paths['normal'],safe=''),jobs={key:job['id'] for key,job in jobs.items()})
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],str(Path(__file__).with_name('residual_chart_browser.py'))],
                   input=json.dumps(profile), text=True, check=True, timeout=200)
    browser = json.loads((api.root/'residual-browser.json').read_text())
    outsider = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'password':'isolated-residual-123','name':'其他组织账号'})
    api.call('GET',api.prefix+'/logs/parse?'+urlencode({'path':paths['normal']}),expect=404,
             extra_headers={'Authorization':'Bearer '+outsider['token']})
    record = dict(storage='s3' if store else 'fs',windows_jobs=profile['jobs'],native_results=True,
        exact_summary=True,cross_account_http=404,browser=browser)
    (api.root/'residual-chart.json').write_text(json.dumps(record,indent=2))
    return record


if __name__=='__main__':
    web = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (web/'index.html').is_file()
    mount = "\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(web))+",html=True))\n"
    asyncio.run(main(selected_override=scenario, fixture_override=mount, store_volume_max=6))
