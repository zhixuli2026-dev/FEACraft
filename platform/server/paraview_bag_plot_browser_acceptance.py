"""Exercise real ParaView statistical charts and field-based displays in Edge."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


CASE = 'Bag Plot table in Edge'
DISPLAY_CASE = 'Native vector representation in Edge'
PANORAMA_CASE = 'Native panoramic projection in Edge'


def browser_test(data):
    from playwright.sync_api import expect, sync_playwright

    output = Path(data['evidence'])
    result = {'passed': False, 'actual_model_calls': 0, 'mock_llm_calls': 0}
    representation = data.get('representation',False)
    panorama = data.get('panorama',False)
    case = PANORAMA_CASE if panorama else DISPLAY_CASE if representation else CASE
    evidence = 'panorama' if panorama else 'representation' if representation else 'bag-plot'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge')
        page = browser.new_page(viewport={'width':1440, 'height':900}, locale='zh-CN')
        page.set_default_timeout(30000)
        page.add_init_script('localStorage.setItem("vf_token",'+json.dumps(data['auth'])+');')
        try:
            page.goto(data['url'], wait_until='domcontentloaded')
            page.locator('[data-case-name='+json.dumps(case)+']').click()
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^可视化$')).click()
            panel = page.locator('.vf-pv-catalog')
            panel.get_by_text('ParaView 来源与过滤器',exact=True).click()
            panel.get_by_text('ParaView 插件',exact=True).click()
            if representation or panorama:
                result.update(verify_display(page,panel,output,expect,panorama),passed=True)
                return
            panel.get_by_label('ParaView 插件',exact=True).select_option('BagPlotViewsAndFilters')
            panel.get_by_label('插件条目',exact=True).select_option('filters/ExtractBagPlots')
            form = panel.locator('form').filter(has=page.get_by_label('Variables of Interest',exact=True))
            picker = form.get_by_label('Variables of Interest',exact=True)
            with page.expect_response(lambda reply:'/paraview/dataset?' in reply.url,timeout=40000) as table:
                form.get_by_label('输入数据集',exact=True).select_option('D-post/observations.vtt')
            assert table.value.ok, table.value.status
            assert sum(item['association'] == '行' for item in table.value.json()['arrays']) == 12
            picker.scroll_into_view_if_needed()
            expect(picker.locator('option')).to_have_count(12,timeout=20000)
            columns = [f'sample{index}' for index in range(12)]
            picker.select_option(columns)
            form.get_by_label('GridSize',exact=True).fill('24')
            submit = form.get_by_role('button',name='应用并生成结果',exact=True)
            with page.expect_response(lambda reply: reply.ok and reply.request.method=='POST'
                                      and reply.url.endswith('/paraview/pipeline')) as sent:
                submit.click()
            request = sent.value.request.post_data_json
            assert request['plugins'] == ['BagPlotViewsAndFilters']
            assert set(request['nodes'][-1]['properties']['SelectArrays']) == set(columns)
            job = sent.value.json()
            stats = form.get_by_text(re.compile(rf"作业 #{job['job_id']} · \d+ 个点"))
            expect(stats).to_be_visible(timeout=150000)
            chart = form.get_by_role('img',name='Bag Plot 统计图',exact=True)
            expect(chart).to_be_visible(timeout=45000)
            page.wait_for_function("img => img.complete && img.naturalWidth === 900",arg=chart.element_handle())
            with page.expect_download() as downloaded:
                form.get_by_role('button',name='下载统计图',exact=True).click()
            chart_bytes = Path(downloaded.value.path()).read_bytes()
            assert chart_bytes.startswith(b'\x89PNG\r\n\x1a\n')
            result.update(job_id=job['job_id'], selected_columns=columns, widths=[])
            result['chart_sha256'] = hashlib.sha256(chart_bytes).hexdigest()
            for width in (1440,390):
                page.set_viewport_size({'width':width,'height':900})
                chart.scroll_into_view_if_needed()
                assert form.evaluate('(node) => node.scrollWidth <= node.clientWidth + 2')
                page.screenshot(path=str(output/f'bag-plot-{width}.png'))
                result['widths'].append(width)
            result['passed'] = True
        finally:
            if not result['passed']:
                page.screenshot(path=str(output/(evidence+'-failed.png')))
            (output/(evidence+'-browser.json')).write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
            browser.close()


def verify_display(page, panel, output, expect, panorama):
    plugin = 'PanoramicProjectionView' if panorama else 'SurfaceLIC'
    mode = 'Equirectangular' if panorama else 'Surface LIC'
    panel.get_by_label('ParaView 插件',exact=True).select_option(plugin)
    panel.get_by_label('插件条目',exact=True).select_option('view/'+mode)
    label = '全景角度（°）' if panorama else '矢量场'
    form = panel.locator('form').filter(has=page.get_by_label(label,exact=True))
    submit = form.get_by_role('button',name='生成预览',exact=True)
    expect(submit).to_be_disabled()
    if panorama:
        form.get_by_label('输入数据集',exact=True).select_option('D-post/panorama.vtp')
        form.get_by_label(label,exact=True).fill('270')
        settings = {'projection':{'mode':mode,'angle':270}}
    else:
        selection = ['POINTS','Velocity']
        with page.expect_response(lambda reply:'/paraview/dataset?' in reply.url,timeout=40000) as dataset:
            form.get_by_label('输入数据集',exact=True).select_option('D-post/surface.vtp')
        assert dataset.value.ok,dataset.value.status
        form.get_by_label(label,exact=True).select_option(json.dumps(selection,separators=(',',':')))
        settings = {'representation':{'mode':mode,'array':selection}}
    with page.expect_response(lambda reply: reply.ok and reply.request.method=='POST'
                              and reply.url.endswith('/paraview/pipeline')) as sent:
        submit.click()
    request = sent.value.request.post_data_json
    assert request['plugins'] == [plugin] and request['inspection'] == settings
    job = sent.value.json()
    expect(form.get_by_text(re.compile(rf"作业 #{job['job_id']} · \d+ 个点"))).to_be_visible(timeout=150000)
    image = form.get_by_role('img',name='ParaView 检查视图',exact=True)
    expect(image).to_be_visible(timeout=45000)
    page.wait_for_function('(args) => args[0].complete && args[0].naturalWidth === args[1]',
                           arg=[image.element_handle(),1024 if panorama else 800])
    if panorama:
        with page.expect_download() as downloaded:
            form.get_by_role('button',name='下载全景图',exact=True).click()
        png = Path(downloaded.value.path()).read_bytes()
        assert png.startswith(b'\x89PNG\r\n\x1a\n')
        settings['image_sha256'] = hashlib.sha256(png).hexdigest()
    widths = []
    for width in (1440,390):
        page.set_viewport_size({'width':width,'height':900})
        image.scroll_into_view_if_needed()
        assert form.evaluate('(node) => node.scrollWidth <= node.clientWidth + 2')
        kind = 'panorama' if panorama else 'representation'
        page.screenshot(path=str(output/f'{kind}-{width}.png'))
        widths.append(width)
    return {'job_id':job['job_id'],'widths':widths,**settings}


def main(web, native, edge_python, representation, panorama):
    import httpx
    from testsupport import IsolatedServer

    label = 'ff-d20-panorama-edge' if panorama else 'ff-d20-representation-edge' if representation else 'ff-d20-bag-plot-edge'
    api = IsolatedServer(label,app='bag_plot_fixture:app')
    print('PARAVIEW_EDGE_EVIDENCE '+str(api.root),flush=True)
    (api.root/'bag_plot_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(web)!r},html=True))\n',encoding='utf8')
    fixture = Path(__file__).with_name('paraview_engineering_plugins_acceptance.py')
    inputs = api.root/'inputs'
    inputs.mkdir()
    filename = 'panorama.vtp' if panorama else 'surface.vtp' if representation else 'observations.vtt'
    if panorama:
        generate = ['--make-panorama',str(inputs/filename)]
    elif representation:
        generate = ['--make-surface',str(inputs/filename)]
    else:
        generate = ['--make-inputs',str(inputs)]
    subprocess.run([str(native),str(fixture),*generate],check=True,timeout=45)
    with api, httpx.Client(base_url=api.base+'/api/v1',trust_env=False,timeout=30) as client:
        api.create_case(PANORAMA_CASE if panorama else DISPLAY_CASE if representation else CASE)
        client.headers['Authorization'] = 'Bearer '+api.token
        original = (inputs/filename).read_bytes()
        uploaded = client.post(api.prefix+'/upload',params={'path':'D-post/'+filename},
                               files={'file':(filename,original,'application/octet-stream')})
        uploaded.raise_for_status()
        subprocess.run([str(edge_python),'-B','-X','utf8',__file__,'--edge'],check=True,timeout=210,
                       input=json.dumps({'evidence':str(api.root),'url':api.base,'auth':api.token,
                                         'representation':representation,'panorama':panorama}),text=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--edge']:
        browser_test(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        for key in ('web','native','edge-python'):
            parser.add_argument('--'+key,type=Path,required=True)
        display = parser.add_mutually_exclusive_group()
        display.add_argument('--representation',action='store_true')
        display.add_argument('--panorama',action='store_true')
        arguments = parser.parse_args()
        main(*(path.resolve(strict=True) for path in (arguments.web,arguments.native,arguments.edge_python)),
             representation=arguments.representation,panorama=arguments.panorama)
