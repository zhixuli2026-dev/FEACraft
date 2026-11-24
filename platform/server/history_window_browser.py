"""Exercise the shipped workbench against the isolated history acceptance server."""
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


def verify(data):
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        try:
            ctx = browser.new_context(viewport={'width':1660,'height':1000})
            ctx.add_init_script("if(location.origin==="+json.dumps(data['base'])+
                                ")localStorage.setItem('vf_token',"+json.dumps(data['token'])+");")
            posts = []
            ctx.on('request', lambda req: posts.append(req.method) if req.method=='POST'
                   and re.search(r'/conversations/\d+/messages$', req.url) else None)
            page = ctx.new_page()
            page.set_default_timeout(30000)
            cid = data['cid']

            def open_history():
                page.locator('[data-case-name="'+data['case']+'"]').click()
                page.locator('.dv-tab:visible').filter(has_text='Agent').first.click()
                selector = page.get_by_test_id('agent-conversation')
                selector.select_option(str(cid))
                duration = page.get_by_test_id('run-duration').filter(has_text='秒')
                expect(duration).to_have_attribute('data-run-id', data['run_id'])
                expect(duration).to_have_attribute('data-status', 'completed')
                expect(page.get_by_test_id('chat-message').filter(has_text=data['marker'])).to_have_count(2)
                return duration.inner_text()

            page.goto(data['base'])
            duration = open_history()
            older = page.get_by_test_id('chat-load-older')
            expect(older).to_be_visible()
            nav = page.get_by_test_id('chat-user-nav').locator('option')
            before = nav.count()
            response = ctx.request.post(data['base']+f'/api/v1/testing/windows/{cid}/seed/append',
                                        headers={'Authorization':'Bearer '+data['token']})
            assert response.ok
            with page.expect_response(lambda res:f'/conversations/{cid}?' in res.url and 'offset=' in res.url) as next_page:
                older.click()
            reply = next_page.value
            query = parse_qs(urlparse(reply.url).query)
            assert reply.ok and query['offset']==['280'] and int(query['snapshot_id'][0])>0
            expect(nav).to_have_count(322)
            labels = nav.all_inner_texts()
            numbers = [int(m.group(1)) for label in labels if (m:=re.search(r'HISTORY-user-(\d+)', label))]
            assert numbers==list(range(1,321)) and before<322
            assert not any('HISTORY-append-' in label for label in labels)
            page.reload()
            assert open_history()==duration
            expect(page.get_by_test_id('chat-user-nav')).to_contain_text('HISTORY-append-')
            # Only the compact-layout browser query fails. Production React,
            # recovery timers, authentication, network and reload all run normally.
            fault = """
                const original = window.matchMedia.bind(window);
                window.matchMedia = q => {
                  if (q === '(max-width: 760px)' && sessionStorage.historyFault !== 'off')
                    throw new Error('isolated-history-display-fault');
                  return original(q);
                };
            """
            for persistent in (False, True):
                tab = ctx.new_page()
                tab.add_init_script(fault)
                tab.goto(data['base'])
                alert = tab.locator('[role="alert"][data-recovery-attempt]')
                if persistent:
                    expect(alert).to_have_attribute('data-display-generation', '3')
                    expect(alert).to_have_attribute('data-recovering', 'false')
                    tab.evaluate("sessionStorage.historyFault='off'")
                    with tab.expect_navigation():
                        alert.get_by_role('button').click()
                    expect(tab.locator('[data-display-generation="0"] [data-case-name]').first).to_be_visible()
                else:
                    expect(alert).to_have_attribute('data-recovery-attempt', '1')
                    tab.evaluate("sessionStorage.historyFault='off'")
                    expect(tab.locator('[data-display-generation="1"] [data-case-name]').first).to_be_visible()
                tab.close()
            assert posts==[]
            return {'chromium':browser.version, 'source_user_rows':len(numbers), 'first_page_options':before,
                    'snapshot':int(query['snapshot_id'][0]), 'duration_label':duration.strip(),
                    'transient_remounts':1, 'persistent_remounts':3, 'manual_reload_restored':True,
                    'message_posts_from_history_and_recovery':len(posts)}
        finally:
            browser.close()


if __name__=='__main__':
    data = json.load(sys.stdin)
    Path(data['output']).write_text(json.dumps(verify(data), ensure_ascii=True), encoding='utf8')
