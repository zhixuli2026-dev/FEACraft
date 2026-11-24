"""Verify real Code_Aster solves, published MED displacements and cancellation."""
import asyncio
from functools import partial
import hashlib
import json
import re
import subprocess
import time
from urllib.parse import urlencode
import uuid

from live_llm_job_repeat_acceptance import main, put, repeat
from s3testsupport import ObjectStore, SERVER


FOLDER = 'C-run/code-aster/'
INPUT = FOLDER+'axial.export'
MED = FOLDER+'axial.rmed'
H5DUMP = '/opt/vibeflow-ff-c08/code-aster/bin/h5dump'
FIELD = '/CHA/solutionDEPL/0000000000000000000100000000000000000001/NOE/MED_NO_PROFILE_INTERNAL/CO'
PROBE = '''from pathlib import Path
assert not Path('/mnt/d').exists()
status = Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in status and 'NoNewPrivs:\\t1' in status
routes = Path('/proc/net/route').read_text().strip().splitlines()
assert len(routes) <= 1
print('VIBEFLOW_CODE_ASTER_CONFINED=1', flush=True)

'''


def raw(api, path):
    return api.call('GET',api.prefix+'/raw?'+urlencode({'path':path}),raw=True)


def published_solution(api, expected, name):
    result = raw(api,MED)
    message = raw(api,FOLDER+'axial.mess').decode('utf8')
    assert re.search(r'^VIBEFLOW_CODE_ASTER_CONFINED=1$',message,re.M)
    path = api.root/(name+'.rmed')
    path.write_bytes(result)
    command = [H5DUMP]
    native = str(path)
    if path.drive:
        command = ['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',H5DUMP]
        native = '/mnt/'+path.drive[0].lower()+path.as_posix()[2:]
    dump = subprocess.run([*command,'-y','-m','%.17g','-d',FIELD,native],capture_output=True,text=True,
                          check=True,encoding='utf8',timeout=30).stdout
    values = [float(item) for item in re.search(r'\bDATA\s*\{([^}]+)\}',dump).group(1).split(',')]
    assert len(values)==6 and values[:1]+values[2:]==[0]*5
    error = abs(values[1]-expected)
    assert error<1e-12
    return {'tip_m':values[1],'expected_tip_m':expected,'absolute_error_m':error,
            'med_sha256':hashlib.sha256(result).hexdigest(),'message_sha256':hashlib.sha256(message.encode()).hexdigest()}


def completed(api, job, deck):
    attempt = api.wait_job(job,timeout=120)['attempt']
    assert attempt['handler']['key']=='code-aster.solve' and attempt['process_stop']['verified']
    child, = attempt['child_processes']['children'].values()
    assert child['purpose']=='code_aster_solve' and child['stop']['verified']
    assert child['identity']['native']['process']['uid']==65534
    assert attempt['outputs']['publication']['status']=='committed'
    assert {INPUT,FOLDER+'axial.comm',FOLDER+'axial.mail'} <= set(attempt['workspace']['files'])
    assert attempt['workspace']['files'][FOLDER+'axial.comm']['sha256']==hashlib.sha256(deck.encode()).hexdigest()


def scenario(api, *, store=None, **_):
    fixture = SERVER/'examples/code_aster/axial'
    native = PROBE+(fixture/'axial.comm').read_text(encoding='ascii')
    export = (fixture/'axial.export').read_text(encoding='ascii')
    mesh = (fixture/'axial.mail').read_text(encoding='ascii')
    assert put(api,INPUT,export)['ok'] and put(api,FOLDER+'axial.mail',mesh)['ok']
    assert put(api,FOLDER+'axial.comm',native)['ok']
    route = api.prefix+'/code-aster:solve'
    assert put(api,FOLDER+'invalid.export',export.replace('axial.mail','../foreign.mail'))['ok']
    assert api.call('POST',route,{'input_path':FOLDER+'invalid.export'},expect=400)['detail']
    body = {'input_path':INPUT}
    headers = {'Idempotency-Key':uuid.uuid4().hex}
    first = api.call('POST',route,body,extra_headers=headers)['job_id']
    completed(api,first,native)
    first_result = published_solution(api,.001,'first')
    assert api.call('POST',route,body,extra_headers=headers)['job_id']==first

    changed = native.replace('FX=2.1e6','FX=4.2e6')
    assert changed!=native and put(api,FOLDER+'axial.comm',changed)['ok']
    option = api.call('GET',f'/jobs/{first}/operations')
    rerun = {'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
             'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence'],
             'parameters':body}
    key = uuid.uuid4().hex
    second = repeat(api,first,rerun,key)['id']
    completed(api,second,changed)
    second_result = published_solution(api,.002,'second')
    assert repeat(api,first,rerun,key)['id']==second

    delayed = changed.replace("DEBUT(LANG='EN')", "DEBUT(LANG='EN')\nimport time\ntime.sleep(40)")
    assert delayed!=changed and put(api,FOLDER+'axial.comm',delayed)['ok']
    cancel_job = api.call('POST',route,body)['job_id']
    deadline = time.monotonic()+45
    while True:
        active = api.call('GET',f'/jobs/{cancel_job}')
        rows = (active['attempt'].get('child_processes') or {}).get('children',{}).values()
        if any(row['phase']=='running' and row['purpose']=='code_aster_solve' for row in rows):
            break
        assert active['status'] in ('queued','running') and time.monotonic()<deadline, active.get('log_backlog')
        time.sleep(.15)
    time.sleep(2)
    assert api.call('GET',f'/jobs/{cancel_job}')['status']=='running'
    assert api.call('POST',f'/jobs/{cancel_job}:cancel')['ok']
    deadline = time.monotonic()+90
    while True:
        done = api.call('GET',f'/jobs/{cancel_job}')
        attempt = done['attempt']
        rows = (attempt.get('child_processes') or {}).get('children',{}).values()
        if (done['status']=='cancelled' and (attempt.get('process_stop') or {}).get('verified')
                and len(rows)==1 and all((row.get('stop') or {}).get('verified') for row in rows)):
            break
        assert time.monotonic()<deadline, 'Code_Aster cancellation did not publish both stop receipts'
        time.sleep(.15)
    assert (attempt['outputs'].get('publication') or {}).get('status')!='committed'
    assert hashlib.sha256(raw(api,MED)).hexdigest()==second_result['med_sha256']
    record = {'passed':True,'storage':'s3' if store else 'fs','first_job':first,'second_job':second,
              'cancelled_job':cancel_job,'results':[first_result,second_result],'native_child_stop_verified':True,
              'filesystem_and_network_isolation':True,'outside_export_path_rejected':True,'old_med_preserved':True}
    (api.root/'code-aster.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf8')
    return record


if __name__=='__main__':
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=scenario,store_volume_max=8,
        store_factory=partial(ObjectStore,executable=binary,volume_max=8,authenticated=True)))
