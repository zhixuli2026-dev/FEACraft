"""Compare native OpenFOAM v2406 binary and ASCII thermal results."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import tempfile

from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import authenticated_case_api, solve
from flotherm_heat_balance_acceptance import wsl_balance
from flotherm_monitor_acceptance import model_xml


ARCH = b'LSB;label=32;scalar=64'
POINTS = 'constant/polyMesh/points'
FACES = 'constant/polyMesh/faces'
OWNER = 'constant/polyMesh/owner'
ZONE = 'constant/polyMesh/cellZones'
TEMPERATURE = '100/T'


def raw_offset(data, entries, prefix=b''):
    start = re.search(prefix+rb'\s*'+str(entries).encode()+rb'\s*\(',data[:1400])
    assert start is not None, entries
    return start.end()


@contextmanager
def substitute(path, changed):
    original = path.read_bytes()
    path.write_bytes(changed(original))
    try:
        yield
    finally:
        path.write_bytes(original)


def shifted_point(data):
    start = raw_offset(data,1331)
    values = struct.iter_unpack('<3d', data[start:start+1331*24])
    for i,point in enumerate(values):
        if all(.02 < value < .18 for value in point):
            offset = start+i*24
            return data[:offset]+struct.pack('<d',point[0]+1e-5)+data[offset+8:]
    raise AssertionError('No interior point')


def change_label(data, width, entry, value, prefix=b''):
    offset = raw_offset(data,width,prefix)+entry*4
    return data[:offset]+struct.pack('<i',value)+data[offset+4:]


def corruption_checks(case):
    scenarios = (
        ('invalid_architecture',POINTS,lambda data:data.replace(ARCH,b'LSB;label=17;scalar=64'),'二进制架构'),
        ('ambiguous_architecture',POINTS,lambda data:data.replace(ARCH+b'";',ARCH+b'";\n    arch "invalid";',1),'二进制架构'),
        ('truncated_scalar_data',TEMPERATURE,lambda data:data[:2500],'长度不符或被截断'),
        ('wrong_face_offset',FACES,lambda data:change_label(data,3301,1,5),'紧凑面偏移'),
        ('negative_zone_label',ZONE,lambda data:change_label(data,1000,0,-1,rb'cellLabels\s+List<label>'),'不得为负'),
        ('deformed_mesh',POINTS,shifted_point,'正交六面体'),
    )
    for name, path, changed, error in scenarios:
        with substitute(case/path,changed):
            result = invoke('balance',case,status=2,returns_json=False)
            assert error in result.stderr, (name,result.stderr)
    return [name for name, *_ in scenarios]


def reencode(data, entries, original, alternate, prefix=b''):
    """Re-encode native values to exercise a second width and byte order."""
    start = raw_offset(data,entries,prefix)
    size = struct.calcsize(original)*entries
    original_bytes = data[start:start+size]
    assert data[start+size:start+size+1] == b')'
    packed = b''.join(struct.pack(alternate,*row) for row in struct.iter_unpack(original,original_bytes))
    return (data[:start]+packed+data[start+size:]).replace(ARCH,b'MSB;label=64;scalar=32',1)


def verify(root):
    original = root/'observed.ecxml'
    original.write_text(model_xml(),encoding='utf8')
    options = root/'binary.json'
    options.write_text('{"writeFormat":"binary"}',encoding='utf8')
    ir = root/'imported'
    invoke('import',original,'--output',ir)
    ascii_case = root/'ascii'
    binary_case = root/'binary'
    assert invoke('build',ir,'--output',ascii_case)['runnable_preflight']
    assert invoke('build',ir,'--config',options,'--output',binary_case)['runnable_preflight']
    assert invoke('check',binary_case)['ok']
    assert 'writeFormat binary;' in (binary_case/'system/controlDict').read_text(encoding='utf8')
    assert json.loads((binary_case/'case-manifest.json').read_text(encoding='utf8'))['write_format'] == 'binary'
    ascii_proof = solve(ascii_case)
    binary_proof = solve(binary_case,binary=True)
    ascii_balance = invoke('balance',ascii_case)
    binary_balance = invoke('balance',binary_case)
    assert ascii_balance['ok'] and binary_balance == wsl_balance(binary_case) and binary_balance['ok']
    assert ascii_balance['input_power_w'] == binary_balance['input_power_w'] == 1200
    assert ascii_balance['monitors'][0]['temperature_k'] == binary_balance['monitors'][0]['temperature_k']
    assert abs(binary_balance['residual_w']) < .001
    delta = abs(binary_balance['outward_power_w']-ascii_balance['outward_power_w'])
    assert delta < .1, delta
    for relative in (POINTS,FACES,OWNER,'constant/polyMesh/neighbour','constant/polyMesh/boundary',ZONE,TEMPERATURE):
        data = (binary_case/relative).read_bytes()
        assert (b'format      binary;' in data[:1024] and binary_balance['result_sha256'][relative] ==
                hashlib.sha256(data).hexdigest()), relative
    rejected = corruption_checks(binary_case)
    with substitute(binary_case/POINTS,lambda data:reencode(data,1331,'<3d','>3f')):
        with substitute(binary_case/OWNER,lambda data:reencode(data,3300,'<i','>q')):
            with substitute(binary_case/TEMPERATURE,
                            lambda data:reencode(data,1000,'<d','>f',rb'internalField\s+nonuniform List<scalar>')):
                alternate = invoke('balance',binary_case)
                # Half a float32 ULP at the maximum observed temperature,
                # propagated through the six walls. Allow 1 mW for mesh coordinates.
                temperature_roundoff = math.ldexp(1,math.frexp(binary_proof['maximum_k'])[1]-25)
                precision_bound = temperature_roundoff*160*.24/.01+.001
                precision_difference = abs(alternate['outward_power_w']-binary_balance['outward_power_w'])
                assert alternate['ok'] and precision_difference < precision_bound, alternate
    assert invoke('balance',binary_case) == binary_balance
    invalid = root/'invalid.json'
    invalid.write_text('{"writeFormat":"other"}',encoding='utf8')
    rejected_setting = invoke('build',ir,'--config',invalid,'--output',root/'invalid-case',status=2,returns_json=False)
    assert '必须为 ascii 或 binary' in rejected_setting.stderr
    return {'native_ascii':ascii_proof,'native_binary':binary_proof,'ascii_balance':ascii_balance,
            'binary_balance':binary_balance,'outward_difference_w':delta,
            'binary_payload_faults_rejected':rejected,'altered_architecture_protocol_verified':True,
            'alternate_precision_difference_w':precision_difference,'alternate_precision_bound_w':precision_bound,
            'invalid_write_format_rejected':True,'windows_wsl_equal':True}


def verify_s3():
    with authenticated_case_api('flotherm-binary-format-s3') as api:
        api.create_case('Publish OpenFOAM binary format')
        data = model_xml()
        original = 'Ref/observed.ecxml'
        api.put(original,data)
        derived = api.call('POST',api.prefix+'/flotherm:import',
                           {'paths':[original],'expected_hashes':{original:hashlib.sha256(data.encode()).hexdigest()}})
        api.put('Ref/observed.json',derived['content'])
        api.put('Ref/binary.json','{"writeFormat":"binary"}')
        target = 'C-run/binary'
        accepted = api.call('POST',api.prefix+'/flotherm:convert',
                            {'ir_path':'Ref/observed.json','settings_path':'Ref/binary.json','output_dir':target})
        result = api.wait_job(accepted['job_id'],timeout=90)
        assert result['status'] == 'succeeded'
        manifest = json.loads(api.call('GET',api.prefix+f'/file?path={target}/case-manifest.json')['content'])
        control = api.call('GET',api.prefix+f'/file?path={target}/system/controlDict')['content']
        assert manifest['write_format'] == 'binary' and 'writeFormat binary;' in control
        return {'job':accepted['job_id'],'format':manifest['write_format'],'root':str(api.root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='flotherm-binary-balance-',dir=args.report.resolve().parent))
    evidence = {'root':str(root),'passed':False,'actual_model_calls':0,'mock_llm_calls':0}
    try:
        evidence.update(verify(root))
        if args.s3:
            evidence['authenticated_s3'] = verify_s3()
        evidence['passed'] = True
    finally:
        args.report.write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_BINARY_BALANCE_PASS',args.report,flush=True)


if __name__ == '__main__':
    main()
