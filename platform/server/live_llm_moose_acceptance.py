"""Run real MOOSE attempts through the public API and read the published fields."""
import asyncio
import csv
from functools import partial
import hashlib
import json
import re
import subprocess
import time
import uuid
from urllib.parse import urlencode

from live_llm_job_repeat_acceptance import main, put, repeat
from s3testsupport import ObjectStore, SERVER


INPUT = 'C-run/moose/thermal.i'
BASE = INPUT[:-2]+'_out'


def read_solution(api, value):
    def raw(suffix):
        return api.call('GET',api.prefix+'/raw?'+urlencode({'path':BASE+suffix}),raw=True)

    table = raw('.csv')
    records = list(csv.DictReader(table.decode('ascii').splitlines()))
    assert [float(row['time']) for row in records]==[0,1]
    assert float(records[-1]['midpoint'])==value/2
    exodus = raw('.e')
    path = api.root/f'moose-{value}.e'
    path.write_bytes(exodus)
    if path.drive:
        native_path = '/mnt/'+path.drive[0].lower()+path.as_posix()[2:]
        command = ['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',
                   '/opt/vibeflow-ff-c08/moose/bin/ncdump']
    else:
        native_path = str(path)
        command = ['/opt/vibeflow-ff-c08/moose/bin/ncdump']
    dump = subprocess.run([*command,'-p','17,17','-v','time_whole,coordx,vals_nod_var1',native_path],
                          capture_output=True,text=True,encoding='utf-8',check=True,timeout=30).stdout.split('data:',1)[1]
    fields = dict(re.findall(r'^\s*(time_whole|coordx|vals_nod_var1)\s*=\s*([^;]+);',dump,re.M))
    times,positions,temperatures = ([float(item) for item in fields[name].split(',')]
                                    for name in ('time_whole','coordx','vals_nod_var1'))
    assert times==[0,1] and positions==[node/10 for node in range(11)] and len(temperatures)==22
    error = max(abs(y-value*x) for x,y in zip(positions,temperatures[-11:],strict=True))
    assert error<1e-10
    return {'midpoint':value/2,'nodes':len(positions),'max_abs_error':error,
            'csv_sha256':hashlib.sha256(table).hexdigest(),'exodus_sha256':hashlib.sha256(exodus).hexdigest()}


def scenario(api, *, store=None, **_):
    route = api.prefix+'/moose:solve'
    body = {'input_path':INPUT}
    original = (SERVER/'examples/moose/thermal/line.i').read_text(encoding='ascii')
    assert original.count('value = 100')==1
    cases = []
    for boundary in (100,200):
        deck = original.replace('value = 100',f'value = {boundary}')
        assert put(api,INPUT,deck)['ok']
        headers = {'Idempotency-Key':uuid.uuid4().hex}
        if not cases:
            job = api.call('POST',route,body,extra_headers=headers)['job_id']
            replay = partial(api.call,'POST',route,body,extra_headers=headers)
            id_field = 'job_id'
        else:
            first = cases[0]['job']
            selector = api.call('GET',f'/jobs/{first}/operations')
            request = {'action':'rerun','source_attempt_id':selector['source_attempt_id'],'source_version':selector['source_version'],
                'expected_revision':selector['expected_revision'],'expected_event_sequence':selector['expected_event_sequence'],
                'parameters':body}
            replay = partial(repeat,api,first,request,headers['Idempotency-Key'])
            id_field = 'id'
            job = replay()[id_field]
        done = api.wait_job(job,timeout=150)
        attempt = done['attempt']
        assert attempt['handler']['key']=='moose.solve' and attempt['process_stop']['verified']
        assert attempt['outputs']['publication']['status']=='committed'
        children = list(attempt['child_processes']['children'].values())
        assert len(children)==1 and children[0]['stop']['verified']
        if children[0]['purpose']=='moose_solve':
            assert children[0]['identity']['native']['process']['uid']==65534
        else:
            assert children[0]['purpose']=='moose_solve_linux'
        assert attempt['workspace']['files'][INPUT]['sha256']==hashlib.sha256(deck.encode()).hexdigest()
        assert replay()[id_field]==job
        cases.append({'job':job,'native_purpose':children[0]['purpose'],**read_solution(api,boundary)})

    assert put(api,INPUT,'!include /absent-external-model.i\n'+original)['ok']
    rejected = api.call('POST',route,body,expect=400,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
    assert 'Unsupported MOOSE block' in rejected['detail']
    assert read_solution(api,200)['exodus_sha256']==cases[-1]['exodus_sha256']
    assert cases[0]['job']!=cases[-1]['job'] and cases[0]['exodus_sha256']!=cases[-1]['exodus_sha256']
    transient = original.replace('type = Steady','type = Transient\n  dt = 0.01\n  num_steps = 100000').replace(
        '[Kernels]','[Kernels]\n  [time_derivative]\n    type = TimeDerivative\n    variable = u\n  []')
    assert put(api,INPUT,transient)['ok']
    cancelled_job = api.call('POST',route,body)['job_id']
    deadline = time.monotonic()+60
    while True:
        active = api.call('GET',f'/jobs/{cancelled_job}')
        rows = (active['attempt'].get('child_processes') or {}).get('children',{}).values()
        if any(row['phase']=='running' and row['purpose'].startswith('moose_solve') for row in rows):
            break
        assert active['status'] in ('queued','running') and time.monotonic()<deadline, active.get('log_backlog')
        time.sleep(.1)
    assert api.call('POST',f'/jobs/{cancelled_job}:cancel')['ok']
    deadline = time.monotonic()+90
    while True:
        stopped = api.call('GET',f'/jobs/{cancelled_job}')
        cancelled = stopped['attempt']
        rows = (cancelled.get('child_processes') or {}).get('children',{}).values()
        if (stopped['status']=='cancelled' and (cancelled.get('process_stop') or {}).get('verified')
                and len(rows)==1 and all((row.get('stop') or {}).get('verified') for row in rows)):
            break
        assert time.monotonic()<deadline, 'MOOSE cancellation did not provide both stop receipts'
        time.sleep(.15)
    assert (cancelled['outputs'].get('publication') or {}).get('status')!='committed'
    assert read_solution(api,200)['exodus_sha256']==cases[-1]['exodus_sha256']
    record = {'passed':True,'storage':'s3' if store else 'fs','thermal':cases,'same_key_replay':True,
              'external_include_rejected':True,'preserved_previous_artifacts':True,'rerun_current_inputs':True,
              'cancelled_job':cancelled_job,'parent_and_child_stopped':True}
    (api.root/'moose.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf8')
    return record


if __name__=='__main__':
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=scenario,store_volume_max=8,
        store_factory=partial(ObjectStore,executable=binary,volume_max=8,authenticated=True)))
