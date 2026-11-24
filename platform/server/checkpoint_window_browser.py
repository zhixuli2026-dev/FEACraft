"""Open the real workbench and compare recovered content with the committed checkpoint."""
import json
from pathlib import Path
import sys

from playwright.sync_api import expect, sync_playwright


def verify(data):
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        try:
            ctx = browser.new_context(viewport={'width':1500, 'height':900},
                                      permissions=['clipboard-read','clipboard-write'])
            ctx.add_init_script("if(location.origin==="+json.dumps(data['base'])+
                                ")localStorage.setItem('vf_token',"+json.dumps(data['token'])+");")
            page = ctx.new_page()
            page.set_default_timeout(45000)
            page.goto(data['base'])
            def select_conversation():
                case = page.locator('[data-case-name="'+data['case']+'"]')
                tab = page.locator('.dv-tab:visible').filter(has_text='Agent').first
                expect(case.or_(tab).first).to_be_visible()
                if case.is_visible():
                    case.click()
                tab.click()
                page.get_by_test_id('agent-conversation').select_option(str(data['cid']))
            messages = page.get_by_test_id('chat-message')
            partial = messages.filter(has_text='Agent · 失败' if data.get('failed') else 'Agent · 已中断')
            def check():
                expect(partial).to_have_count(1)
                expect(partial.get_by_test_id('chat-markdown')).to_be_visible()
                expect(partial.get_by_test_id('chat-branch')).to_have_count(0)
                expect(partial.get_by_test_id('run-duration')).to_have_attribute('data-run-id',data['run'])
                copy = partial.get_by_test_id('chat-copy')
                page.bring_to_front()
                page.evaluate("navigator.clipboard.writeText('checkpoint browser verification')")
                copy.click()
                expect(copy).to_have_text('已复制')
                assert page.evaluate('navigator.clipboard.readText()')==data['partial']['content']
                reasoning = data['partial']['reasoning']
                if reasoning:
                    card = partial.get_by_test_id('reasoning-content')
                    if not card.is_visible():
                        partial.get_by_test_id('reasoning-toggle').click()
                    assert card.text_content()==reasoning
                expect(messages.last.get_by_test_id('chat-branch')).to_have_count(1)
            select_conversation()
            check()
            if not data.get('failed'):
                page.get_by_test_id('chat-load-older').click()
                expect(page.get_by_test_id('chat-load-older')).to_have_count(0)
                check()
            page.reload()
            select_conversation()
            check()
            return {'chromium':browser.version,'view_checks':2 if data.get('failed') else 3,'one_partial_in_order':True,
                    'clipboard_matches_committed_content':True,'reasoning_matches':bool(data['partial']['reasoning']),
                    'branch_only_on_completed_answer':True}
        finally:
            browser.close()


if __name__=='__main__':
    data=json.load(sys.stdin)
    Path(data['output']).write_text(json.dumps(verify(data),indent=2),encoding='utf8')
