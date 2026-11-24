"""Verify generated-case cleanup and rerun in native OpenFOAM v2406."""

import argparse
import json
from pathlib import Path
import tempfile

from flotherm_channel_case_acceptance import load, wsl
from flotherm_cli_acceptance import invoke, snapshot
from flotherm_ecxml_acceptance import NEUTRAL_ECXML
from flotherm_merge_restart_acceptance import CONFIG, run_until, source_xml
from flotherm_refinement_acceptance import build, stage


def verify(root, *, conjugate):
    label = 'cht' if conjugate else 'conduction'
    raw = source_xml() if conjugate else NEUTRAL_ECXML.encode()
    options = {**CONFIG,'openfoam':{**CONFIG['openfoam'],'parallelSubdomains':2}} if conjugate else {}
    ir, _, source = stage(root,label,raw,options,'ecxml')
    case, result = build(root,label,ir)
    assert result['runnable_preflight']
    assert load(case/'case-manifest.json')['parallel_subdomains']==2
    end = 200 if conjugate else 100
    log = run_until(case,'parallel',end,'Allrun.parallel')
    assert 'nProcs : 2' in log and (case/'processor1'/str(end)).is_dir()
    assert (case/'constant/polyMesh').exists() or (case/'constant/fluid/polyMesh').exists()

    authored = {'constant/reference.txt':'authored constant content',
                '73/T':'authored temperature notes',
                'processor9/README':'authored run notes'}
    for name, content in authored.items():
        path = case/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(content,encoding='utf8')

    before = {name:snapshot(case/name) for name in ('0','system')}
    inputs = {name:(case/name).read_bytes() for name in
              ('flotherm-ir.json','case-manifest.json','.flotherm2foam-generated','preflight.json')}
    untouched = root/(label+'-outside.txt')
    untouched.write_text('outside generated case',encoding='utf8')
    wsl(case,f'ln -s ../{untouched.name} log.outside\ncd ..\nbash {case.name}/Allclean')
    assert untouched.read_text(encoding='utf8')=='outside generated case'
    assert (case/'log.outside').is_symlink()
    assert all((case/name).read_text(encoding='utf8')==content for name,content in authored.items())
    assert {name:snapshot(case/name) for name in before}==before
    assert all((case/name).read_bytes()==data for name,data in inputs.items()) and source.read_bytes()==raw
    assert not any((case/name).exists() for name in ('processor0','processor1',str(end),
                                                    'postProcessing','log.parallel','constant/polyMesh',
                                                    'constant/fluid/polyMesh'))
    assert not load(case/'mesh_gate.json')['ok']
    wsl(case,'bash Allclean')

    # Remove the deliberately ambiguous test fixture names before cold start.
    for name in ('73','processor9'):
        (case/name).rename(root/(label+'-'+name))
    wsl(case,'unlink log.outside')
    assert invoke('check',case)['ok']
    run_until(case,'rerun',end,'Allrun.serial')
    if conjugate:
        assert load(case/'mesh_gate.json')['ok']
        result = invoke('flow',case,'--energy-balance')
    else:
        result = invoke('balance',case)
    assert result['ok']
    return {'initial_parallel_ranks':2,'rerun_time':end,'energy':result,
            'inputs_and_unrelated_files_preserved':True,'idempotent_clean':True}


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='flotherm-clean-',dir=args.report.resolve().parent))
    report = {'passed':False,'root':str(root),'actual_model_calls':0,'simulated_model_calls':0}
    try:
        report['conduction'] = verify(root,conjugate=False)
        report['conjugate'] = verify(root,conjugate=True)
        report['passed'] = True
    finally:
        args.report.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('FLOTHERM_CLEAN_PASS',args.report)
