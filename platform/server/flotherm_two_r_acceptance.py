"""Verify native 2R compact devices, branch energy and case-local compilation."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

from flotherm_channel_case_acceptance import load, wsl
from flotherm_cli_acceptance import invoke, snapshot
from flotherm_ecxml_acceptance import authenticated_case_api
from flotherm_paired_acceptance import method, node, numbers, token
from flotherm_plate_pcb_acceptance import _heat, _material


CONFIG = {'boundaries':{'mode':'channel','inletVelocity':.2},
          'openfoam':{'baseCellSize':.018,'endTime':2000,'writeInterval':500},'writeFormat':'binary'}
DOMAIN = {'domain':[0,0,0,.26,.14,.14],'ambient_temperature_k':300}
SOURCE_BUILD = 'constant/flotherm-two-r-src'
LIBRARY = 'constant/flotherm-libs/libvibeflowFlothermTwoR.so'
BOARD = '''<printedCircuitBoard><name>Control board</name><material>Board substrate</material><plane>+xy</plane>
<location><x>.076</x><y>.042</y><z>.038</z></location><size><x>.072</x><y>.059</y><z>.004</z></size>
<powerDissipation>.03</powerDissipation></printedCircuitBoard>'''
ECXML = '''<neutralXML><solutionDomain><location><x>0</x><y>0</y><z>0</z></location>
<size><x>.26</x><y>.14</y><z>.14</z></size><ambientConditions><temperature>300</temperature></ambientConditions></solutionDomain>
<materials><material><name>Board substrate</name><density>1850</density><specific_heat>900</specific_heat>
<thermalConductivity><orthotropic><x>3</x><y>3</y><z>.3</z></orthotropic></thermalConductivity></material></materials>
<geometry>'''+BOARD+'''
<twoResistorModel><name>Compact device</name><plane>+xy</plane><thetaJC>25</thetaJC><thetaJB>45</thetaJB>
<location><x>.096</x><y>.055</y><z>.042</z></location><size><x>.035</x><y>.032</y><z>.024</z></size>
<powerDissipation>.3</powerDissipation></twoResistorModel></geometry></neutralXML>'''


def compact_device(name, origin, dimensions, theta, power):
    axes = [size if i==j else 0 for i,size in enumerate(dimensions) for j in range(3)]
    return node(0x2057000,name,numbers(0x1003009,*dimensions),numbers(0x100300A,*axes),numbers(0x100300B,*origin),
                method(0x2057003,(12,0x3082001)),method(0x205700d,(12,0x3080004)),
                numbers(0x2057014,*theta),numbers(0x205701f,power))


def pdml(include_passive=True, theta=(25,45)):
    board = node(0x2035000,'Control board',numbers(0x1003009,.072,.059,.004),
        numbers(0x100300A,.072,0,0,0,.059,0,0,0,.004),numbers(0x100300B,.076,.042,.038),
        method(0x1001005,(8,'FR4'),(12,0x301000e)),method(0x1001005,(8,'Cu'),(12,0x301000f)),
        method(0x2035004,(12,0x302d002)),numbers(0x2035005,4),method(0x1001008,(8,'boardHeat')))
    active = compact_device('Compact device',(.096,.055,.042),(.035,.032,.024),theta,.3)
    passive = compact_device('Passive device',(.133,.078,.042),(.012,.016,.016),(18,22),0)
    return (b'#FFFB V2310 2r compact thermal fixture\n'+bytes.fromhex('fffb00000906000a')+
            _material('FR4',.3,1850,900)+_material('Cu',390,8960,385)+_heat('boardHeat',.03)+board+active+
            (passive if include_passive else b'')+token(1,0)+b'\n#END\n')


def build(root, label, raw, config, extension):
    source, options = root/f'{label}.{extension}',root/f'{label}.json'
    source.write_bytes(raw)
    options.write_text(json.dumps(config),encoding='utf8')
    ir,case = root/(label+'-ir'),root/(label+'-case')
    result = invoke('import',source,'--config',options,'--output',ir)
    assert result['source']['sha256'] == hashlib.sha256(raw).hexdigest()
    assert invoke('build',ir,'--output',case)['runnable_preflight'] and invoke('check',case)['ok']
    return case


def solve(case, *, parallel=False):
    before = snapshot(case/SOURCE_BUILD)
    solution = 'Allrun.parallel' if parallel else 'Allrun.serial'
    wsl(case,f'ulimit -c 0\nbash {solution} > log.run 2>&1')
    assert load(case/'mesh_gate.json')['ok'] and invoke('check',case)['ok']
    for stage in ('blockMesh','splitMeshRegions','checkMesh'):
        assert 'Could not load' not in (case/f'log.{stage}').read_text(encoding='utf8')
    binary = case/LIBRARY
    assert binary.read_bytes().startswith(b'\x7fELF')
    after = snapshot(case/SOURCE_BUILD)
    assert all(after.get(name)==digest for name,digest in before.items())
    compiled_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    wsl(case,'bash Allrun.build > log.rebuild 2>&1')
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == compiled_hash
    log = (case/'log.run').read_text(encoding='utf8')
    if parallel:
        assert re.search(r'nProcs\s*:\s*2\b',log) and 'Reconstructing fields' in log
    result = invoke('two-r',case)
    flow = invoke('flow',case,'--energy-balance')
    assert result['time'] == flow['time'] and math.isclose(flow['energy_balance']['source_power_w'],.33)
    manifest = load(case/'case-manifest.json')
    native = {row['name']:row['integral_from_field_w'] for row in flow['wall_heat_flux']['patches']}
    heat_to_board = .03
    for network in result['networks']:
        branches = network['branches']
        heat_to_board += branches['board']['native_power_w']
        assert abs(network['native_power_residual_w']) < 2e-5
        case_power = branches['case']['native_power_w']
        assert math.isclose(case_power,native['fluid_to_'+branches['case']['region']],abs_tol=2e-5)
        if network['power_w'] == 0:
            assert case_power > 0 > branches['board']['native_power_w'], network
    assert math.isclose(heat_to_board,native['fluid_to_solid0'],abs_tol=2e-5)
    assert load(case/'power-balance.json')['generated_power_w'] == manifest['heat_sources'][0]['power_w']+.3
    return {'root':str(case),'uid':65534,'library_sha256':compiled_hash,'parallel':parallel,
            'network':result,'flow':flow,'case_manifest':manifest}


def verify_pair(root, config):
    xml, raw = root/'pair.ecxml',root/'pair.pdml'
    xml.write_text(ECXML,encoding='utf8')
    raw.write_bytes(pdml(False))
    invoke('import',xml,'--pdml',raw,'--config',config,'--output',root/'paired-ir')
    pairs = load(root/'paired-ir/flotherm-ir.json')['source_associations']
    assert pairs[1]['verified_by'][-3:] == ['normal','resistance','power_w']
    raw.write_bytes(pdml(False,theta=(25,46)))
    rejected = invoke('import',xml,'--pdml',raw,'--config',config,'--output',root/'bad-pair',status=2,returns_json=False)
    assert '配对' in rejected.stderr


def verify(root):
    xml_case = build(root,'xml',ECXML.encode(),CONFIG,'ecxml')
    xml = solve(xml_case)
    value, = xml['network']['networks']
    assert 312 < value['junction_k'] < 315
    assert 0 < value['branches']['case']['native_power_w'] < value['branches']['board']['native_power_w']
    cap = xml['case_manifest']['solid_regions']['solid1']
    assert math.isclose(cap['bounds_m'][1][2]-cap['bounds_m'][0][2],.024*.45)
    raw_log = xml_case/'log.twoResistance'
    native_log = raw_log.read_bytes()
    try:
        last = native_log.splitlines()[-1]
        raw_log.write_bytes(native_log.replace(last,last.rsplit(b'=',1)[0]+b'=0.45'))
        altered = invoke('two-r',xml_case,status=2)
        assert {'branch_power_imbalance','branch_result_mismatch'}<=set(row['code'] for row in altered['reasons'])
        raw_log.write_bytes(b'\n'.join(native_log.splitlines()[:-1])+b'\n')
        stale = invoke('two-r',xml_case,status=2,returns_json=False)
        assert '最新时刻' in stale.stderr
    finally:
        raw_log.write_bytes(native_log)
    binary = solve(build(root,'pdml',pdml(),{**CONFIG,**DOMAIN},'pdml'),parallel=True)
    assert len(binary['network']['networks']) == 2
    verify_pair(root,root/'xml.json')

    source = root/'no-board.ecxml'
    source.write_text(ECXML.replace(BOARD,''),encoding='utf8')
    invoke('import',source,'--config',root/'xml.json','--output',root/'no-board-ir')
    destination = root/'no-board-case'
    blocked = invoke('build',root/'no-board-ir','--output',destination)
    assert not blocked['runnable_preflight'] and 'PCB' in blocked['blockers'][0]['reason']
    assert 'unresolved blockers' in wsl(destination,'bash Allrun.pre',expected=1).stderr
    assert not (destination/SOURCE_BUILD).exists()
    reject = root/'negative.ecxml'
    reject.write_text(ECXML.replace('thetaJB>45','thetaJB>-45'),encoding='utf8')
    assert '热阻' in invoke('import',reject,'--output',root/'negative-ir',status=2,returns_json=False).stderr
    return {'ecxml_serial':xml,'pdml_parallel':binary,'signed_passive_branch':True,
            'stale_branch_log_rejected':True,'altered_branch_log_rejected':True,
            'pair_resistance_conflict_rejected':True,'missing_board_rejected':True,'negative_resistance_rejected':True}


def verify_s3():
    with authenticated_case_api('flotherm-two-r-s3') as api:
        case = api.create_case('FloTHERM native 2R')
        path = 'Ref/twoR.ecxml'
        api.put(path,ECXML)
        imported = api.call('POST',api.prefix+'/flotherm:import',{'path':path,
            'expected_sha256':hashlib.sha256(ECXML.encode()).hexdigest()})
        api.put('Ref/model.json',imported['content'])
        api.put('Ref/thermal.json',json.dumps(CONFIG))
        output = 'C-run/twoR'
        job = api.call('POST',api.prefix+'/flotherm:convert',{'ir_path':'Ref/model.json',
                       'settings_path':'Ref/thermal.json','output_dir':output})['job_id']
        result = api.wait_job(job,timeout=100)
        assert result['status'] == 'succeeded' and not result['completion_pending'], result
        def read(name):
            return json.loads(api.call('GET',api.prefix+f'/file?path={output}/{name}')['content'])
        manifest, = read('case-manifest.json')['two_resistance_networks']
        assert manifest['power_w'] == .3 and manifest['theta_jc_k_w'] == 25 and manifest['theta_jb_k_w'] == 45
        files = read('.flotherm2foam-generated')['files']
        assert 'Allrun.build' in files and SOURCE_BUILD+'/twoResistanceNetworkSource.C' in files
        return {'case':case['id'],'job':job,'root':str(api.root),'network':manifest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--s3',action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='flotherm-two-r-acceptance-',dir=args.report.resolve().parent))
    evidence = {'passed':False,'root':str(root),'actual_model_calls':0,'simulated_model_calls':0}
    try:
        evidence.update(verify(root))
        if args.s3:
            evidence['authenticated_s3'] = verify_s3()
        evidence['passed'] = True
    finally:
        args.report.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_TWO_R_PASS',args.report)


if __name__ == '__main__':
    main()
