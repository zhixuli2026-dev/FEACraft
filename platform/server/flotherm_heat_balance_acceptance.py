"""Run OpenFOAM and verify FloTHERM thermal monitors against its real fields."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from flotherm_cli_acceptance import invoke
from flotherm_ecxml_acceptance import NEUTRAL_ECXML, reference_temperatures, solve


def final_sample(case, function, file):
    datasets = list((case/'postProcessing'/function).glob('*/'+file))
    assert len(datasets) == 1, datasets
    rows = [line.split() for line in datasets[0].read_text(encoding='utf8').splitlines()
            if line.strip() and not line.startswith('#')]
    assert float(rows[-1][0]) == 100
    return float(rows[-1][-1])


def wsl_balance(case):
    def path(value):
        assert value.drive.upper() == 'D:', value
        return '/mnt/d'+value.as_posix()[2:]

    cli = Path(__file__).resolve().with_name('flotherm2foam.py')
    result = subprocess.run(['wsl.exe', '--distribution', 'vibeflow-linux', '--user', 'nobody',
        '--exec', 'env', 'PYTHONDONTWRITEBYTECODE=1', 'python3', path(cli), 'balance', path(case)],
        encoding='utf8', capture_output=True, timeout=25)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def verify(root):
    source = root/'thermal.ecxml'
    source.write_text(NEUTRAL_ECXML, encoding='utf8')
    ir = root/'imported'
    case = root/'converted'
    invoke('import', source, '--output', ir)
    model = ir/'flotherm-ir.json'
    parsed = json.loads(model.read_text(encoding='utf8'))
    target = parsed['objects'][0]['id']
    parsed['monitors'] = [
        {'type': 'region-temperature', 'objects': [target]},
        {'type': 'wall-heat-flux', 'objects': [target]},
        {'type': 'probe', 'objects': [target], 'locations': [[.07, .09, .13]]},
    ]
    model.write_text(json.dumps(parsed), encoding='utf8')
    assert invoke('build', ir, '--output', case)['runnable_preflight']
    assert invoke('check', case)['ok']
    incomplete = invoke('balance', case, status=2, returns_json=False)
    assert '未找到已求解' in incomplete.stderr, incomplete.stderr
    coverage = json.loads((case/'conversion-coverage.json').read_text(encoding='utf8'))
    assert [row['type'] for row in coverage['monitors']] == [row['type'] for row in parsed['monitors']]
    assert coverage['summary']['input_power_w'] == coverage['summary']['mapped_power_w'] == 1200
    manifest = json.loads((case/'case-manifest.json').read_text(encoding='utf8'))
    assert [item['method'] for item in manifest['monitor_results']] == [
        'volFieldValue', 'solved-temperature-wall-gradient', 'probes',
    ]
    native = solve(case)
    average = final_sample(case, 'monitor_0_region_temperature', 'volFieldValue.dat')
    probe = final_sample(case, 'monitor_2_probe', 'T')
    reference = reference_temperatures()
    assert abs(average - sum(reference)/len(reference)) < .001, average
    assert min(reference) < probe < max(reference)+.001, probe
    proof = root/'solved-balance.json'
    result = invoke('balance', case, '--report', proof)
    assert wsl_balance(case) == result
    assert json.loads(proof.read_text(encoding='utf8')) == result
    assert result['source']['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result['time'] == '100' and result['cells'] == 1000 and result['input_power_w'] == 1200, result
    assert abs(result['outward_power_w'] - 1200) < .4, result
    assert all(abs(row['area_m2']-.04)<1e-10 and abs(row['outward_power_w']-200)<.1 for row in result['walls'])
    assert len(result['result_sha256']) == 9
    assert result['monitors'][0]['temperature_k'] == [average]
    assert abs(result['monitors'][0]['recomputed_temperature_k']-average)<.001 and not result['reasons']
    assert result['monitors'][1]['outward_power_w'] == result['outward_power_w']
    assert result['monitors'][2]['temperature_k'] == [probe]
    starting = proof.read_bytes()
    invoke('balance', case, '--report', proof, status=2, returns_json=False)
    assert proof.read_bytes() == starting

    field = case/'100/T'
    original = field.read_text(encoding='utf8')
    try:
        edited, edits = re.subn(r'internalField\s+nonuniform List<scalar>\s*1000\s*\([^)]*\)\s*;',
                                'internalField uniform 310;', original)
        assert edits == 1
        field.write_text(edited, encoding='utf8')
        wrong = invoke('balance', case, status=2)
        assert not wrong['ok'] and wrong['outward_power_w'] == 0 and wrong['residual_w'] == 1200
        assert {row['code'] for row in wrong['reasons']} == {'wall_power_mismatch', 'region_temperature_mismatch'}
    finally:
        field.write_text(original, encoding='utf8')
    (case/'101').mkdir()
    pending = invoke('balance', case, status=2, returns_json=False)
    assert pending.stderr
    (case/'101').rmdir()
    region = case/result['monitors'][0]['result_path']
    series = region.read_bytes()
    try:
        lines = series.splitlines(keepends=True)
        region.write_bytes(b''.join(lines[:-1]))
        missing_monitor = invoke('balance', case, status=2, returns_json=False)
        assert '缺少唯一的最新求解时刻' in missing_monitor.stderr, missing_monitor.stderr
    finally:
        region.write_bytes(series)
    assert invoke('balance', case)['ok']
    return {'openfoam': native, 'balance': result, 'region_average_k': average, 'probe_k': probe,
            'detected_unbalanced_field': True, 'partial_latest_time_rejected': True,
            'missing_monitor_rejected': True, 'existing_report_preserved': True,
            'windows_wsl_report_equal': True, 'root': str(root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    scratch = args.report.resolve().parent
    root = Path(tempfile.mkdtemp(prefix='flotherm-heat-balance-', dir=scratch))
    report = {'passed': False, 'actual_model_calls': 0, 'mock_llm_calls': 0}
    try:
        report.update(verify(root), passed=True)
    finally:
        args.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    print('FLOTHERM_HEAT_BALANCE_PASS', args.report, flush=True)


if __name__ == '__main__':
    main()
