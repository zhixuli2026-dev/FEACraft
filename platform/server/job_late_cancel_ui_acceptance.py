"""Delay a browser cancellation until its real native job finishes."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.request import build_opener, ProxyHandler

from testsupport import IsolatedServer, SERVER


def main():
    web = SERVER.parent/'web'
    node = shutil.which('node')
    with IsolatedServer('job-late-cancel-ui') as api:
        case = api.create_case('Late cancellation')
        user = api.call('GET','/auth/me')
        marker = api.root/'allow-real-job-exit'
        api.put('Ref/finish.py', f"""from pathlib import Path
import time
deadline=time.monotonic()+130
while not Path({str(marker)!r}).exists():
    if time.monotonic()>deadline: raise TimeoutError('release')
    time.sleep(.15)
print('ORIGINAL_RESULT', flush=True)
""")
        job = api.call('POST', api.prefix+'/jobs', {'command':'python Ref/finish.py','input_paths':['Ref/finish.py']})
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        with (api.root/'preview.log').open('w',encoding='utf8') as log:
            preview = subprocess.Popen([node,str(web/'node_modules/vite/bin/vite.js'),'preview','--host',
                '127.0.0.1','--port',str(port),'--strictPort'],cwd=web,env={**os.environ,'VITE_API_TARGET':api.base},
                stdout=log,stderr=subprocess.STDOUT)
            try:
                opener = build_opener(ProxyHandler({}))
                end = time.monotonic()+20
                while time.monotonic()<end and preview.poll() is None:
                    try:
                        with opener.open(origin,timeout=1):break
                    except OSError:time.sleep(.2)
                else:raise TimeoutError('Vite preview unavailable')
                workspace = api.call('GET','/workspaces')[0]
                setup = {'origin':origin,'caseId':case['id'],'userId':user['id'],'workspaceId':workspace['id'],
                         'jobId':job['id'],'marker':str(marker),'folder':str(api.root)}
                subprocess.run([node,str(SERVER/'job_late_cancel_browser.cjs')],check=True,timeout=120,
                    cwd=web,env={**os.environ,'VF_TEST_SESSION':api.token,'VF_TEST_SETUP':json.dumps(setup)})
            finally:
                marker.touch()
                preview.terminate()
                preview.wait(timeout=10)
                final = api.wait_job(job['id'])
                assert 'ORIGINAL_RESULT' in final['log_backlog'] and final['attempt']['process_stop']['verified']


if __name__=='__main__':
    main()
