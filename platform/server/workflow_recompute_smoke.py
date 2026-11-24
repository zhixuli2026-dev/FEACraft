"""Real new-report recomputation with immutable reuse, explicit gates and races."""
from concurrent.futures import ThreadPoolExecutor
import json
import uuid

import s3_core_workflow_smoke as core
from job_release_smoke import wait_file
from professional_launch_smoke import settled

original_api = core.workflow_api
FIXTURE = '''
import asyncio
from app.services import dependency_status
original_dependency_inspect=dependency_status.inspect
recompute_reads=0
async def dependency_inspect(*args,**kwargs):
 global recompute_reads
 result=await original_dependency_inspect(*args,**kwargs)
 if (root/'recompute-hold').exists():
  recompute_reads+=1
  (root/('recompute-ready-'+str(recompute_reads))).touch()
  deadline=time.monotonic()+90
  while not (root/'recompute-release').exists():
   if time.monotonic()>deadline:raise TimeoutError('recompute planning fixture')
   await asyncio.sleep(.02)
 return result
dependency_status.inspect=dependency_inspect
from app.services import workflow_execution
original_dispatch=workflow_execution.dispatch
async def dispatch(*args,**kwargs):
 if (root/'fail-recompute-dispatch').exists():
  (root/'fail-recompute-dispatch').unlink()
  raise RuntimeError('injected API failure after durable recomputation creation')
 return await original_dispatch(*args,**kwargs)
workflow_execution.dispatch=dispatch
'''


def api_for(store, peer=None):
    api = original_api(store, peer)
    path = api.root/'s3_typed_fixture.py'
    path.write_text(path.read_text(encoding='utf8')+FIXTURE, encoding='utf8')
    return api


core.workflow_api = api_for
pending_reuse = {}


def extra(api, store, phase):
    if phase != 'published':
        for job in api.call('GET', api.prefix+'/jobs'):
            if job['status'] not in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
                assert settled(api, job['id'])['status'] == 'succeeded'
        flow = api.call('GET', '/workflow-runs/'+pending_reuse['id'])
        step = next(s for s in flow['steps'] if s['key'] == 'confirm_B')
        error = api.call('POST', f'/workflow-runs/{flow["id"]}/steps/{step["id"]}:refresh-confirmation',
            {'expected_version': step['version']}, expect=409)
        assert error['detail']['code'] == 'input_stale', error
        stopped = api.call('POST', '/workflow-runs/'+flow['id']+':cancel', {'expected_version': flow['version']})
        assert stopped['status'] == 'cancelled' and all(not s['attempts'] for s in stopped['steps'])
        parent = api.call('GET', '/workflow-runs/'+pending_reuse['parent_id'])
        saved = api.call('GET', api.prefix+'/caseDict')
        rebuilt = api.call('POST', '/workflow-runs/'+parent['id']+':recompute',
            {'expected_version': parent['version'], 'expected_revision': saved['dict_revision'], 'confirmed_reset': True},
            extra_headers={'Idempotency-Key': 'geometry-change-full-rebuild'})
        assert rebuilt['recomputation']['reused'] == {}
        assert rebuilt['recomputation']['rebuild'] == ['geometry', 'mesh', 'solve', 'postprocess', 'report']
        for key in ('confirm_B', 'confirm_C', 'confirm_D', 'confirm_E'):
            waiting = core.wait(api, rebuilt['id'], key)
            confirmation = next(s for s in waiting['steps'] if s['key'] == key)
            api.call('POST', api.prefix+'/caseDict:advance', {'state': confirmation['confirmation_state'],
                'confirmed': True, 'expected_revision': confirmation['confirmation_revision'],
                'workflow_step_id': confirmation['id'], 'expected_step_version': confirmation['version']})
        finished = core.wait(api, rebuilt['id'])
        assert all(len(s['attempts']) == 1 for s in finished['steps'] if s['key'] in rebuilt['recomputation']['rebuild'])
        assert not api.call('GET', api.prefix+'/report/results')['publication']['stale']
        evidence = json.loads((store.root/'recompute-observations.json').read_text(encoding='utf8'))
        evidence['geometry_recomputed'] = finished
        (store.root/'recompute-observations.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
        return ['reused_confirmation_is_pinned_and_cannot_borrow_new_geometry_head',
                'geometry_change_reexecutes_all_five_native_steps_and_publishes_current_report']
    checks = []
    flows = api.call('GET', api.prefix+'/workflow-runs')
    source = api.call('GET', '/workflow-runs/'+flows[0]['id'])
    endpoint = '/workflow-runs/'+source['id']+':recompute'
    def body():
        saved = api.call('GET', api.prefix+'/caseDict')
        return {'expected_version': source['version'], 'expected_revision': saved['dict_revision'], 'confirmed_reset': True}
    def post(payload, key, **kwargs):
        return api.call('POST', endpoint, payload, extra_headers={'Idempotency-Key': key}, **kwargs)
    path = 'C-run/conduction/constant/transportProperties'
    original = api.call('GET', api.prefix+'/raw?path='+path, raw=True).decode('utf8')
    old_report = next(s for s in source['steps'] if s['key'] == 'report')['publication']['id']
    history_path = f'.vibeflow/publications/{old_report}/files/E-report/build/report.html'
    old_html = api.call('GET', api.prefix+'/raw?path='+history_path, raw=True)
    unchanged = body()
    post(unchanged, 'already-current', expect=409)
    assert len(api.call('GET', api.prefix+'/workflow-runs')) == 1
    checks.append('current_target_does_not_create_an_unnecessary_run')
    api.put(path, original.replace('0.01;', '0.02;'))
    payload = body()
    post(dict(payload, confirmed_reset=False), 'not-confirmed', expect=409)
    api.call('POST', endpoint, payload, expect=422)
    post(dict(payload, expected_revision=payload['expected_revision']+1), 'wrong-revision', expect=409)
    checks.append('reset_confirmation_idempotency_key_and_revision_are_required')
    (api.root/'recompute-hold').touch()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, payload, 'same-request')
        second = pool.submit(post, payload, 'same-request')
        try:
            wait_file(api.root/'recompute-ready-1', seconds=90)
            wait_file(api.root/'recompute-ready-2', seconds=90)
        finally:
            (api.root/'recompute-release').touch()
        left, right = first.result(timeout=120), second.result(timeout=120)
    (api.root/'recompute-hold').unlink()
    (api.root/'recompute-release').unlink()
    assert left['id'] == right['id'] != source['id'], (left, right)
    flow = left
    lineage = flow['recomputation']
    assert lineage['source_run_id'] == source['id']
    assert flow['recomputation_reset']['from_state'] == 'E' and flow['recomputation_reset']['to_state'] == 'A'
    assert lineage['rebuild'] == ['mesh', 'solve', 'postprocess', 'report'], lineage
    assert set(lineage['reused']) == {'geometry'}
    assert len(api.call('GET', api.prefix+'/workflow-runs')) == 2
    assert post(payload, 'same-request')['id'] == flow['id']
    post(dict(payload, target='mesh'), 'same-request', expect=409)
    checks.append('concurrent_identical_requests_create_one_durable_new_workflow')
    checks.append('fresh_geometry_is_reused_and_stale_suffix_is_scheduled')
    for key in ('confirm_B', 'confirm_C', 'confirm_D', 'confirm_E'):
        current = core.wait(api, flow['id'], key)
        step = next(s for s in current['steps'] if s['key'] == key)
        if key == 'confirm_B':
            geo = next(s for s in current['steps'] if s['key'] == 'geometry')
            assert geo['execution_mode'] == 'reuse' and not geo['attempts']
            assert geo['publication']['id'] == lineage['reused']['geometry']['id']
            assert api.call('GET', api.prefix+'/caseDict')['workflow_state'] == 'A'
        result = api.call('POST', api.prefix+'/caseDict:advance', {'state': step['confirmation_state'],
            'confirmed': True, 'expected_revision': step['confirmation_revision'],
            'workflow_step_id': step['id'], 'expected_step_version': step['version']})
        assert result['workflow_dispatch']['status'] == 'dispatched', result
    done = core.wait(api, flow['id'])
    assert post(payload, 'same-request')['id'] == done['id']
    assert all(len(s['attempts']) == 1 for s in done['steps'] if s['key'] in lineage['rebuild']), done
    assert api.call('GET', '/workflow-runs/'+source['id']) == source
    report = api.call('GET', api.prefix+'/report/results')
    assert not report['publication']['stale'] and report['publication']['id'] != old_report
    assert api.call('GET', api.prefix+'/raw?path='+history_path, raw=True) == old_html
    checks.append('explicit_B_to_E_gates_execute_new_mesh_solve_postprocess_and_report')
    checks.append('old_workflow_and_old_report_remain_intact')
    # A source edit during lock-free planning must not reset the stage or leave
    # a new run behind. The mutation happens after the graph was inspected.
    api.put(path, original.replace('0.01;', '0.03;'))
    changed = body()
    before = api.call('GET', api.prefix+'/caseDict')
    (api.root/'recompute-hold').touch()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(post, changed, 'source-race', expect=409)
        try:
            wait_file(api.root/'recompute-ready-3', seconds=90)
            api.put(path, original.replace('0.01;', '0.04;'))
        finally:
            (api.root/'recompute-release').touch()
        conflict = future.result(timeout=120)
    (api.root/'recompute-hold').unlink()
    (api.root/'recompute-release').unlink()
    assert conflict['detail']['code'] == 'input_stale', conflict
    assert api.call('GET', api.prefix+'/caseDict') == before
    assert len(api.call('GET', api.prefix+'/workflow-runs')) == 2
    checks.append('edit_during_planning_rejects_without_stage_reset_or_orphan_run')
    cancelled = post(dict(body(), target='mesh'), 'cancel-before-new-native')
    assert cancelled['recomputation']['rebuild'] == ['mesh']
    assert not {'solve', 'postprocess', 'report'} & {s['key'] for s in cancelled['steps']}
    checks.append('mesh_target_excludes_solver_postprocessing_and_report_steps')
    pending = core.wait(api, cancelled['id'], 'confirm_B')
    stopped = api.call('POST', '/workflow-runs/'+cancelled['id']+':cancel', {'expected_version': pending['version']})
    assert stopped['status'] == 'cancelled' and all(not s['attempts'] for s in stopped['steps'])
    assert api.call('GET', '/workflow-runs/'+source['id']) == source
    checks.append('cancellation_does_not_launch_reused_or_pending_native_steps')
    api.put(path, original.replace('0.01;', '0.02;'))
    # Restore explicit engineering confirmation with the still-current evidence
    # so the original core fixture can continue its fresh-cache checks.
    for stage in 'BCDE':
        saved = api.call('GET', api.prefix+'/caseDict')
        api.call('POST', api.prefix+'/caseDict:advance', {'state': stage, 'confirmed': True,
            'expected_revision': saved['dict_revision']})
    foreign = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex+'@example.com',
        'password': 'isolated-test-123', 'name': 'Unrelated recompute caller'})
    api.call('POST', endpoint, body(), extra_headers={'Authorization': 'Bearer '+foreign['token'],
        'Idempotency-Key': 'foreign'}, expect=404)
    checks.append('unrelated_user_cannot_recompute_owned_history')
    # Change the requested report mode only: four current producers are reusable.
    # Fail precisely after the new run/reset commit and before dispatch.
    mode_payload = dict(body(), parameters={'geometry_mode': 'export', 'report_mode': 'markdown'})
    (api.root/'fail-recompute-dispatch').touch()
    api.call('POST', endpoint, mode_payload, extra_headers={'Idempotency-Key': 'dispatch-interrupted'}, expect=500, raw=True)
    revision_after_reset = api.call('GET', api.prefix+'/caseDict')['dict_revision']
    with core.workflow_api(store, peer=api) as reader:
        recovered = reader.call('POST', endpoint, mode_payload, extra_headers={'Idempotency-Key': 'dispatch-interrupted'})
        assert recovered['status'] == 'queued'
        assert recovered['recomputation']['rebuild'] == ['report']
        assert set(recovered['recomputation']['reused']) == {'geometry', 'mesh', 'solve', 'postprocess'}
        assert reader.call('GET', api.prefix+'/caseDict')['dict_revision'] == revision_after_reset
        started = reader.call('POST', '/workflow-runs/'+recovered['id']+':dispatch', {'expected_version': recovered['version']})
        assert started['status'] == 'awaiting_confirmation'
        pending_reuse.update(id=started['id'], parent_id=source['id'])
    checks.append('changed_report_mode_reuses_four_current_producers')
    checks.append('fresh_API_resumes_committed_run_without_repeating_reset_or_creation')
    before_active = api.call('GET', api.prefix+'/caseDict')
    error = post(dict(body(), parameters={'geometry_mode': 'export', 'report_mode': 'markdown'}), 'other-active-run', expect=409)
    assert error['detail']['code'] == 'state_conflict', error
    assert api.call('GET', api.prefix+'/caseDict') == before_active
    checks.append('another_active_workflow_blocks_reset_and_new_execution')
    evidence = {'source': source, 'recomputed': done, 'cancelled': stopped, 'resumed': started, 'checks': checks}
    (store.root/'recompute-observations.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
    return checks


if __name__ == '__main__':
    core.main(extra)
