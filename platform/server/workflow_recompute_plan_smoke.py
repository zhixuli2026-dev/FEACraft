"""Recomputation plan boundaries: target scope, reuse prefix, identities and gates."""
from copy import deepcopy
import uuid

from app.services.dependency_status import CORE
from app.services.workflow_recompute import make_plan


def main():
    count = 0
    for index, target in enumerate(CORE):
        kinds = list(CORE[:index+1])
        for boundary in range(len(kinds)):
            reused = {kind: {'id': str(uuid.uuid4()), 'kind': kind, 'generation': 1,
                'attempt_id': str(uuid.uuid4()), 'manifest_sha256': 'a'*64} for kind in kinds[:boundary]}
            lineage = {'source_run_id': str(uuid.uuid4()), 'source_run_version': 3,
                'observed_revision': 5, 'observed_event_sequence': 8, 'reused': reused, 'rebuild': kinds[boundary:]}
            plan = make_plan(target, {'geometry_mode': 'export', 'report_mode': 'compose'}, lineage)
            native = [step for step in plan['steps'] if step['key'] in CORE]
            assert [step['key'] for step in native] == kinds
            assert [step['key'] for step in native if 'reuse' not in step] == lineage['rebuild']
            confirmations = [step['confirmation_state'] for step in plan['steps'] if step['confirmation_state']]
            assert confirmations == list('BCDE'[:min(index+1, 4)])
            assert all(step['dependencies'] == [plan['steps'][i-1]['key']] for i,step in enumerate(plan['steps']) if i)
            count += 1
    original = lineage
    faults = [
        lambda d: d.update(rebuild=[]),
        lambda d: d['reused'].pop('geometry'),
        lambda d: d.update(rebuild=['mesh', 'report']),
        lambda d: d.update(source_run_id='not-a-uuid'),
        lambda d: d.update(source_run_version=True),
        lambda d: d['reused']['geometry'].update(generation=True),
        lambda d: d['reused']['geometry'].update(kind='solve'),
        lambda d: d['reused']['geometry'].update(manifest_sha256='unknown'),
        lambda d: d['reused']['geometry'].update(attempt_id=None),
        lambda d: d.update(unregistered_field={}),
    ]
    for fault in faults:
        damaged = deepcopy(original)
        fault(damaged)
        try:
            make_plan('report', {'geometry_mode': 'export'}, damaged)
        except (ValueError, TypeError):
            count += 1
        else:
            raise AssertionError(damaged)
    print('WORKFLOW_RECOMPUTE_PLAN_OK '+str({'count': count, 'valid_target_prefixes': 15, 'rejected_invalid_plans': 10}), flush=True)


if __name__ == '__main__':
    main()
