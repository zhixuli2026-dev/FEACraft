"""Import real-format probe and region records, then solve their OpenFOAM outputs."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import tempfile

from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import NEUTRAL_ECXML, authenticated_case_api, reference_temperatures, solve
from flotherm_heat_balance_acceptance import wsl_balance
from flotherm_paired_acceptance import CONFIG, node, numbers, pdml_fixture, upload_binary


PROBE = (.073, .091, .137)
PDML_REGION = (.04, .04, .02, .14, .14, .12)
XML_REGION = (.02, .03, .04, .10, .14, .16)


def model_xml(point=PROBE):
    x, y, z = point
    observations = f'''
       <monitorPoint><name>Core sample</name><active>true</active>
         <location><x>{x}</x><y>{y}</y><z>{z}</z></location>
       </monitorPoint>
       <monitorPoint><name>Disabled sample</name><active>false</active></monitorPoint>
       <region><name>Local ECXML region</name>
         <location><x>.02</x><y>.03</y><z>.04</z></location>
         <size><x>.08</x><y>.11</y><z>.12</z></size>
       </region>
    '''
    return NEUTRAL_ECXML.replace('    </assembly>', observations+'    </assembly>')


def model_pdml(point=PROBE):
    x, y, z = point
    monitor = node(0x2027000, 'Core sample', numbers(0x100300B, y, .2-x, z),
                   numbers(0x1003009, .001, .001, .001),
                   numbers(0x100300A, .001, 0, 0, 0, .001, 0, 0, 0, .001))
    region = node(0x2029000, 'Local rotated PDML region',
                  numbers(0x100300B, .04, .06, .02),
                  numbers(0x100300A, .1, 0, 0, 0, .1, 0, 0, 0, .1),
                  numbers(0x1003009, .1, .1, .1))
    side = .04/math.sqrt(2)
    angled = node(0x2029000, 'Oblique PDML region',
                  numbers(0x100300B, .09, .06, .03),
                  numbers(0x100300A, side, side, 0, -side, side, 0, 0, 0, .08),
                  numbers(0x1003009, .04, .04, .08))
    return pdml_fixture(observations=(monitor, region, angled))


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def near(actual, expected):
    return len(actual) == len(expected) and all(math.isclose(a,b,abs_tol=1e-12) for a,b in zip(actual,expected))


def imported_facts(model, formats):
    assert len(model['objects']) == 1
    monitor, = model['monitors']
    assert monitor['name'] == 'Core sample' and monitor['path'] == 'Test fixture/Core sample'
    assert monitor['objects'] == [model['objects'][0]['id']]
    assert near(monitor['locations'][0], PROBE)
    assert [source['format'] for source in monitor['sources']] == formats
    for source in monitor['sources']:
        if source['format'] == 'PDML':
            assert source['offset'] > 0
        else:
            assert source['element'] == monitor['path'] and source['indices']
    boxes = {row['name']:row for row in model['regions']}
    if 'PDML' in formats:
        assert near(boxes['Local rotated PDML region']['bounds_m'],PDML_REGION)
        angled = boxes['Oblique PDML region']
        side = .04/math.sqrt(2)
        assert near(angled['origin_m'],(.14,.09,.03))
        assert near(angled['bounds_m'],(.14-2*side,.09-side,.03,.14,.09+side,.11))
        assert near(angled['edge_vectors_m'][0],(-side,side,0))
    if 'ECXML' in formats:
        assert near(boxes['Local ECXML region']['bounds_m'],XML_REGION)
        assert near(boxes['solutionDomain']['bounds_m'],[0,0,0,.2,.2,.2])
    return monitor, list(boxes)


def verify(root):
    xml = root/'thermal.ecxml'
    binary = root/'thermal.pdml'
    config = root/'domain.json'
    xml.write_text(model_xml(), encoding='utf8')
    binary.write_bytes(model_pdml())
    config.write_text(json.dumps(CONFIG), encoding='utf8')
    values = {}
    reference = reference_temperatures(tuple(int(value/.02) for value in PROBE))
    for name, args, formats in (
            ('ecxml', [xml], ['ECXML']), ('pdml', [binary,'--config',config], ['PDML']),
            ('paired', [xml,'--pdml',binary], ['ECXML','PDML'])):
        ir, case = root/(name+'-ir'), root/(name+'-case')
        invoke('import', *args, '--output', ir)
        model = read(ir/'flotherm-ir.json')
        monitor, boxes = imported_facts(model, formats)
        assert invoke('build', ir, '--output', case)['runnable_preflight']
        assert invoke('check', case)['ok']
        report = read(case/'conversion-coverage.json')
        assert report['monitor_coverage'][0]['mapping'] == 'converted'
        assert any(row['mapping'] == 'metadata-only' for row in report['regions'])
        assert 'Core sample' in (case/'conversion-coverage.md').read_text(encoding='utf8')
        native = solve(case, model['objects'][0]['id']+'Cells')
        balance = invoke('balance', case)
        reading, = balance['monitors']
        measured, = reading['temperature_k']
        assert balance['ok'] and abs(measured-reference) < .001, (balance, reference)
        assert reading['sources'] == monitor['sources'] and reading['result_path'] in balance['result_sha256']
        if name == 'paired':
            assert wsl_balance(case) == balance
        values[name] = {'native':native, 'source':model['source'], 'regions':boxes, 'probe_k':measured,
                        'reference_probe_k':reference, 'result_path':reading['result_path'],
                        'result_sha256':balance['result_sha256'][reading['result_path']],
                        'outward_power_w':balance['outward_power_w']}

    outside = root/'outside.ecxml'
    outside.write_text(model_xml((.21,.091,.137)),encoding='utf8')
    invoke('import', outside, '--output', root/'outside-ir')
    blocked = invoke('build', root/'outside-ir', '--output', root/'outside-case')
    assert not blocked['runnable_preflight'] and any('monitor_id' in row for row in blocked['blockers'])
    recorded, = read(root/'outside-case/conversion-coverage.json')['monitor_coverage']
    assert recorded['mapping'] == 'unsupported' and '固体' in recorded['reason']

    mismatch = root/'mismatch'
    mismatch.mkdir()
    pair = mismatch/'thermal.pdml'
    pair.write_bytes(model_pdml((.083,.091,.137)))
    rejected = invoke('import',xml,'--pdml',pair,'--output',root/'mismatch-ir',status=2,returns_json=False)
    assert '同名探针坐标' in rejected.stderr and not (root/'mismatch-ir').exists()

    missing = root/'missing.pdml'
    missing.write_bytes(pdml_fixture(observations=(node(0x2027000,'Missing coordinates'),)))
    rejected = invoke('import',missing,'--config',config,'--output',root/'missing-ir',status=2,returns_json=False)
    assert '缺少明确位置' in rejected.stderr
    return {'solved':values, 'provenance_retained':True, 'outside_probe_blocked':True,
            'mismatched_pair_rejected':True, 'missing_pdml_probe_position_rejected':True}


def verify_s3():
    with authenticated_case_api('flotherm-native-monitors-s3') as api:
        case = api.create_case('Raw paired monitor inputs')
        xml, pdml = 'Ref/thermal.ecxml', 'Ref/thermal.pdml'
        xml_data, binary_data = model_xml(), model_pdml()
        api.put(xml, xml_data)
        upload_binary(api, case['id'], pdml, binary_data)
        paths = [xml,pdml]
        hashes = {xml:hashlib.sha256(xml_data.encode()).hexdigest(),
                  pdml:hashlib.sha256(binary_data).hexdigest()}
        inspection = api.call('POST',api.prefix+'/flotherm:inspect',{'paths':paths})
        assert inspection['ok']
        model = api.call('POST',api.prefix+'/flotherm:import',{'paths':paths,'expected_hashes':hashes})
        imported_facts(json.loads(model['content']), ['ECXML','PDML'])
        api.put('Ref/imported.json',model['content'])
        job = api.call('POST',api.prefix+'/flotherm:convert',
                       {'ir_path':'Ref/imported.json','output_dir':'C-run/observations'})
        assert api.wait_job(job['job_id'],timeout=90)['status'] == 'succeeded'
        record = api.call('GET', api.prefix+'/file?path=C-run/observations/conversion-coverage.json')
        report = json.loads(record['content'])
        assert report['monitor_coverage'][0]['mapping'] == 'converted'
        assert len(report['regions']) == 4
        return {'job':job['job_id'],'source_hashes':hashes,'region_count':4,'root':str(api.root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='flotherm-native-observations-',dir=args.report.resolve().parent))
    evidence = {'passed':False,'root':str(root),'actual_model_calls':0,'mock_llm_calls':0}
    try:
        evidence.update(verify(root))
        if args.s3:
            evidence['authenticated_s3'] = verify_s3()
        evidence['passed'] = True
    finally:
        args.report.write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_NATIVE_MONITORS_PASS',args.report,flush=True)


if __name__ == '__main__':
    main()
