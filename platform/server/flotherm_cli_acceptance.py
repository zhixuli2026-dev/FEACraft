"""Verify actual offline FloTHERM CLI invocations and generated OpenFOAM files."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from app.services.flotherm_ir import build_ir


CLI = Path(__file__).with_name('flotherm2foam.py')


def invoke(*args, status=0, returns_json=True):
    result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(CLI), *map(str, args)],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == status, (args, result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout) if returns_json else result


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def verify(root):
    raw = '<ecxml><box bbox="0 0 0 1 1 1" /></ecxml>\n'
    xml, pdml = root/'chip.ecxml', root/'chip.pdml'
    xml.write_text(raw, encoding='utf-8')
    pdml.write_text('#FFFB V2310\n', encoding='utf-8')
    assert invoke('inspect', xml, '--pdml', pdml)['kind'] == invoke('--inspect', xml, pdml)['kind'] == 'ECXML+PDML'
    model = build_ir('chip.ecxml', xml.read_bytes(), [{'id': 'chip', 'type': 'box', 'material': 'silicon',
        'mapping': 'converted', 'power_w': 5, 'shape': {'kind': 'box', 'origin': [0, 0, 0],
        'dimensions': [1, 1, 1]}}], {'silicon': {'conductivity_w_mk': 130, 'rho_cp': 1000}})
    ir = root/'model-ir'
    ir.mkdir()
    source = ir/'flotherm-ir.json'
    source.write_text(json.dumps(model), encoding='utf-8')
    assert invoke('validate', ir)['coverage']['power_w'] == invoke('inspect', source)['coverage']['power_w'] == 5
    settings = root/'overrides.json'
    settings.write_text('{"power":{"chip":{"value":7500,"unit":"mW"}}}', encoding='utf-8')
    output = root/'case output'
    assert invoke('build', ir, '--config', settings, '--output', output)['runnable_preflight']
    assert invoke('check', output)['ok']
    constructed = snapshot(output)
    readback = json.loads((output/'flotherm-ir.json').read_text(encoding='utf-8'))
    assert readback['settings']['source']['path'] == settings.name
    assert readback['coverage']['power_w'] == 7.5
    assert constructed.get('system/blockMeshDict') and constructed.get('constant/fvOptions')

    # Malformed requests and foreign files cannot erase a prior result.
    bad = root/'bad.json'
    bad.write_text('{"schema":"invalid"}', encoding='utf-8')
    assert invoke('validate', bad, status=2, returns_json=False).stderr
    invoke('build', bad, '--output', output, '--force', status=2, returns_json=False)
    assert snapshot(output) == constructed
    invoke('build', source, '--output', output, status=2, returns_json=False)
    assert snapshot(output) == constructed
    user = output/'notes.txt'
    user.write_text('preserve', encoding='utf-8')
    invoke('build', source, '--output', output, '--force', status=2, returns_json=False)
    observed = snapshot(output)
    assert set(observed) == {*constructed, 'notes.txt'}
    assert all(observed[path] == digest for path, digest in constructed.items())
    assert user.read_text(encoding='utf-8') == 'preserve'
    user.unlink()

    file = output/'constant/fvOptions'
    file.write_text('modified dictionary', encoding='utf-8')
    assert invoke('check', output, status=2)['changed'] == ['constant/fvOptions']
    file.unlink()
    missing = invoke('check', output, status=2)
    assert missing['missing'] == ['constant/fvOptions']
    # Restore the complete registered set before testing its replacement.
    previous = root/'previous-case'
    invoke('build', source, '--config', settings, '--output', previous)
    file.write_bytes((previous/'constant/fvOptions').read_bytes())
    invoke('build', source, '--output', output, '--force')
    assert invoke('check', output)['ok']
    assert json.loads((output/'power-balance.json').read_text(encoding='utf-8'))['input_power_w'] == 5
    assert json.loads(source.read_text(encoding='utf-8')) == model
    return {'native': sys.platform, 'inspection': 'ECXML+PDML', 'initial_power_w': 7.5, 'rebuild_power_w': 5,
            'cli_commands': ['inspect', '--inspect', 'validate', 'build', 'check'],
            'rejected': ['invalid_ir', 'existing_output', 'unregistered_file', 'changed_file', 'missing_file'],
            'model_requests': 0}


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='flotherm-cli-check-') as temp:
        report = verify(Path(temp))
    if len(sys.argv) == 2:
        Path(sys.argv[1]).write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))
