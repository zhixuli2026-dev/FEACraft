"""Drive Chromium and a real Windows log writer through incremental chart states."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from live_llm_job_repeat_acceptance import main, put


NAME = '残差曲线增量验收'
FIXTURE = '''
from fastapi import Depends
from pathlib import Path
from app.db import get_db
from app.deps import get_current_user
from app.routers.jobs import _get_job
from app.services.jobrunner import get_proc

@app.post('/api/v1/testing/rewrite-job-log/{job_id}')
async def rewrite_log(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    await _get_job(job_id,user,db)
    process = get_proc(job_id)
    assert process is not None and process.public_status=='running'
    path = process.log_path.resolve()
    assert path.is_relative_to((Path(__file__).parent/'files').resolve())
    replacement = b'Time = 3.1\\r\\nDIC: Solving for changed, Initial residual = .1, Final residual = 7e-3, No Iterations 1\\r\\n'
    with path.open('r+b') as stream:
        stream.write(replacement)
        stream.truncate()
    return {'bytes':len(replacement)}
'''

PROGRAM = '''import sys
import time
from pathlib import Path

root = Path({root!r})

def emit(text):
    sys.stdout.write(text)
    sys.stdout.flush()

def wait(marker):
    deadline = time.monotonic()+95
    while not (root/marker).exists():
        if time.monotonic()>deadline:
            raise TimeoutError(marker)
        time.sleep(.035)

emit('TimeStep 4: Time = .1\\nCourant Number mean: .2 max: .5\\nWarning: live notice\\n'
     'DILU: Solving for Ux, Initial residual = .1, Final residual = .01, No Iterations 2\\n'
     'PCG: Solving for Ux, Initial')
wait('second-sample')
emit(' residual = .01, Final residual = .001, No Iterations 3\\nTime = .2\\n')
wait('third-sample')
emit('TimeStep 8: Time = .9\\nGAMG: Solving for p, Initial residual = .01, Final residual = .0001, No Iterations 2\\n')
wait('finish-sample')
emit('Time = 4\\nExecutionTime = 2 s\\nEnd')
'''


def scenario(api, *, store=None, **_):
    api.create_case(NAME)
    path = 'Ref/live-residual.py'
    put(api, path, PROGRAM.format(root=str(api.root)))
    submitted = api.call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'windows',
        'command':f'"{sys.executable}" "{path}"','timeout_seconds':130},
        extra_headers={'Idempotency-Key':uuid.uuid4().hex})
    job_id = submitted['id']
    outsider = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'password':'isolated-residual-foreign-123','name':'Unrelated account'})
    api.call('GET',f'/jobs/{job_id}/log/parse',expect=404,
        extra_headers={'Authorization':'Bearer '+outsider['token']})
    configuration = dict(base=api.base,token=api.token,case=NAME,output=str(api.root),job_id=job_id)
    try:
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],
            str(Path(__file__).with_name('residual_cursor_browser.py'))],
            input=json.dumps(configuration),text=True,check=True,timeout=145)
    finally:
        for gate in ('second-sample','third-sample','finish-sample'):
            (api.root/gate).touch()
    finished = api.wait_job(job_id,timeout=50)
    api.call('GET',f'/jobs/{job_id}/log/parse?offset=1&fingerprint=short',expect=400)
    assert finished['parsed_summary']['times']==[3.1,4]
    assert finished['parsed_summary']['residuals']=={'changed':[.007]}
    result = {'storage':'s3' if store else 'fs','job':job_id,'foreign_http':404,'bad_cursor_http':400,
        'terminal_summary':finished['parsed_summary'],'browser':json.loads((api.root/'residual-cursor-browser.json').read_text())}
    (api.root/'residual-cursor.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    web = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (web/'index.html').is_file()
    mount = "\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(web))+",html=True))\n"
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE+mount, store_volume_max=6))
