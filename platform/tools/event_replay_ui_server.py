"""Disposable actual UI/API plus deterministic event fault fixtures."""
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

PLATFORM=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(PLATFORM/'server'))
from testsupport import IsolatedServer
from event_replay_smoke import FIXTURE

EXTRA='''
@app.post('/api/v1/fixture/job/{case_id}')
async def job_fixture(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 await get_owned_case(case_id,user,db)
 job=await db.scalar(select(Job).where(Job.case_id==case_id,Job.command=='Cursor delivery acceptance'))
 if job is None:
  job=Job(case_id=case_id,type='shell',command='Cursor delivery acceptance',status='running');db.add(job);await db.flush()
 job.status=body['status']
 if job.status in {'failed','succeeded'}:job.ended_at=now();job.exit_code=0 if job.status=='succeeded' else 1
 event=await append_event(db,case_id,'jobs.updated',{'job_id':job.id,'status':job.status})
 await db.commit();return {'job_id':job.id,'event_id':event.id,'seq':event.seq}
'''

server=IsolatedServer('event-browser',app='event_fixture:app')
(server.root/'event_fixture.py').write_text(FIXTURE+EXTRA,encoding='utf8')
with server as api:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with (api.root/'web.log').open('w',encoding='utf8') as log:
        web=subprocess.Popen(['node',str(PLATFORM/'web/node_modules/vite/bin/vite.js'),
            '--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=PLATFORM/'web',
            env=dict(api.env,VITE_API_TARGET=api.base),stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            url=f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:urllib.request.urlopen(url,timeout=1).close();break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('Disposable UI unavailable')
            print(f'UI_URL={url} STOP_FILE={api.root/"stop"}',flush=True)
            for _ in range(1200):
                if (api.root/'stop').exists():break
                if web.poll() is not None:raise RuntimeError('UI stopped')
                time.sleep(1)
        finally:web.terminate();web.wait(timeout=10)
