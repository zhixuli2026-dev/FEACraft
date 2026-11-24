"""Binary PDML and paired XTXML/ECXML checks through the actual FloTHERM CLI."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import urllib.parse
import urllib.request
import uuid

from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import NEUTRAL_ECXML, authenticated_case_api, solve


CONFIG = {'domain': {'location': [0, 0, 0], 'size': [.2, .2, .2], 'ambientTemperature': 310}}
XTXML = '''<xtxml_assembly xmlns="urn:independent-assembly-fixture">
  <documents>
    <assembly name="FixtureDocument"><child_components>
      <component name="Heated solid" document="SolidDocument"><position x="0" y="0" z="0"/></component>
    </child_components></assembly>
    <part name="SolidDocument"/>
  </documents>
  <root_assembly><assembly name="Test root"><child_components>
    <component name="Fixture" document="FixtureDocument">
      <position x="0.2" y="0" z="0"/>
      <rotation r00="0" r01="-1" r02="0" r10="1" r11="0" r12="0" r20="0" r21="0" r22="1"/>
    </component>
  </child_components></assembly></root_assembly>
</xtxml_assembly>
'''


def token(tag, value):
    if tag == 8:
        raw = value.encode('utf-8')
        return struct.pack('>BI', tag, len(raw))+raw
    return struct.pack('>Bd' if tag == 6 else '>BI', tag, value)


def method(identifier, *fields):
    return token(10, identifier)+b''.join(token(*field) for field in fields)+token(1, 3)


def node(identifier, name, *contents):
    return token(7, identifier)+token(8, name)+token(3, 0)+token(1, 1)+b''.join(contents)+token(1, 2)


def numbers(identifier, *items):
    return method(identifier, *((6, item) for item in items))


def pdml_fixture(*, power=1200, solid_name='Heated solid', material_reference='source-material',
                 assembly_position=.2, observations=()):
    material = node(0x201B000, 'Homogeneous solid', method(0x1001002, (8, 'source-material')),
                    numbers(0x201B004, 160), numbers(0x201B001, 2000), numbers(0x201B002, 500))
    heat = node(0x2022000, 'Defined total power', method(0x1001002, (8, 'source-heat')),
                method(0x2022004, (6, power), (12, 0x3044001)))
    part = node(0x2025000, solid_name, numbers(0x100300A, .2, 0, 0, 0, .2, 0, 0, 0, .2),
                numbers(0x1003009, .2, .2, .2), numbers(0x100300B, 0, 0, 0),
                method(0x1001005, (8, material_reference)), method(0x1001008, (8, 'source-heat')))
    parent = node(0x202E000, 'Test fixture', numbers(0x100300A, 0, 1, 0, -1, 0, 0, 0, 0, 1),
                  numbers(0x1003009, 1, 1, 1), numbers(0x100300B, assembly_position, 0, 0), part, *observations)
    return b'#FFFB V2310 independent-record-fixture\n'+bytes.fromhex('fffb00000906000a')+material+heat+parent+token(1, 0)+b'\n#END\n'


def upload_binary(api, case_id, path, payload):
    marker = uuid.uuid4().hex
    field = (f'--{marker}\r\nContent-Disposition: form-data; name="file"; filename="raw.pdml"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n').encode('ascii')
    data = field+payload+f'\r\n--{marker}--\r\n'.encode('ascii')
    address = f'{api.base}/api/v1/cases/{case_id}/upload?'+urllib.parse.urlencode({'path': path})
    request = urllib.request.Request(address, data=data, headers={'Authorization': 'Bearer '+api.token,
        'Content-Type': 'multipart/form-data; boundary='+marker}, method='POST')
    with api.opener.open(request, timeout=30) as response:
        assert response.status == 200


def _fixture(root):
    paths = {key: root/f'thermal.{key}' for key in ('ecxml', 'pdml', 'xtxml', 'xtxmla')}
    paths['ecxml'].write_bytes(NEUTRAL_ECXML.encode())
    paths['pdml'].write_bytes(pdml_fixture())
    for kind in ('xtxml', 'xtxmla'):
        paths[kind].write_bytes(XTXML.encode())
    config = root/'domain.json'
    config.write_text(json.dumps(CONFIG), encoding='utf8')
    return paths, config


def verify(root, with_solver):
    paths, config = _fixture(root)
    combos = (('ecxml',), ('pdml',), ('ecxml', 'pdml'), ('xtxml', 'pdml'), ('xtxmla', 'pdml'),
              ('xtxml', 'pdml', 'ecxml'), ('xtxmla', 'pdml', 'ecxml'))
    results = []
    for parts in combos:
        name = '+'.join(parts)
        arguments = [paths[parts[0]]]
        for part in parts[1:]:
            arguments.extend((f'--{part}', paths[part]))
        if 'ecxml' not in parts:
            arguments.extend(('--config', config))
        target = root/name
        result = invoke('import', *arguments, '--output', target)
        raw = json.loads((target/'flotherm-ir.json').read_text(encoding='utf8'))
        assert result['coverage']['power_w'] == invoke('validate', target)['coverage']['power_w'] == 1200
        assert set(row['sha256'] for row in raw['source']['inputs']) == {
            hashlib.sha256(paths[part].read_bytes()).hexdigest() for part in parts}
        assert raw['objects'][0]['shape']['origin'] == [0, 0, 0]
        assert raw['objects'][0]['shape']['dimensions'] == [.2, .2, .2]
        if 'pdml' in parts and 'ecxml' in parts:
            assert len(raw['source_associations']) == 1
            assert raw['source_associations'][0]['verified_by'] == ['name', 'bounds_m']
        if parts[0] in ('xtxml', 'xtxmla'):
            graph = raw['assembly']['xtxml']
            assert graph['geometry_source'] == 'PDML'
            leaf = graph['instances'][1]
            assert leaf['translation'] == [.2, 0, 0] and leaf['name_match'] == raw['objects'][0]['id']
        output = root/(name+'-case')
        assert invoke('build', target, '--output', output)['runnable_preflight']
        assert invoke('check', output)['ok']
        item = {'input': name, 'objects': raw['coverage']['objects'], 'power_w': 1200}
        if with_solver and parts == ('pdml',):
            item['openfoam'] = solve(output, raw['objects'][0]['id']+'Cells')
        results.append(item)

    if not invoke('inspect', paths['xtxml'], '--pdml', paths['pdml'])['ok']:
        raise AssertionError('XTXML and binary fixture failed preflight')
    invalid = root/'invalid'
    invalid.mkdir()
    binary = invalid/'thermal.pdml'
    expected = [
        (pdml_fixture()[:-13] + b'\n#END\n', (), 'PDML'),
        (pdml_fixture(material_reference='unmatched'), (), '引用'),
        (pdml_fixture(solid_name='Different'), ('--ecxml', paths['ecxml']), '配对'),
        (pdml_fixture(assembly_position=.21), ('--ecxml', paths['ecxml']), '配对'),
    ]
    for index, (payload, extra, reason) in enumerate(expected):
        binary.write_bytes(payload)
        destination = root/f'rejected{index}'
        outcome = invoke('import', binary, *extra, '--config', config, '--output', destination, status=2, returns_json=False)
        assert reason in outcome.stderr and not destination.exists(), outcome.stderr
    missing = invoke('import', paths['pdml'], '--output', root/'without-config', status=2, returns_json=False)
    assert '配置 JSON' in missing.stderr
    bad_xt = invalid/'thermal.xtxml'
    bad_xt.write_text(XTXML.replace('r01="-1"', 'r01="-4"'), encoding='utf8')
    corrupt = invoke('import', bad_xt, '--pdml', paths['pdml'], '--config', config,
                     '--output', root/'bad-transform', status=2, returns_json=False)
    assert '正交' in corrupt.stderr
    return {'passed': True, 'combinations': results, 'rejected': ['truncated_binary', 'bad_reference', 'ambiguous_name',
            'bbox_conflict', 'missing_domain', 'invalid_rigid_transform'], 'model_requests': 0}


def verify_s3():
    with authenticated_case_api('flotherm-pair-s3') as api:
        case = api.create_case('FloTHERM paired raw fixtures')
        binary, assembly = 'Ref/thermal.pdml', 'Ref/thermal.xtxmla'
        settings = 'Ref/domain.json'
        api.put(assembly, XTXML)
        api.put(settings, json.dumps(CONFIG))
        upload_binary(api, case['id'], binary, pdml_fixture())
        sources = [assembly, binary]
        inspection = api.call('POST', api.prefix+'/flotherm:inspect', {'paths': sources})
        assert inspection['ok'] and inspection['kind'] == 'XTXMLA+PDML'
        hashes = {name:item['sha256'] for name, item in zip(sources, inspection['files'])}
        request = {'paths': sources, 'expected_hashes': hashes, 'settings_path': settings}
        api.put(assembly, XTXML+'\n')
        rejected = api.call('POST', api.prefix+'/flotherm:import', request, expect=409)
        assert rejected['detail']['code'] == 'input_stale'
        hashes[assembly] = hashlib.sha256((XTXML+'\n').encode()).hexdigest()
        derived = api.call('POST', api.prefix+'/flotherm:import', request)
        model = json.loads(derived['content'])
        assert model['source']['configuration']['path'] == settings
        assert model['assembly']['xtxml']['instances'][1]['name_match'] == model['objects'][0]['id']
        ir = 'Ref/paired-ir.json'
        api.call('PUT', api.prefix+'/file', {'path': ir, 'content': derived['content'], 'expected_sha256': None})
        staged = api.call('POST', api.prefix+'/flotherm:convert', {'ir_path': ir, 'output_dir': 'C-run/paired'})
        result = api.wait_job(staged['job_id'], timeout=110)
        assert result['status'] == 'succeeded' and not result['completion_pending']
        balance = api.call('GET', api.prefix+'/file?path=C-run/paired/power-balance.json')
        digest = json.loads(balance['content'])
        assert digest['input_power_w'] == 1200 and digest['balanced']
        return {'job': result['id'], 'input_kind': inspection['kind'], 'power_w': digest['input_power_w'],
                'stale_pair_rejected': True, 'evidence': str(api.root)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--solver', action='store_true')
    parser.add_argument('--s3', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='flotherm-paired-check-') as folder:
        report = verify(Path(folder), args.solver)
    if args.s3:
        report['authenticated_s3'] = verify_s3()
    result = json.dumps(report, ensure_ascii=False, indent=2)+'\n'
    args.report.write_text(result, encoding='utf8')
    print(result)
