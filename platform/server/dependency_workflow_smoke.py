"""Dependency ranges on the real geometry/OpenFOAM/postprocessing/report chain."""
import json

from app.services.casedict import set_block
from professional_launch_smoke import settled
from s3_core_workflow_smoke import main

CORE = ['geometry', 'mesh', 'solve', 'postprocess', 'report']
observations = []


def extra(api, store, phase):
    checks = []
    endpoint = api.prefix+'/dependencies?run_dir=C-run/conduction'
    def graph(label, expected):
        result = api.call('GET', endpoint)
        observations.append({'label': label, 'result': result})
        (store.root/'dependency-observations.json').write_text(json.dumps(observations, indent=2), encoding='utf8')
        assert result['needs_inspection'] == [], result
        assert result['recompute'] == expected, (label, result)
        return result
    if phase == 'stale':
        # Editing the generator starts a real auto-preview job. Observe its
        # completion instead of racing a new publication during graph reads.
        for job in api.call('GET', api.prefix+'/jobs'):
            if job['status'] not in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
                assert settled(api, job['id'])['status'] == 'succeeded'
        result = graph('geometry edit after automatic preview', CORE[1:])
        assert any(r['code'] == 'dependency_stale' for n in result['publications'] if n['kind'] == 'report' for r in n['reasons'])
        checks.append('geometry_edit_stales_downstream_but_retains_completed_new_preview')
        return checks
    graph('confirmed complete workflow', [])
    checks.append('dependency_graph_fresh_after_all_workflow_confirmations')
    flows = api.call('GET', api.prefix+'/workflow-runs')
    history = api.call('GET', '/workflow-runs/'+flows[0]['id'])
    saved = api.call('GET', api.prefix+'/caseDict')
    api.call('PUT', api.prefix+'/caseDict', {'content': saved['content']+'\n// dependency bookkeeping revision\n',
                                            'expected_revision': saved['dict_revision']})
    graph('bookkeeping revision', [])
    checks.append('bookkeeping_revision_does_not_invalidate_engineering')
    def rewrite(text):
        current = api.call('GET', api.prefix+'/caseDict')
        api.call('PUT', api.prefix+'/caseDict', {'content': text, 'expected_revision': current['dict_revision']})
    for label, block, value in (
        ('mesh parameters', 'mesh', {'source': 'blockMesh', 'qualityTargets': {'maxNonOrtho': 60, 'policy': 'fail'}}),
        ('solver configuration', 'numericalMethod', {'solver': 'simpleFoam'})):
        rewrite(set_block(saved['content'], block, value))
        graph(label, CORE)
        rewrite(saved['content'])
        graph(label+' restored', [])
        checks.append(label.replace(' ', '_')+'_invalidates_and_restoration_reuses_matching_content')
    for path, label, old, new in (('C-run/conduction/system/blockMeshDict', 'mesh input file', '(20 4 4)', '(24 4 4)'),
                        ('C-run/conduction/0/T', 'initial temperature field', 'uniform 300', 'uniform 310'),
                        ('C-run/conduction/constant/transportProperties', 'thermal diffusivity', '0.01;', '0.02;')):
        original = api.call('GET', api.prefix+'/raw?path='+path, raw=True).decode('utf8')
        assert old in original, path
        api.put(path, original.replace(old, new))
        graph(label, CORE[1:])
        api.put(path, original)
        graph(label+' restored', [])
        checks.append(label.replace(' ', '_')+'_keeps_geometry_and_invalidates_downstream')
    other = api.call('GET', api.prefix+'/dependencies?run_dir=C-run/other')
    assert other['recompute'] == CORE[1:] and not other['needs_inspection'], other
    checks.append('run_directory_mismatch_requires_new_run_evidence')
    after = api.call('GET', '/workflow-runs/'+flows[0]['id'])
    assert after == history, (history, after)
    checks.append('dependency_inspection_and_input_edits_preserve_terminal_workflow_history')
    return checks


if __name__ == '__main__':
    main(extra)
