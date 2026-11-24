"""Exercise final SSE bytes, raw cursors, restart, shared viewing and revocation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import uuid

import httpx

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put
from live_llm_publication_restart_acceptance import FIXTURE as PID_FIXTURE, reboot


FIXTURE = PID_FIXTURE + '''
import asyncio
from app.services import job_log_io
original_log_read = job_log_io.read
async def observed_log_read(selected,prefix,offset,limit,end_offset,**kwargs):
    page = await original_log_read(selected,prefix,offset,limit,end_offset,**kwargs)
    arm = root/'hold-log-job'
    if (arm.exists() and arm.read_text()==str(selected.job_id) and limit==65536
            and not selected.settled and page['chunk']=='FIRST\\n'):
        (root/'old-log-page').touch()
        async with asyncio.timeout(45):
            while not (root/'send-old-log-page').exists(): await asyncio.sleep(.03)
    return page
job_log_io.read = observed_log_read
'''


def stream(api,job,token,suffix='',first_seen=None):
    events = []
    with httpx.Client(trust_env=False,timeout=55,headers={'Authorization':'Bearer '+token}) as client:
        with client.stream('GET',api.base+f'/api/v1/jobs/{job}/log-stream'+suffix) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith('data: '):
                    event = json.loads(line[6:])
                    events.append(event)
                    if first_seen and event['type']=='log': first_seen.touch()
    assert events and events[-1]['type'] in {'done','error'}
    return events


def contents(events):
    return ''.join(e['text'] for e in events if e['type']=='log')


def scenario(api,*,store=None,**_):
    owner = api.token
    prefix = 'FIRST\n'
    tail = ('末尾🌊'*8500)+'\n'
    release = api.root/'emit-last-bytes'
    script = f"""
import sys,time
from pathlib import Path
sys.stdout.buffer.write({prefix.encode()!r})
sys.stdout.buffer.flush()
deadline=time.monotonic()+90
while not Path({str(release)!r}).exists():
    if time.monotonic()>deadline: raise TimeoutError()
    time.sleep(.03)
sys.stdout.buffer.write((('末尾🌊'*8500)+'\\n').encode())
sys.stdout.buffer.flush()
"""
    put(api,'Ref/write-log.py',script)
    def submit():
        return api.call('POST',api.prefix+'/jobs',{'command':'python Ref/write-log.py','runtime':'windows',
                        'input_paths':['Ref/write-log.py'],'timeout_seconds':110},
                        extra_headers={'Idempotency-Key':uuid.uuid4().hex})['id']
    job = submit()
    route = f'/jobs/{job}/log'
    (api.root/'hold-log-job').write_text(str(job))
    with ThreadPoolExecutor(max_workers=1) as pool:
        read = pool.submit(stream,api,job,owner,'?offset=0')
        try:
            wait_for((api.root/'old-log-page').exists,'The actual SSE read never observed the prefix')
            release.touch()
            done = api.wait_job(job)
        finally:
            (api.root/'send-old-log-page').touch()
        events = read.result(timeout=55)
    expected = prefix+tail
    report = {'expected_bytes':len(expected.encode()),'stream_bytes':len(contents(events).encode()),
              'terminal':events[-1],'storage':'s3' if store else 'fs'}
    evidence = api.root/'job-log-stream.json'
    evidence.write_text(json.dumps(report,indent=2),encoding='utf8')
    assert contents(events)==expected and events[-1]['type']=='done'
    assert events[-1]['attempt_id']==done['attempt']['id'] and events[-1]['exit_code']==0
    report['restart_pids'] = reboot(api)
    assert contents(stream(api,job,owner))==expected
    assert contents(stream(api,job,owner,'?offset=6'))==tail
    page = api.call('GET',route+'?offset=6&limit=4')
    assert page['chunk']=='末' and page['offset']==9
    assert page['chunk']+contents(stream(api,job,owner,'?offset='+str(page['offset'])))==tail
    api.call('GET',route+'?offset=9&end_offset=6',expect=422)

    release.unlink()
    held = submit()
    owner_org = api.call('GET','/org/mine')['org']['id']
    ws = '/workspaces/'+str(api.call('GET','/workspaces')[0]['id'])
    email = uuid.uuid4().hex+'@example.com'
    viewer = api.call('POST','/auth/register',{'email':email,'name':'Viewer','password':'isolated-test-123'})['token']
    api.token=None
    api.call('GET',f'/jobs/{held}/log-stream',expect=401)
    api.token=viewer
    api.call('GET',f'/jobs/{held}/log-stream',expect=404)
    api.token=owner
    api.call('POST',f'/org/{owner_org}/members',{'email':email,'role':'viewer'},expect=201)
    api.token=viewer
    api.call('POST',f'/org/{owner_org}/invitation:accept',{})
    api.token=owner
    sharing={'org_id':owner_org,'role':'viewer','enabled':True}
    api.call('PUT',ws+'/sharing',sharing)
    api.token=viewer
    api.call('POST',f'/jobs/{held}:cancel',expect=404)
    api.token=owner
    seen = api.root/'viewer-received-log'
    with ThreadPoolExecutor(max_workers=1) as pool:
        subscribed = pool.submit(stream,api,held,viewer,'?offset=0',seen)
        try:
            wait_for(seen.exists,'Authorized viewer received no initial log')
            api.call('PUT',ws+'/sharing',{**sharing,'enabled':False})
            denied = subscribed.result(timeout=15)
            assert contents(denied)==prefix and denied[-1]=={'type':'error','code':'permission_denied'}
        finally:
            api.call('POST',f'/jobs/{held}:cancel')
    cancelled = stream(api,held,owner)
    assert cancelled[-1]['type']=='done' and cancelled[-1]['status']=='cancelled' and cancelled[-1]['cleanup_verified']
    report.update(sha256=hashlib.sha256(expected.encode()).hexdigest(),cursor_resume_bytes=len(tail.encode()),
                  cancelled=cancelled[-1],viewer_final=denied[-1],viewer_bytes=len(contents(denied).encode()))
    evidence.write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
