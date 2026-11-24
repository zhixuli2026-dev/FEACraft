"""Verify exact replacements through real DeepSeek, actual storage and Edge."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

from live_llm_agent_recovery_acceptance import stream
from live_llm_file_proposal_history_acceptance import browser_decision, run, select_history, use_case


CASE_NAME = 'FF-F27 exact text replacement'
TOOL = 'propose_text_replacement'
INVALID = {
    'missing_file': 'replacement_file_missing',
    'missing_text': 'replacement_text_missing',
    'ambiguous': 'replacement_text_ambiguous',
    'binary': 'replacement_binary_file',
}
MODES = ('single', 'automatic', 'all_manual', 'deletion', 'rejected', 'stale', 'dictionary', *INVALID)


def write_fixture(api, path, content):
    until = time.monotonic() + 18
    while True:
        response = api.call('PUT', api.prefix+'/file', {'path':path,'content':content}, expect=(200,409))
        if response.get('ok'):
            return
        detail = response.get('detail')
        assert isinstance(detail, dict) and detail.get('code')=='storage_quota_busy', response
        assert time.monotonic()<until, response
        # The server rejected admission before the fixture wrote anything.
        time.sleep(.1)


def confirm_stale(page, api, cid):
    from playwright.sync_api import expect

    use_case(page, api, CASE_NAME)
    page.get_by_test_id('agent-conversation').select_option(str(cid), timeout=25000)
    pending = page.get_by_test_id('approval-card')
    status = pending.get_by_test_id('approval-diff-status')
    status.wait_for(timeout=20000)
    for _ in range(2):
        if status.get_attribute('data-state') == 'stale':
            break
        retry = pending.get_by_role('button', name='重读差异', exact=True)
        retry.click(timeout=10000)
        status.wait_for(timeout=20000)
    expect(status).to_have_attribute('data-state', 'stale', timeout=20000)
    expect(pending.get_by_role('button', name='批准执行', exact=True)).to_be_disabled()
    pending.screenshot(path=api.root/'stale-approval.png')


def exercise(api, page, case, profile, config, mode):
    page.goto('about:blank')
    reason = '精确替换核对'+uuid.uuid4().hex[:7]
    old_marker = '原文_'+uuid.uuid4().hex[:12]
    new_marker = '替换_'+uuid.uuid4().hex[:12]
    original = old_marker
    replacement = new_marker
    path = f'Ref/replacement-{mode}.txt'
    automatic = mode == 'automatic'
    replace_all = mode in {'automatic', 'all_manual'}
    added_lines = removed_lines = 1
    revision = None
    if mode == 'single':
        original = old_marker+'\n'+old_marker
        replacement = new_marker+' $& $1\n'+new_marker
        before = 'header\n'+original+'\ntrailer\n'
        added_lines = removed_lines = 2
    elif replace_all or mode == 'ambiguous':
        before = old_marker+'\nseparator\n'+old_marker+'\n'
        added_lines = removed_lines = 2
    elif mode == 'deletion':
        original = old_marker+'\n'
        replacement = new_marker = ''
        before = 'header\n'+original+'trailer\n'
        added_lines = 0
    elif mode == 'missing_text':
        before = 'kept text\n'
    elif mode == 'binary':
        before = original+'\n\x00data\n'
    elif mode == 'dictionary':
        path = '0-caseDict/caseDict'
        prior = api.call('GET', api.prefix+'/caseDict')
        before = prior['content'].rstrip('\n')+'\n// '+old_marker+'\n'
        revision = api.call('PUT', api.prefix+'/caseDict', {
            'content': before, 'expected_revision': prior['dict_revision']})['dict_revision']
    else:
        before = original+'\n'
    raw = api.prefix+'/raw?path='+path
    if mode not in {'dictionary', 'missing_file'}:
        write_fixture(api, path, before)
        assert api.call('GET', raw, raw=True) == before.encode('utf8')

    cid = api.call('POST', '/conversations', {'case_id':case['id'], 'title':mode,
        'permission_mode':'full_access' if automatic else 'approve_writes',
        'provider_profile_id':profile['id'], 'model':config['model']})['id']
    route = f'/conversations/{cid}'
    requested = {'path':path, 'old_text':original, 'new_text':replacement,
                 'reason':reason, 'replace_all':replace_all}
    message = {'content':'这是隔离验收，平台会校验实际文件。请直接调用一次 '+TOOL+
        '，参数必须是以下 JSON 的原样值。即使平台报告找不到文件、匹配不唯一或文件不是文本，也只尝试这一次。'
        '收到工具结果后只报告结果，不再调用任何工具。\n'+json.dumps(requested, ensure_ascii=False),
        'provider_profile_id':profile['id'], 'model':config['model']}
    events = stream(api, route+'/messages', message, mode+'-proposal', config)
    failures = [event for event in events if event['type'] in {'stream.error','request.failed','request.timed_out'}]
    assert not failures, failures
    state = api.call('GET', route)

    if mode in INVALID:
        assert not state['pending_tools']
        [result] = [event for event in events if event['type']=='tool.result']
        assert result['tool']==TOOL and not result['ok'] and result['code']==INVALID[mode], result
        assert all(result['args'].get(key)==value for key,value in requested.items()), (requested,result['args'])
        assert any(event['type']=='run.completed' for event in events)
        [persisted] = [event['payload'] for event in state['events'] if event['type']=='tool_result']
        assert persisted['code']==result['code']
        if mode=='missing_file':
            api.call('GET', raw, expect=404)
        else:
            assert api.call('GET', raw, raw=True)==before.encode('utf8')
        return {'passed':True, 'tool':TOOL, 'code':result['code'], 'pending_tools':0, 'changed':False}

    candidates = state['tool_calls'] if automatic else state['pending_tools']
    assert len(candidates)==1, [(event.get('tool'), event.get('code')) for event in events if event['type']=='tool.result']
    [proposal] = candidates
    assert proposal['tool']==TOOL
    args = proposal['args']
    assert all(args.get(key)==value for key,value in requested.items()), (requested,args)
    assert args['baseline_sha256']==hashlib.sha256(before.encode('utf8')).hexdigest()
    assert args['proposal_snapshot']=={'kind':'text','before':before}
    approve = route+'/tool-calls/'+proposal['id']+':approve'
    if mode=='single':
        stranger=api.call('POST','/auth/register',{
            'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Replacement permission check'})
        unauthorized={'Authorization':'Bearer '+stranger['token']}
        api.call('POST', approve, {}, expect=404, extra_headers=unauthorized)
        assert api.call('GET', route)['pending_tools'][0]['id']==proposal['id']
    approved = mode not in {'rejected','stale'}
    after = before.replace(original, replacement, -1 if replace_all else 1)
    label = '人工批准'
    if automatic:
        [tool] = [event for event in events if event['type']=='tool.result']
        assert tool['auto'] and tool['ok'] and any(event['type']=='run.completed' for event in events)
        label = '自动应用'
    else:
        assert api.call('GET', raw, raw=True)==before.encode('utf8')
        if mode=='stale':
            # Keep the exact search text and change a different part of the file.
            after = 'external edit\n'+before
            write_fixture(api, path, after)
            confirm_stale(page, api, cid)
            settled = stream(api, approve, {}, 'stale-rejected-at-commit', config)
            [result] = [event for event in settled if event['type']=='tool.result']
            assert result['code']=='file_conflict' and not result['ok'], result
        else:
            current = api.call('GET', api.prefix+'/file?path='+path)
            assert current['kind']=='text' and current['content']==before and current['content_sha256']==args['baseline_sha256']
            use_case(page, api, CASE_NAME)
            browser_decision(page, cid, proposal['id'], approved, reason, added=added_lines, removed=removed_lines,
                             approval_evidence=api.root/(mode+'-approval-events.json'))
            if not approved:
                after, label = before, '已拒绝'

    assert api.call('GET', raw, raw=True)==after.encode('utf8')
    state = api.call('GET', route)
    assert not state['pending_tools']
    [result] = [event['payload'] for event in state['events'] if event['type']=='tool_result']
    assert result['ok']==approved
    if mode=='stale':
        assert result['code']=='file_conflict'
    if revision is not None:
        assert api.call('GET', api.prefix+'/caseDict')['dict_revision'] == revision+1
    api.call('POST', approve, {}, expect=409)
    if automatic:
        assert after.count(new_marker)==2
        write_fixture(api, path, 'edited after automatic replacement\n')
    use_case(page, api, CASE_NAME)
    select_history(page, cid, path, reason, label, old_marker, new_marker,
                   added=added_lines, removed=removed_lines, screenshot=api.root/(mode+'-history.png'))
    return {'passed':True, 'conversation_id':cid, 'tool_id':proposal['id'], 'path':path, 'reason':reason,
            'label':label, 'before':old_marker, 'after':new_marker, 'history_added':added_lines,
            'history_removed':removed_lines, 'dictionary_revision':None if revision is None else revision+1,
            'committed':approved, 'conflict':result.get('code'), 'other_account_rejected':mode=='single'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--dist', type=lambda path: Path(path).resolve(strict=True), required=True)
    parser.add_argument('--modes', default=','.join(MODES))
    parser.add_argument('--wire', choices=('completions','messages'), default='completions')
    parser.add_argument('--s3', action='store_true')
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    modes = args.modes.split(',')
    if len(set(modes)) != len(modes) or not set(modes) <= set(MODES) or not (args.dist/'index.html').is_file():
        parser.error('Choose a built frontend and unique replacement modes')
    sys.exit(asyncio.run(run(args, case_name=CASE_NAME, scenario=exercise,
                             evidence_label='live-text-replacement', continue_on_error=True)))
