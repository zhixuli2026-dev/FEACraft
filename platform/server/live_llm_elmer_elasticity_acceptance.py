"""Check two real, published Elmer elasticity loads against the axial solution."""
import asyncio
from functools import partial
import hashlib
import json
import struct
import uuid
from xml.etree import ElementTree

from live_llm_elmer_acceptance import completed
from live_llm_job_repeat_acceptance import main
from s3testsupport import ObjectStore, SERVER


def verify_field(data, strain):
    header,raw = data.split(b'<AppendedData',1)
    metadata = ElementTree.fromstring(header+b'</VTKFile>')
    payload = raw[raw.index(b'_')+1:]
    assert metadata.attrib['byte_order']=='LittleEndian'

    def values(array, count):
        offset = int(array.attrib['offset'])
        assert array.attrib['type']=='Float64' and struct.unpack_from('<I',payload,offset)[0]==count*8
        return struct.unpack_from('<'+'d'*count,payload,offset+4)

    points = values(metadata.find('.//Points/DataArray'),36)
    displacement = values(metadata.find('.//PointData/DataArray[@Name="displacement"]'),36)
    axial_stress = values(metadata.find('.//PointData/DataArray[@Name="stress_xx"]'),12)
    undeformed_x = [point-offset for point,offset in zip(points[::3],displacement[::3])]
    assert sorted({round(x,12) for x in undeformed_x})==[0.,.5,1.]
    transverse = max(abs(value) for i,value in enumerate(displacement) if i%3)
    displacement_error = max(transverse,max(abs(value-x*strain) for x,value in zip(undeformed_x,displacement[::3])))
    expected_stress = 210e9*strain
    axial_error = max(abs(value-expected_stress) for value in axial_stress)
    assert displacement_error < 1e-12 and axial_error < 1e-5
    return {'vtu_sha256':hashlib.sha256(data).hexdigest(), 'expected_stress_pa':expected_stress,
            'max_stress_error_pa':axial_error,'max_displacement_error_m':displacement_error,
            'midspan_displacement_m':[u for x,u in zip(undeformed_x,displacement[::3]) if abs(x-.5)<1e-12]}


def scenario(api, *, store=None, **_):
    example = SERVER/'examples/elmer/axial'
    for part in ('mesh.header','mesh.nodes','mesh.elements','mesh.boundary'):
        name = 'brick/'+part
        assert api.put(name,(example/name).read_text('ascii'))['ok']
    original = (example/'axial.sif').read_text('ascii')
    assert original.count('Displacement 1 = 0.001')==1
    cases = []
    for displacement in (.001,.002):
        deck = original.replace('Displacement 1 = 0.001',f'Displacement 1 = {displacement:g}')
        assert api.put('axial.sif',deck)['ok']
        route = api.prefix+'/elmer:solve'
        body = {'input_path':'axial.sif'}
        headers = {'Idempotency-Key':uuid.uuid4().hex}
        job = api.call('POST',route,body,extra_headers=headers)['job_id']
        attempt = completed(api,job,input_path='axial.sif',mesh_path='brick')
        assert attempt['workspace']['files']['axial.sif']['sha256']==hashlib.sha256(deck.encode()).hexdigest()
        assert api.call('POST',route,body,extra_headers=headers)['job_id']==job
        assert api.call('GET',api.prefix+'/raw?path=brick/axial.result',raw=True).lstrip().startswith(b'ASCII 3')
        data = api.call('GET',api.prefix+'/raw?path=brick/axial_t0001.vtu',raw=True)
        cases.append({'job':job,**verify_field(data,displacement)})
    assert cases[0]['job']!=cases[1]['job'] and cases[0]['vtu_sha256']!=cases[1]['vtu_sha256']
    record = {'passed':True,'storage':'s3' if store else 'fs','linear_elasticity':cases,
              'native_child_stops_verified':True,'same_key_replay_preserved':True}
    (api.root/'elmer-elasticity.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf8')
    return record


if __name__ == '__main__':
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=scenario,store_volume_max=8,
        store_factory=partial(ObjectStore,executable=binary,volume_max=8,authenticated=True)))
