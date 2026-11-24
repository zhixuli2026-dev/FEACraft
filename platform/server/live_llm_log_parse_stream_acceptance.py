"""Read actual solver logs through the public endpoint on local and S3 storage."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
import uuid

import httpx

from live_llm_job_repeat_acceptance import main
from live_llm_job_repeat_acceptance import put


FIXTURE = '''
import gc, json, time, tracemalloc
from pathlib import Path
from app.routers import logs
from app.services.logparse import parse_solver_log, parse_solver_log_incremental

root = Path(__file__).parent
parse = logs.parse_log_file
def observed(reader, incremental, offset, fingerprint):
    if (root/'pause-log').exists():
        (root/'log-selected').touch()
        deadline = time.monotonic()+20
        while (root/'pause-log').exists():
            if time.monotonic()>=deadline:
                raise TimeoutError('Selected log version was not released')
            time.sleep(.03)
    if not (root/'measure-log').exists():
        return parse(reader, incremental, offset, fingerprint)
    gc.collect()
    tracemalloc.start()
    try:
        data = reader.read()
        if incremental:
            reference = parse_solver_log_incremental(data, offset, fingerprint)
        else:
            reference = parse_solver_log(data.decode('utf-8', 'replace'))
        baseline = tracemalloc.get_traced_memory()[1]
        del data
        gc.collect()
        tracemalloc.reset_peak()
        result = parse(reader, incremental, offset, fingerprint)
        measured = tracemalloc.get_traced_memory()[1]
        assert reference == result
    finally:
        tracemalloc.stop()
    mode = 'resume' if offset else 'incremental' if incremental else 'full'
    (root/(mode+'-peaks.json')).write_text(
        json.dumps(dict(buffered=baseline, streamed=measured, source_bytes=reader.size)))
    return result
logs.parse_log_file = observed
'''


def scenario(api, *, store=None, **_):
    path = 'Ref/solver-output.log'
    endpoint = api.prefix+'/logs/parse'
    filler = ('  simulation diagnostic with no plot values; 数值验证\n'.encode()*204000)
    first = (b'TimeStep 12: Time = .05\r\n' + filler + b'Time = 0.1\r\n'
             b'Courant Number mean: 0.125 max: 0.25\r\n'
             b'ExecutionTime = 1.25 s\r\nDILU: Solving for Ux, Initial')
    appended = (b' residual = 0.1, Final residual = 1e-3, No Iterations 3\r\n'
                b'Time = 0.2\r\n -- Warning: Courant warning\r\nEnd')
    measure = api.root/'measure-log'
    pause = api.root/'pause-log'

    def read(**query):
        return api.call('GET',endpoint+'?'+urlencode({'path':path,**query}))

    with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                      timeout=60, trust_env=False) as client:
        def upload(data):
            response = client.post(api.prefix+'/upload', params={'path':path},
                                   files={'file':('solver-output.log',data,'application/octet-stream')})
            assert response.status_code == 200, (response.status_code,response.text[:300])
        upload(first)
        measure.touch()
        try:
            full = read()
            incremental = read(incremental='true')
        finally:
            measure.unlink()
        assert full['times'] == [0.1] and incremental['summary']['courant_max_last'] == .25
        assert not full['summary']['finished_cleanly'] and incremental['residuals'] == {}
        cursor = incremental['cursor']
        assert cursor['pending_bytes'] > 0
        assert cursor['fingerprint'] == hashlib.sha256(first[:cursor['offset']]).hexdigest()
        versioned = client.get(api.prefix+'/raw', params={'path':path}, headers={'Range':'bytes=0-26'})
        assert versioned.status_code == 206 and versioned.content == first[:27]
        assert versioned.headers['Content-Range'] == f'bytes 0-26/{len(first)}'
        version = versioned.headers['X-Vibeflow-File-Version']
        upload(first+appended)
        measure.touch()
        try:
            delta = read(incremental='true', offset=cursor['offset'], fingerprint=cursor['fingerprint'])
        finally:
            measure.unlink()
        peaks = {k:json.loads((api.root/(k+'-peaks.json')).read_text()) for k in ('full','incremental','resume')}
        assert all(r['streamed'] < r['buffered']//2 for r in peaks.values()), peaks
        assert delta['times'] == [.2] and delta['residuals'] == {'Ux':[.001]}
        assert not delta['summary']['finished_cleanly'] and delta['summary']['warnings'] == 1
        assert delta['cursor']['pending_bytes'] == 3
        terminal = read()
        assert terminal['summary']['finished_cleanly'] and terminal['times']==[.1,.2]
        unchanged = read(incremental='true', offset=delta['cursor']['offset'],
                         fingerprint=delta['cursor']['fingerprint'])
        assert unchanged['cursor']['unchanged'] and unchanged['cursor']['fingerprint']==delta['cursor']['fingerprint']
        conflict = client.get(api.prefix+'/raw', params={'path':path},
                              headers={'X-Vibeflow-File-Version':version,'Range':'bytes=0-26'})
        assert conflict.status_code==409
        invalid_offset = client.get(endpoint,params={'path':path,'incremental':True,'offset':2,
                                                   'fingerprint':hashlib.sha256(first[:2]).hexdigest()})
        assert invalid_offset.status_code==400 and '完整行' in invalid_offset.json()['detail']

        # Hold an actual parse before opening its pinned object, then replace it
        # through the same owner's business API.
        pause.touch()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(client.get, endpoint, params={'path':path})
            try:
                deadline = time.monotonic()+15
                while not (api.root/'log-selected').exists():
                    if pending.done() or time.monotonic()>=deadline:
                        raise AssertionError('Log selection did not reach the read gate')
                    time.sleep(.03)
                put(api,path,'TimeStep 22: Time = .7\nFOAM FATAL ERROR\n')
                assert not pending.done()
            finally:
                pause.unlink()
            changed = pending.result()
        assert changed.status_code==409 and changed.json()['detail']['code']=='file_changed_during_read'
        current = read()
        assert current['times']==[.7] and current['summary']['fatal_errors']==1
        truncated = client.get(endpoint, params={'path':path,'incremental':True, 'offset':cursor['offset'],
                                                 'fingerprint':cursor['fingerprint']})
        assert truncated.status_code==409 and '截断' in truncated.json()['detail']
        small = b'TimeStep 22: Time = .7\n'
        put(api,path,'TimeStep 22: Time = .8\n')
        rewritten = client.get(endpoint, params={'path':path,'incremental':True,'offset':len(small),
                                                 'fingerprint':hashlib.sha256(small).hexdigest()})
        assert rewritten.status_code==409 and 'fingerprint' in rewritten.json()['detail']
        upload(b'x'*(4*1024*1024+1)+b'\n')
        too_long = client.get(endpoint, params={'path':path})
        assert too_long.status_code==413 and '4 MiB' in too_long.json()['detail']
        registered = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
                                                           'name':'log-reader','password':'isolated-log-pass-123'})
        foreign = client.get(endpoint, params={'path':path}, headers={'Authorization':'Bearer '+registered['token']})
        assert foreign.status_code==404
    report = dict(storage='s3' if store else 'fs', peaks=peaks, old_result_identical=True, tail_rejoined=True,
                  finished_cleanly=True, actual_replacement_http=409, writer_completed_during_read=True,
                  rewrite_http=409,truncation_http=409,invalid_offset_http=400,line_limit_http=413,
                  foreign_account_http=404,raw_range_verified=True)
    (api.root/'log-parse-stream.json').write_text(json.dumps(report,indent=2))
    return report


if __name__ == '__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE,store_volume_max=6))
