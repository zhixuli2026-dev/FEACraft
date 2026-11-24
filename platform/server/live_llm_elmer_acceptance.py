"""Real Elmer jobs through the public API, with numerical and native stop proof."""
import asyncio
from functools import partial
import hashlib
import json
import struct
import time
from urllib.parse import urlencode
import uuid
from xml.etree import ElementTree

from live_llm_job_repeat_acceptance import main, repeat
from s3testsupport import ObjectStore, SERVER


INPUT = 'C-run/elmer/heat.sif'
MESH = 'C-run/elmer/line'
RESULT = MESH+'/heat_t0001.vtu'
DECK = '''Header
  CHECK KEYWORDS Warn
  Mesh DB "." "C-run/elmer/line"
  Results Directory "C-run/elmer/line"
End
Simulation
  Max Output Level = 4
  Coordinate System = Cartesian 1D
  Simulation Type = Steady State
  Steady State Max Iterations = 1
  Output File = "heat.result"
  Post File = "heat.vtu"
End
Body 1
  Target Bodies(1) = 1
  Equation = 1
  Material = 1
End
Equation 1
  Active Solvers(1) = 1
End
Solver 1
  Equation = "Heat Equation"
  Procedure = "HeatSolve" "HeatSolver"
  Variable = Temperature
  Variable DOFs = 1
  Linear System Solver = Direct
  Linear System Direct Method = Banded
End
Material 1
  Heat Conductivity = 1.0
  Density = 1.0
  Heat Capacity = 1.0
End
Boundary Condition 1
  Target Boundaries(1) = 1
  Temperature = 0.0
End
Boundary Condition 2
  Target Boundaries(1) = 2
  Temperature = 100.0
End
'''


def read_solution(api, boundary, result_path):
    data = api.call('GET',api.prefix+'/raw?'+urlencode({'path':result_path}),raw=True)
    points = temperature_points(data)
    error = max(abs(actual-position*boundary) for position,actual in points)
    assert error<1e-12
    return {'sha256':hashlib.sha256(data).hexdigest(),'max_error':error,'temperatures':points}


def temperature_points(data):
    head,appended = data.split(b'<AppendedData',1)
    xml = ElementTree.fromstring(head+b'</VTKFile>')
    payload = appended[appended.index(b'_')+1:]

    def values(array,count):
        offset = int(array.attrib['offset'])
        assert array.attrib['type']=='Float64' and struct.unpack_from('<I',payload,offset)[0]==count*8
        return struct.unpack_from('<'+'d'*count,payload,offset+4)

    assert xml.attrib['byte_order']=='LittleEndian'
    x = values(xml.find('.//Points/DataArray'),9)[::3]
    temperature = values(xml.find('.//PointData/DataArray[@Name="temperature"]'),3)
    assert sorted(x)==[0.0,.5,1.0]
    return sorted(zip(x,temperature))


def completed(api, identifier, *, input_path=INPUT, mesh_path=MESH):
    job = api.wait_job(identifier,timeout=180)
    attempt = job['attempt']
    assert attempt['handler']['key']=='elmer.solve' and attempt['process_stop']['verified']
    assert attempt['outputs']['publication']['status']=='committed'
    children = list(attempt['child_processes']['children'].values())
    assert len(children)==1 and children[0]['purpose']=='elmer_solve' and children[0]['stop']['verified']
    mesh_files = {mesh_path+'/'+part for part in ('mesh.nodes','mesh.elements','mesh.header','mesh.boundary')}
    assert set(attempt['workspace']['files']) >= {input_path,'.vibeflow/prepared/elmer.json',*mesh_files}
    return attempt


def scenario(api, *, store=None, **_):
    deck = DECK if store else DECK.replace('  Results Directory "C-run/elmer/line"\n','')
    result_path = RESULT if store else 'line/heat_t0001.vtu'
    for name,data in {
        'mesh.header':'3 2 2\n2\n101 2\n202 2\n',
        'mesh.nodes':'1 -1 0.0 0.0 0.0\n2 -1 0.5 0.0 0.0\n3 -1 1.0 0.0 0.0\n',
        'mesh.elements':'1 1 202 1 2\n2 1 202 2 3\n',
        'mesh.boundary':'1 1 1 0 101 1\n2 2 2 0 101 3\n',
    }.items():
        assert api.put(MESH+'/'+name,data)['ok']
    route = api.prefix+'/elmer:solve'
    assert api.put(INPUT,deck)['ok']
    bad = 'C-run/elmer/unsafe.sif'
    assert api.put(bad,deck.replace('"HeatSolve"','"C:/foreign/UserProcedure.dll"'))['ok']
    assert api.call('POST',route,{'input_path':bad},expect=400)['detail']
    payload = {'input_path':INPUT}
    headers = {'Idempotency-Key':uuid.uuid4().hex}
    submitted = api.call('POST',route,payload,extra_headers=headers)
    identifier = submitted['job_id']
    first = completed(api,identifier)
    golden = read_solution(api,100.0,result_path)
    assert api.call('POST',route,payload,extra_headers=headers)['job_id']==identifier

    changed = deck.replace('Temperature = 100.0','Temperature = 50.0')
    assert api.put(INPUT,changed)['ok']
    selector = api.call('GET',f'/jobs/{identifier}/operations')
    body = {'action':'rerun','source_attempt_id':selector['source_attempt_id'],'source_version':selector['source_version'],
        'expected_revision':selector['expected_revision'],'expected_event_sequence':selector['expected_event_sequence'],
        'parameters':payload}
    rerun = repeat(api,identifier,body,uuid.uuid4().hex)
    current = completed(api,rerun['id'])
    second = read_solution(api,50.0,result_path)
    assert current['workspace']['files'][INPUT]['sha256']==hashlib.sha256(changed.encode()).hexdigest()
    assert first['workspace']['files'][INPUT]['sha256']!=current['workspace']['files'][INPUT]['sha256']

    transient = changed.replace('Max Output Level = 4','Max Output Level = 1').replace(
        'Simulation Type = Steady State','Simulation Type = Transient\n  Timestepping Method = BDF\n  BDF Order = 1\n'
        '  Timestep Sizes = 0.1\n  Timestep Intervals = 1000000\n  Output Intervals = 1000000')
    assert api.put(INPUT,transient)['ok']
    cancelled_id = api.call('POST',route,payload)['job_id']
    stop = time.monotonic()+35
    while True:
        active = api.call('GET',f'/jobs/{cancelled_id}')
        children = (active['attempt'].get('child_processes') or {}).get('children',{}).values()
        if any(row['phase']=='running' and row['purpose']=='elmer_solve' for row in children):
            break
        assert active['status'] in ('queued','running') and time.monotonic()<stop, active['status']
        time.sleep(.1)
    assert api.call('POST',f'/jobs/{cancelled_id}:cancel')['ok']
    stop = time.monotonic()+90
    while True:
        reply = api.call('GET',f'/jobs/{cancelled_id}')
        cancelled = reply['attempt']
        children = (cancelled.get('child_processes') or {}).get('children',{})
        if (reply['status']=='cancelled' and (cancelled.get('process_stop') or {}).get('verified')
                and len(children)==1 and all((row.get('stop') or {}).get('verified') for row in children.values())):
            break
        assert time.monotonic()<stop, 'Elmer cancellation did not publish both stop proofs'
        time.sleep(.15)
    assert (cancelled['outputs'].get('publication') or {}).get('status')!='committed'
    assert read_solution(api,50.0,result_path)['sha256']==second['sha256']
    result = {'passed':True,'backend':'s3' if store else 'fs','initial_job':identifier,'rerun_job':rerun['id'],
        'cancelled_job':cancelled_id,'golden':golden,'changed_model':second,'parent_and_child_stop_verified':True,
        'unsafe_procedure_rejected':True,'cancel_preserved_published_bytes':True,'result_path':result_path}
    (api.root/'elmer-platform.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__ == '__main__':
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=scenario, store_volume_max=8,
        store_factory=partial(ObjectStore,executable=binary,volume_max=8,authenticated=True)))
