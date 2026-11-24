"""Drive the existing workbench against the live Docker ACP acceptance fixture."""
import json
from pathlib import Path
import re
import sys

from playwright.sync_api import expect, sync_playwright


def verify(config):
    base, case = config['base'], config['case']
    report = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge')
        context = browser.new_context(viewport={'width':1440,'height':960}, locale='zh-CN')
        context.add_init_script('localStorage.setItem("vf_token",'+json.dumps(config['token'])+')')
        page = context.new_page()
        page.set_default_timeout(45_000)
        root = Path(config['artifacts'])
        panel = page.get_by_role('region', name='Kimi Code 对话')
        try:
            page.goto(base, wait_until='domcontentloaded')
            page.locator('[data-case-name='+json.dumps(case)+']').click()
            page.locator('[data-path="acp-browser-proof.txt"]').click()
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            page.get_by_test_id('agent-conversation').select_option(str(config['conversation']))
            page.get_by_test_id('agent-backend').select_option('kimi')
            panel.get_by_label('原生模型配置').select_option(str(config['profile']))
            panel.get_by_text('附加算例文件',exact=True).click()
            panel.get_by_role('button', name='附加当前文件').click()
            expect(panel.get_by_role('button', name='移除 acp-browser-proof.txt')).to_be_visible()
            panel.get_by_role('checkbox', name='草稿审批').check()
            page.screenshot(path=str(root/'workbench-files.png'))

            prompt = ('Read /workspace/case/acp-browser-proof.txt using Read, then append "-checked" to the exact '
                      'reference you read and use Write once to save the result to /workspace/workbench-draft.txt. '
                      'Do not include formatting or any other content. Do not retry a declined write.')
            panel.get_by_label('原生文本消息').fill(prompt)
            endpoint = f"/api/v1/acp/conversations/{config['conversation']}/kimi/turns"
            with page.expect_response(lambda response: response.url.endswith(endpoint)
                                      and response.request.method == 'POST', timeout=80_000) as submission:
                panel.get_by_role('button',name='发送',exact=True).click()
            response = submission.value
            assert response.status == 202, response.status
            assert response.request.post_data_json['case_files'] == ['acp-browser-proof.txt']
            approval = panel.get_by_role('button', name='允许写入一次')
            expect(approval).to_be_enabled(timeout=100_000)
            approval.click()
            draft = panel.get_by_text('已保存草稿 · workbench-draft.txt',exact=True)
            expect(draft).to_be_visible(timeout=100_000)
            draft.click()
            expect(panel.locator('pre')).to_contain_text(config['proof']+'-checked')
            panel.get_by_label('workbench-draft.txt 的发布目标').select_option('acp-browser-proof.txt')
            panel.get_by_role('button',name='发布到算例').click()
            status = panel.get_by_text('已发布到 acp-browser-proof.txt',exact=True)
            expect(status).to_be_visible()
            expect(page.locator('.monaco-editor .view-lines').first).to_contain_text(config['proof']+'-checked')
            expect(panel.locator('.native-case-inputs')).not_to_have_attribute('open', '')
            page.screenshot(path=str(root/'workbench-published.png'))
            page.set_viewport_size({'width':880,'height':850})
            status.scroll_into_view_if_needed()
            assert panel.locator('.native-history').bounding_box()['height'] >= 80
            page.screenshot(path=str(root/'workbench-published-compact.png'))
            report.update(passed=True, turn_id=response.json()['id'], edge=browser.version,
                          attachment=True, approval=True, publication=True)
        except Exception:
            page.screenshot(path=str(root/'workbench-failed.png'))
            raise
        finally:
            browser.close()
            (root/'workbench-browser.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')


if __name__ == '__main__':
    verify(json.load(sys.stdin))
