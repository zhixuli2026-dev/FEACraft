"""Run native volume and mass-flow cases and cross-check wallHeatFlux integration."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

from flotherm_binary_balance_acceptance import substitute
from flotherm_cli_acceptance import invoke


_RUN = r'''
source /usr/lib/openfoam/openfoam2406/etc/bashrc || exit
set -e
cd -- "$1"
mode=$2
id -u
if [ "$mode" = mass ]; then
    cp -r "$FOAM_TUTORIALS/heatTransfer/chtMultiRegionSimpleFoam/heatExchanger/." .
    foamDictionary system/controlDict -entry endTime -set 20 > log.configuration
    foamDictionary system/controlDict -entry writeInterval -set 5 >> log.configuration
    foamDictionary system/controlDict -entry writeFormat -set binary >> log.configuration
    python3 - <<'PY'
from pathlib import Path
for name, old, new in [('air', '(30 30 30)', '(15 15 15)'), ('porous', '(40 30 30)', '(20 15 15)')]:
    path = Path('system') / name / 'blockMeshDict'
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old,new))
PY
    blockMesh -region air > log.airMesh 2>&1
    blockMesh -region porous > log.porousMesh 2>&1
    topoSet -region air -dict system/topoSetDict.1 > log.topoSet.1 2>&1
    createBaffles -region air -overwrite > log.baffles 2>&1
    topoSet -region air -dict system/topoSetDict.2 > log.topoSet.2 2>&1
    cp -r 0.orig 0
    foamDictionary 0/air/T -entry boundaryField/walls -set '{ type fixedValue; value uniform 330; }' >> log.configuration
    region='region air;'
    wall='nativeHeat { type wallHeatFlux; libs (fieldFunctionObjects); region air; patches (walls blades); useNamePrefix false; executeControl writeTime; writeControl writeTime; }'
    application=chtMultiRegionSimpleFoam
else
    cp -r "$FOAM_TUTORIALS/incompressible/simpleFoam/pitzDaily/." .
    foamDictionary system/controlDict -entry endTime -set 100 > log.configuration
    foamDictionary system/controlDict -entry writeInterval -set 100 >> log.configuration
    foamDictionary system/controlDict -entry writeFormat -set ascii >> log.configuration
    blockMesh > log.mesh 2>&1
    region=
    wall=
    application=simpleFoam
fi
foamDictionary system/controlDict -entry writePrecision -set 12 >> log.configuration
cat >> system/controlDict <<NATIVE
functions
{
    $wall
    inFlux { type surfaceFieldValue; libs (fieldFunctionObjects); $region regionType patch; name inlet; operation sum; fields (phi); writeFields false; writeControl writeTime; }
    outFlux { type surfaceFieldValue; libs (fieldFunctionObjects); $region regionType patch; name outlet; operation sum; fields (phi); writeFields false; writeControl writeTime; }
}
NATIVE
$application > log.solver 2>&1
'''


def wsl_path(path):
    assert path.drive.upper() == 'D:', path
    return '/mnt/d'+path.as_posix()[2:]


def native(case, mode):
    case.mkdir()
    result = subprocess.run(['wsl.exe','--distribution','vibeflow-linux','--user','nobody',
        '--exec','bash','-c',_RUN,'flotherm-channel',wsl_path(case),mode],
        capture_output=True,encoding='utf8',errors='replace',timeout=125)
    assert result.returncode == 0, (result.stdout,result.stderr,str(case))
    assert result.stdout.strip() == '65534' and (case/'log.solver').read_text(encoding='utf8').rstrip().endswith('End')


def native_sum(case, function, time, region=None):
    root = case/'postProcessing'
    if region is not None:
        root /= region
    data = (root/function/'0/surfaceFieldValue.dat').read_text(encoding='utf8')
    selected = [line.split() for line in data.splitlines() if line.strip() and not line.startswith('#')
                and float(line.split()[0]) == float(time)]
    assert len(selected) == 1 and len(selected[0]) == 2, selected
    return float(selected[0][1])


def wsl_report(case):
    cli = Path(__file__).resolve().with_name('flotherm2foam.py')
    result = subprocess.run(['wsl.exe','--distribution','vibeflow-linux','--user','nobody',
        '--exec','env','PYTHONDONTWRITEBYTECODE=1','python3',wsl_path(cli),'flow',wsl_path(case),
        '--region','air','--wall-heat-flux','nativeHeat'],capture_output=True,encoding='utf8',timeout=25)
    assert result.returncode == 0, (result.stdout,result.stderr)
    return json.loads(result.stdout)


def no_outlet_flux(data):
    prefix = rb'\boutlet\s*\{\s*type\s+calculated\s*;\s*value\s+nonuniform\s+List<scalar>\s+(\d+)\s*\('
    found = re.search(prefix,data)
    assert found is not None
    start, count = found.end(), int(found[1])
    end = start+count*8
    assert data[end:end+2] == b');'
    return data[:start]+bytes(count*8)+data[end:]


def wrong_wall_power(data):
    lines = data.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.split()[:2] == [b'20',b'walls']:
            lines[index] = b'\t'.join([*line.split()[:4], b'0'])+b'\n'
            return b''.join(lines)
    raise AssertionError('Native wall sample not found')


def checks(mass, wall_report):
    reversed_ports = invoke('flow',mass,'--region','air','--inlet','outlet','--outlet','inlet',status=2)
    assert {row['code'] for row in reversed_ports['reasons']} == {'unexpected_flow_direction'}
    result_path = mass/'20/air/phi'
    with substitute(result_path,no_outlet_flux):
        imbalanced = invoke('flow',mass,'--region','air',status=2)
        assert 'flow_conservation_mismatch' in {row['code'] for row in imbalanced['reasons']}
    leak = rb'(walls\s*\{\s*type\s+calculated\s*;\s*value\s+uniform\s+)0;'
    with substitute(result_path,lambda data:re.sub(leak,rb'\g<1>0.000001;',data,count=1)):
        leaked = invoke('flow',mass,'--region','air',status=2)
        assert 'additional_boundary_flow' in {row['code'] for row in leaked['reasons']}
    with substitute(result_path,lambda data:data.replace(b'[1 0 -1 0 0 0 0]',b'[0 1 -1 0 0 0 0]',1)):
        wrong_dimension = invoke('flow',mass,'--region','air',status=2,returns_json=False)
        assert 'phi 量纲' in wrong_dimension.stderr
    with substitute(mass/wall_report,wrong_wall_power):
        refused = invoke('flow',mass,'--region','air','--wall-heat-flux','nativeHeat',status=2)
        assert {row['code'] for row in refused['reasons']} == {'wall_heat_flux_mismatch'}
    pending = mass/'21'
    pending.mkdir()
    try:
        incomplete = invoke('flow',mass,'--region','air',status=2,returns_json=False)
        assert '21' in incomplete.stderr
    finally:
        pending.rmdir()
    return ['swapped_ports','outlet_flux_removed','side_wall_flow','invalid_dimensions',
            'wall_report_mismatch','latest_output_incomplete']


def verify(root):
    mass, volume = root/'mass', root/'volume'
    native(volume,'volume')
    native(mass,'mass')
    emitted = root/'flow-report.json'
    first = invoke('flow',mass,'--region','air','--wall-heat-flux','nativeHeat','--report',emitted)
    second = invoke('flow',volume)
    assert first == json.loads(emitted.read_text(encoding='utf8')) == wsl_report(mass)
    assert first['unit'] == 'kg/s' and first['time'] == '20'
    assert second['unit'] == 'm3/s' and second['time'] == '100'
    assert abs(second['inlet']['inward']-.000254) < 1e-12
    for case, report, region in ((mass,first,'air'), (volume,second,None)):
        assert report['ok'] and report['inlet']['inward'] > 0 and report['outlet']['outward'] > 0
        assert report['other_boundary_throughput'] == 0 and report['net_outward'] != 0
        for label,function in (('inlet','inFlux'), ('outlet','outFlux')):
            expected = native_sum(case,function,report['time'],region)
            assert math.isclose(report[label]['net_outward'],expected,rel_tol=1e-11,abs_tol=1e-13)
        for relative, digest in report['result_sha256'].items():
            assert hashlib.sha256((case/relative).read_bytes()).hexdigest() == digest
    wall = next(row for row in first['wall_heat_flux']['patches'] if row['name'] == 'walls')
    assert wall['native_integral_w'] > 700
    assert abs(wall['native_integral_w']-wall['integral_from_field_w']) < 1e-6
    failures = checks(mass,first['wall_heat_flux']['native_report'])
    assert invoke('flow',mass,'--region','air','--wall-heat-flux','nativeHeat') == first
    exists = invoke('flow',mass,'--region','air','--report',emitted,status=2,returns_json=False)
    assert exists.stderr and json.loads(emitted.read_text(encoding='utf8')) == first
    return {'mass':first,'volume':second,'native_flow_sums_verified':True,'native_openfoam_uid':65534,
            'failures_detected':failures,'windows_wsl_equal':True,'existing_report_preserved':True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='flotherm-channel-acceptance-',dir=args.report.resolve().parent))
    evidence = {'root':str(root),'passed':False,'actual_model_calls':0,'mock_llm_calls':0}
    try:
        evidence.update(verify(root),passed=True)
    finally:
        args.report.write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_CHANNEL_PASS',args.report,flush=True)


if __name__ == '__main__':
    main()
