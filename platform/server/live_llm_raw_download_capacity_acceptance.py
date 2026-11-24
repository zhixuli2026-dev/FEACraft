"""Real raw downloads, request/transfer limits and version-preserving rollback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote

import httpx

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put


FIXTURE = '''
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import time
from fastapi import Depends
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.routers import files
from app.routers.workspaces import get_owned_case
from app.services.file_read import FileRead

original_open = FileRead._open
mode = ContextVar('raw_fixture_mode', default='')

def gate(name):
    flag = root/(name+'-hold')
    if flag.exists():
        (root/(name+'-reached')).touch()
        limit = time.monotonic()+20
        while flag.exists():
            if time.monotonic()>=limit:
                raise TimeoutError('Raw test gate was not released')
            time.sleep(.02)

@contextmanager
def observed_open(reader, start=None, end=None):
    path = reader.location['Key'] if reader.remote else str(reader.path)
    label = mode.get()
    if label == 'replace':
        gate('replace')
    with original_open(reader, start, end) as (stream, remaining):
        if 'raw-capacity.bin' not in path:
            yield stream, remaining
            return
        size_file = root/'read-size.jsonl'
        class ReadObserver:
            def read(self, count):
                if label == 'timeout':
                    gate('timeout')
                with size_file.open('a', encoding='utf-8') as trace:
                    trace.write(json.dumps({'offset':start,'requested':count})+'\\n')
                return stream.read(count)
        yield ReadObserver(), remaining
FileRead._open = observed_open

original_deadline = files.deadline_scope
def deadline(seconds):
    return original_deadline(2 if mode.get()=='timeout' else seconds)
files.deadline_scope = deadline

class RawFault:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type']!='http' or not scope.get('path','').endswith('/raw'):
            return await self.app(scope,receive,send)
        label = dict(scope.get('headers',[])).get(b'x-isolated-raw-test',b'').decode('ascii')
        token = mode.set(label)
        async def gate_send(message):
            if label in {'wire0','wire1','wire2','wire3'} and message['type']=='http.response.body':
                flag = root/(label+'-hold')
                if flag.exists():
                    (root/(label+'-reached')).touch()
                    try:
                        while flag.exists():
                            await asyncio.sleep(.02)
                    finally:
                        (root/(label+'-finished')).touch()
            return await send(message)
        try:
            await self.app(scope,receive,gate_send)
        finally:
            mode.reset(token)
app.add_middleware(RawFault)

@app.get('/api/v1/testing/raw-prefix/{case_id}')
async def raw_prefix(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db,write=False)
    return {'prefix':case.s3_prefix}

original_limit = get_settings().node_storage_limit_mb
@app.post('/api/v1/testing/raw-node-limit/{case_id}')
async def raw_node_limit(case_id: int, full: bool, user=Depends(get_current_user), db=Depends(get_db)):
    await get_owned_case(case_id,user,db)
    get_settings().node_storage_limit_mb = 1 if full else original_limit
    return {'ok':True}
'''


def scenario(api, *, store=None, **_):
    case = api.prefix
    root = api.root
    fs = Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])
    prefix = api.call('GET',f'/testing/raw-prefix/{api.case_id}')['prefix']
    path = 'Ref/raw-capacity.bin'
    replacement = 'Ref/version-conflict.txt'
    large = case+'/raw?path='+quote(path)
    changed = case+'/raw?path='+quote(replacement)
    body = hashlib.shake_256(b'raw-capacity-sample').digest(8*1024*1024+41)
    expected = hashlib.sha256(body).hexdigest()
    result = {'storage':'s3' if store else 'fs','checks':{}}

    def save():
        (root/'raw-download-capacity.json').write_text(json.dumps(result,indent=2),encoding='utf-8')

    if store is None:
        (fs/prefix/path).write_bytes(body)
    else:
        store.client.put_object(Bucket=store.bucket,Key=prefix+'/'+path,Body=body)
    put(api,replacement,'first-version')

    def claims():
        return sorted(json.loads(file.read_text(encoding='utf-8'))['bytes'] for file in
                      (fs/'.node-storage-budget').glob('*.json'))

    def phases(label, state):
        return root/(label+'-'+state)

    def observed_calls():
        trace = root/'read-size.jsonl'
        return [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.exists() else []

    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      timeout=35,trust_env=False) as client:
        before = len(observed_calls())
        head = client.head(large)
        assert head.status_code==200 and not head.content and int(head.headers['content-length'])==len(body)
        assert len(observed_calls())==before, 'HEAD unexpectedly read the body'

        response = client.get(large)
        assert response.status_code==200 and hashlib.sha256(response.content).hexdigest()==expected
        reads = observed_calls()[before:]
        result['full_body'] = {'bytes':len(body),'digest':expected,'source_blocks':len(reads),
                               'largest_source_read':max(row['requested'] for row in reads)}
        save()
        assert result['full_body']['largest_source_read']<=65536 and len(reads)>1
        assert sum(row['requested'] for row in reads)==len(body)
        del response
        wait_for(lambda: not claims(),'Full response retained disk claim')

        version = head.headers['x-vibeflow-file-version']
        span = client.get(large,headers={'Range':'bytes=123-1048730','X-Vibeflow-File-Version':version})
        assert span.status_code==206 and span.content==body[123:1048731]
        range_header = {'Range':'bytes=9-42,70000-70049'}
        multi = client.get(large,headers=range_header)
        assert multi.status_code==206 and multi.headers['content-type'].startswith('multipart/byteranges;')
        boundary = multi.headers['content-type'].split('boundary=',1)[1].encode()
        parts = multi.content.split(b'--'+boundary)
        assert [part.split(b'\r\n\r\n',1)[1][:-2] for part in parts[1:-1]] == [body[9:43],body[70000:70050]]
        head_multi = client.head(large,headers=range_header)
        assert head_multi.status_code==206 and len(multi.content)==int(head_multi.headers['content-length'])
        invalid = client.get(large,headers={'Range':'bytes=800-900,880-1000'})
        assert invalid.status_code==416 and invalid.headers['content-range']==f'bytes */{len(body)}'
        result['checks']['head_and_ranges'] = True

        limit = f'/testing/raw-node-limit/{api.case_id}'
        api.call('POST',limit+'?full=true',{})
        try:
            count = len(observed_calls())
            denied = client.get(large)
            assert denied.status_code==409 and denied.json()['detail']['code']=='node_storage_full'
            assert len(observed_calls())==count and not claims()
        finally:
            api.call('POST',limit+'?full=false',{})
        result['checks']['insufficient_disk_refused_before_body_read'] = True

        # A paused open is after metadata selection. A cooperating replacement
        # on the same case must commit while the read worker is paused.
        old = client.head(changed).headers['x-vibeflow-file-version']
        phases('replace','hold').touch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            waiting = pool.submit(client.get,changed,headers={'X-Isolated-Raw-Test':'replace'})
            try:
                wait_for(phases('replace','reached').exists,'Reader did not pin before replacement')
                health = client.get(api.base+'/api/health',timeout=3)
                assert health.json()['ok'] and not waiting.done()
                writer = pool.submit(put,api,replacement,'other-version')
                assert writer.result(timeout=8)['ok'] and not waiting.done()
            finally:
                phases('replace','hold').unlink(missing_ok=True)
            conflict = waiting.result(timeout=15)
        assert conflict.status_code==409 and conflict.json()['detail']['code']=='file_changed_during_read'
        assert client.get(changed,headers={'X-Vibeflow-File-Version':old}).status_code==409
        assert client.get(changed).content==b'other-version'
        result['checks']['replacement_before_response_and_same_case_writer'] = True

        wire = []
        with ExitStack() as streams:
            try:
                for i in range(4):
                    label = f'wire{i}'
                    wire.append(label)
                    phases(label,'hold').touch()
                    held = streams.enter_context(client.stream('GET',large,headers={'X-Isolated-Raw-Test':label}))
                    assert held.status_code==200 and len(body)==int(held.headers['content-length'])
                    wait_for(phases(label,'reached').exists,'Expected actual first send gate')
                assert claims()==[len(body)]*4
                fifth = client.get(large)
                assert fifth.status_code==429 and fifth.json()['detail']['code']=='raw_download_busy'
                assert put(api,'Ref/while-downloading.txt','actual write under active transfers')['ok']
            finally:
                streams.close()  # Real sockets disconnect before the gated body completes.
                for label in wire:
                    phases(label,'hold').unlink(missing_ok=True)
        wait_for(lambda: not claims(),'Disconnect left active disk claims',25)
        result['checks']['wire_admission_and_disconnect'] = {'active_transfers':4, 'fifth_status':429,
            'claim_for_each_bytes':len(body),'all_claims_released':True}
        save()

        phases('timeout','hold').touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(client.get,large,headers={'X-Isolated-Raw-Test':'timeout'})
            try:
                wait_for(phases('timeout','reached').exists,'Read never reached timeout gate',10)
                time.sleep(2.1)
                assert not waiting.done() and claims()==[len(body)], 'Timeout released unfinished I/O'
            finally:
                phases('timeout','hold').unlink(missing_ok=True)
            expired = waiting.result(timeout=15)
        assert expired.status_code==504 and expired.json()['detail']['code']=='raw_download_timeout'
        wait_for(lambda: not claims(),'Timeout left active disk claim')
        result['checks']['timeout_drains_before_cleanup'] = True
        assert client.head(large).headers['x-vibeflow-file-version']==version

    stranger = api.call('POST','/auth/register',{'name':'Foreign download test',
        'email':str(time.time_ns())+'@example.com','password':'raw-capacity-stranger-123'})['token']
    api.call('GET',large,expect=404,extra_headers={'Authorization':'Bearer '+stranger})
    result['checks']['foreign_read_refused'] = True
    save()
    return result


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
