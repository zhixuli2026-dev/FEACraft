"""Choose a native Analyze writer and download both published files in Edge."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


CASE = 'Native Analyze export in Edge'


def browser_test(data):
    from playwright.sync_api import expect, sync_playwright

    root = Path(data['root'])
    result = {'passed':False,'actual_model_calls':0,'mock_llm_calls':0}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge')
        page = browser.new_page(viewport={'width':1440,'height':900},locale='zh-CN')
        page.set_default_timeout(30000)
        page.add_init_script('localStorage.setItem("vf_token",'+json.dumps(data['auth'])+');')
        try:
            page.goto(data['base'],wait_until='domcontentloaded')
            page.locator('[data-case-name='+json.dumps(CASE)+']').click()
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^可视化$')).click()
            catalog = page.locator('.vf-pv-catalog')
            catalog.get_by_text('ParaView 来源与过滤器',exact=True).click()
            catalog.get_by_text('ParaView 插件',exact=True).click()
            catalog.get_by_label('ParaView 插件',exact=True).select_option('AnalyzeNIfTIReaderWriter')
            catalog.get_by_label('插件条目',exact=True).select_option('writers/AnalyzeWriter')
            form = catalog.locator('form').filter(has=page.get_by_role('button',name='导出',exact=True))
            form.get_by_label('输入数据集',exact=True).select_option('D-post/isotropic.vti')
            with page.expect_response(lambda reply: reply.ok and reply.request.method=='POST'
                                      and reply.url.endswith('/paraview/pipeline')) as sent:
                form.get_by_role('button',name='导出',exact=True).click()
            assert sent.value.request.post_data_json['volume_format'] == 'Analyze'
            fields = {}
            for name, length in (('volume.hdr',348),('volume.img',128)):
                button = form.get_by_role('button',name='下载 '+name,exact=True)
                expect(button).to_be_visible(timeout=120000)
                with page.expect_download() as downloaded:
                    button.click()
                original = Path(downloaded.value.path()).read_bytes()
                assert len(original) == length
                fields[name] = hashlib.sha256(original).hexdigest()
            for width in (1440,390):
                page.set_viewport_size({'width':width,'height':900})
                button.scroll_into_view_if_needed()
                assert form.evaluate('(node) => node.scrollWidth <= node.clientWidth + 2')
                page.screenshot(path=str(root/f'volume-{width}.png'))
            result.update(passed=True,job_id=sent.value.json()['job_id'],files=fields,widths=[1440,390])
        finally:
            if not result['passed']:
                page.screenshot(path=str(root/'volume-failed.png'))
            (root/'volume-browser.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
            browser.close()


def main(web, native, browser_python):
    import httpx
    from testsupport import IsolatedServer

    api = IsolatedServer('ff-d20-volume-edge',app='volume_fixture:app')
    print('VOLUME_EDGE_EVIDENCE '+str(api.root),flush=True)
    (api.root/'volume_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/",StaticFiles(directory={str(web)!r},html=True))\n',encoding='utf8')
    fixture = Path(__file__).with_name('paraview_volume_acceptance.py')
    subprocess.run([str(native),str(fixture),'--make',str(api.root)],check=True,timeout=45)
    with api, httpx.Client(base_url=api.base+'/api/v1',trust_env=False,timeout=30) as client:
        api.create_case(CASE)
        client.headers['Authorization'] = 'Bearer '+api.token
        original = (api.root/'isotropic.vti').read_bytes()
        upload = client.post(api.prefix+'/upload',params={'path':'D-post/isotropic.vti'},
                             files={'file':('isotropic.vti',original,'application/octet-stream')})
        upload.raise_for_status()
        subprocess.run([str(browser_python),'-B','-X','utf8',__file__,'--edge'],check=True,timeout=210,
                       input=json.dumps({'root':str(api.root),'base':api.base,'auth':api.token}),text=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--edge']:
        browser_test(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        for key in ('web','native','browser-python'):
            parser.add_argument('--'+key, type=Path, required=True)
        options = parser.parse_args()
        main(*(path.resolve(strict=True) for path in (options.web,options.native,options.browser_python)))
