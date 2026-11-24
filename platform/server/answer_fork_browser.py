"""Fork two actual streamed replies with identical text in the production workbench."""
import json
from pathlib import Path
import sys

from playwright.sync_api import expect, sync_playwright


def verify(data):
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        try:
            ctx = browser.new_context(viewport={'width':1660, 'height':1000})
            ctx.add_init_script("if(location.origin==="+json.dumps(data['base'])+
                                ")localStorage.setItem('vf_token',"+json.dumps(data['token'])+");")
            page = ctx.new_page()
            page.set_default_timeout(60000)
            page.goto(data['base'])
            page.locator('[data-case-name="'+data['case']+'"]').click()
            page.locator('.dv-tab:visible').filter(has_text='Agent').first.click()
            selector = page.get_by_test_id('agent-conversation')
            cid = data['cid']
            selector.select_option(str(cid))
            editor = page.get_by_test_id('agent-input')
            response_ids = []
            for index, prompt in enumerate(data['prompts']):
                if index:
                    res = ctx.request.patch(data['base']+f'/api/v1/conversations/{cid}/permission',
                          headers={'Authorization':'Bearer '+data['token']}, data={'permission_mode':'ask'})
                    assert res.ok
                editor.fill(prompt)
                with page.expect_response(lambda r:r.url.endswith(f'/conversations/{cid}/messages')) as pending:
                    page.get_by_test_id('agent-send').click()
                response = pending.value
                assert response.ok
                events = [json.loads(line[5:]) for line in response.text().splitlines() if line.startswith('data:')]
                end, = [e for e in events if e['type']=='run.completed']
                response_ids.append(end['answer_event_id'])
                expect(page.get_by_test_id('chat-branch')).to_have_count(index+1)
            assert response_ids[0]<response_ids[1]
            headers = {'Authorization':'Bearer '+data['token']}
            def detail(number):
                res = ctx.request.get(data['base']+f'/api/v1/conversations/{number}', headers=headers)
                assert res.ok
                return res.json()
            original = detail(cid)
            answers = [e for e in original['events'] if e.get('can_fork')]
            assert [e['id'] for e in answers]==response_ids
            assert [e['payload']['content'].strip() for e in answers]==[data['marker']]*2
            def fork(source):
                with page.expect_response(lambda r:r.url.endswith(f'/conversations/{source}/fork')) as pending:
                    page.get_by_test_id('chat-branch').first.click()
                response = pending.value
                assert response.ok and response.request.post_data_json=={'head_event_id':response_ids[0]}
                target = response.json()['id']
                expect(selector).to_have_value(str(target))
                expect(page.get_by_test_id('chat-branch')).to_have_count(1)
                timing = page.get_by_test_id('run-duration').last
                expect(timing).to_have_attribute('data-status','completed')
                expect(timing).to_have_attribute('data-run-id',answers[0]['payload']['run_id'])
                return target
            child = fork(cid)
            nested = fork(child)
            prefix = [e['id'] for e in original['events'] if e['id']<=response_ids[0]]
            assert [e['id'] for e in detail(child)['events']]==prefix
            assert [e['id'] for e in detail(nested)['events']]==prefix
            settings = page.locator('.agent-settings')
            if settings.get_attribute('open') is None:
                settings.locator('summary').click()
            page.once('dialog', lambda dialog:dialog.accept('Browser fork'))
            with page.expect_response(lambda r:r.url.endswith(f'/conversations/{nested}') and r.request.method=='PATCH') as rename:
                page.get_by_test_id('agent-rename').click()
            assert rename.value.ok and rename.value.json()['title']=='Browser fork'
            page.once('dialog',lambda dialog:dialog.accept())
            with page.expect_response(lambda r:r.url.endswith(f'/conversations/{nested}') and r.request.method=='DELETE') as removed:
                page.get_by_test_id('agent-delete').click()
            assert removed.value.ok
            expect(selector.locator(f'option[value="{nested}"]')).to_have_count(0)
            return {'chromium':browser.version, 'same_text_distinct_ids':response_ids, 'first_child':child,
                    'nested_deleted':nested, 'prefix_size':len(prefix), 'inherited_duration_ms':answers[0]['answer_state']['duration_ms'],
                    'rename_and_delete':True}
        finally:
            browser.close()


if __name__=='__main__':
    data = json.load(sys.stdin)
    Path(data['output']).write_text(json.dumps(verify(data), indent=2), encoding='utf8')
