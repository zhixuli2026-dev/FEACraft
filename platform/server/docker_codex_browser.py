"""Use the normal workbench to submit real Docker Codex plans and images."""
import base64
import json
from pathlib import Path
import re
import sys
import time

from playwright.sync_api import expect, sync_playwright


def verify(config):
    root = Path(config['artifacts'])
    cid = config['conversation']
    proof = config['proof']
    report = {'passed':False}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width':1440,'height':960},locale='zh-CN')
        context.add_init_script('localStorage.setItem("vf_token",'+json.dumps(config['token'])+')')
        page = context.new_page()
        page.set_default_timeout(50_000)
        native = page.get_by_test_id('native-chat')
        def open_workbench():
            page.locator('[data-case-name='+json.dumps(config['case'])+']').click()
            tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$'))
            if not tab.count():
                right = page.locator('.dv-groupview').filter(
                    has=page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^公式识别$')))
                right.locator('.dv-tabs-overflow-dropdown-default:visible').click()
                tab = page.locator('.dv-tabs-overflow-container .dv-tab:visible').filter(has_text=re.compile(r'^Agent$'))
            tab.click()
            page.get_by_test_id('agent-conversation').select_option(str(cid))
        try:
            page.goto(config['base'],wait_until='domcontentloaded')
            open_workbench()
            backend = page.get_by_test_id('agent-backend')
            expect(backend.locator('option[value="docker"]')).to_have_text('Codex · Docker')
            backend.select_option('docker')
            expect(native).to_be_visible()
            task = native.get_by_test_id('native-input')
            task.fill('Call vibeflow_read_case_file exactly once with path ref-codex-docker.txt. '
                      'Use no other tool and do not write files. Reply with the exact reference it returns.')
            with page.expect_response(lambda r:r.request.method=='POST'
                    and r.url.endswith(f'/api/v1/codex/conversations/{cid}/session-requests'),timeout=110_000) as opened:
                native.get_by_role('button',name='发送',exact=True).click()
            assert opened.value.status==200 and opened.value.request.post_data_json['runtime']=='docker'
            startup_id = opened.value.json()['id']
            reply = native.get_by_text(proof,exact=False).first
            expect(reply).to_be_visible(timeout=130_000)
            expect(native.get_by_role('button',name='发送',exact=True)).to_be_disabled()
            reply.scroll_into_view_if_needed()
            page.screenshot(path=str(root/'codex-docker-workbench.png'))
            page.reload(wait_until='domcontentloaded')
            open_workbench()
            expect(backend).to_have_value('docker',timeout=85_000)
            expect(reply).to_be_visible(timeout=85_000)
            reply.scroll_into_view_if_needed()
            page.screenshot(path=str(root/'codex-docker-reloaded.png'))
            report.update(passed=True,edge=edge.version,retained_backend=True,reply_visible=True,
                          startup_id=startup_id)
            if config.get('writes'):
                approve = page.get_by_test_id('approve-plan')
                expect(approve).to_be_enabled(timeout=100_000)
                with page.expect_response(lambda r:r.request.method=='PATCH'
                        and r.url.endswith(f'/api/v1/conversations/{cid}/permission')) as approved:
                    approve.click()
                assert approved.value.status==200 and approved.value.json()['permission_mode']=='approve_writes'
                native.get_by_role('checkbox',name='恢复原生线程').check()
                target = 'ref-codex-workbench.txt'
                task.fill(f'Actually run cp ref-codex-docker.txt {target} in /workspace with the shell tool. '
                          'Use normal workspace permissions. Confirm after the copy succeeds.')
                with page.expect_response(lambda r:r.request.method=='POST'
                        and r.url.endswith(f'/api/v1/codex/conversations/{cid}/session-requests'),
                        timeout=110_000) as writable:
                    native.get_by_role('button',name='发送',exact=True).click()
                assert writable.value.status==200
                report['write_startup_id']=writable.value.json()['id']
                deadline=time.monotonic()+130
                while time.monotonic()<deadline:
                    file_url=f"{config['base']}/api/v1/cases/{config['case_id']}/file?path={target}"
                    response=context.request.get(file_url,headers={'Authorization':'Bearer '+config['token']})
                    if response.ok and response.json()['content']==proof:
                        break
                    page.wait_for_timeout(230)
                else:
                    raise AssertionError('The browser-triggered write was not published')
                page.screenshot(path=str(root/'codex-docker-plan-written.png'))
                report['continued_plan_write']=True
            if (examples := config.get('images')):
                cid = examples['conversation']
                page.get_by_test_id('agent-conversation').select_option(str(cid))
                expect(backend).to_have_value('api')
                backend.select_option('docker')
                expect(backend).to_have_value('docker')
                native.get_by_test_id('native-image-input').set_input_files([
                    {'name': asset['name'], 'mimeType': asset['mime'], 'buffer': base64.b64decode(asset['data'])}
                    for asset in examples['assets']])
                expect(native.get_by_test_id('attachment-chip')).to_have_count(3)
                task.fill(examples['prompt'])
                with page.expect_response(lambda r:r.request.method=='POST'
                        and r.url.endswith(f'/api/v1/codex/conversations/{cid}/session-requests'),
                        timeout=110_000) as started:
                    native.get_by_role('button',name='发送',exact=True).click()
                assert started.value.status==200
                report['images_startup_id']=started.value.json()['id']
                digits = re.compile(r'^\s*' + r',\s*'.join(examples['expected']) + r'\s*$')
                matched = native.get_by_text(digits).first
                expect(matched).to_be_visible(timeout=130_000)
                page.reload(wait_until='domcontentloaded')
                open_workbench()
                expect(matched).to_be_visible(timeout=85_000)
                saved_images = native.get_by_test_id('chat-image')
                expect(saved_images).to_have_count(3,timeout=85_000)
                for image in saved_images.all():
                    expect(image).to_have_js_property('naturalWidth',96)
                matched.scroll_into_view_if_needed()
                page.screenshot(path=str(root/'codex-docker-image-history.png'))
                report['images_visible_after_reload']=True
        except Exception:
            report['passed']=False
            page.screenshot(path=str(root/'codex-docker-workbench-failure.png'))
            raise
        finally:
            edge.close()
            (root/'codex-docker-edge.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')


if __name__=='__main__':
    verify(json.load(sys.stdin))
