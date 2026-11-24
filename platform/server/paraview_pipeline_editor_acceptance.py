"""Build and execute a branched native ParaView pipeline through the Edge workbench."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode


CASE = 'Native ParaView node editor'


def browser_test(data):
    from playwright.sync_api import expect, sync_playwright

    root = Path(data['root'])
    receipt = {'passed':False,'actual_model_calls':0,'mock_llm_calls':0}
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
            catalog.get_by_label('ParaView 插件',exact=True).select_option('NodeEditor')
            catalog.get_by_role('button',name='打开管线编辑器',exact=True).click()
            editor = catalog.get_by_role('region',name='管线编辑器',exact=True)
            workspace = editor.get_by_role('list',name='已连接的管线节点',exact=True)
            choose = editor.get_by_label('新节点',exact=True)
            add = editor.get_by_role('button',name='添加节点',exact=True)

            def node(operator, count):
                choose.select_option(operator)
                add.click()
                expect(workspace.get_by_role('listitem')).to_have_count(count,timeout=30000)

            node('sources/SphereSource',1)
            editor.locator('label[title="ThetaResolution"] input').fill('20')
            editor.locator('label[title="PhiResolution"] input').fill('24')
            node('filters/TriangleFilter',2)
            node('sources/ConeSource',3)
            editor.locator('label[title="Resolution"] input').fill('12')
            node('filters/AppendPolyData',4)
            editor.locator('.vf-pv-node-properties select[multiple]').select_option(['node_2','node_3'])
            run = editor.get_by_role('button',name='运行管线',exact=True)
            with page.expect_response(lambda reply: reply.ok and reply.request.method=='POST'
                                      and reply.url.endswith('/paraview/pipeline')) as sent:
                run.click()
            request = sent.value.request.post_data_json['nodes']
            assert request[1]['inputs'] == {'Input':['node_1']}
            assert list(request[-1]['inputs'].values()) == [['node_2','node_3']]
            assert request[0]['properties']['ThetaResolution'] == 20
            job = sent.value.json()
            expect(editor.get_by_role('button',name='查看管线结果',exact=True)).to_be_visible(timeout=120000)
            endpoint = data['base']+'/api/v1/cases/'+str(data['case_id'])+'/raw?'
            headers = {'Authorization':'Bearer '+data['auth']}
            report = page.request.get(endpoint+urlencode({'path':job['report_path']}),headers=headers).json()
            sphere,triangle,cone,joined = report['nodes']
            assert (sphere['points'],sphere['cells']) == (442,880)
            assert (triangle['points'],triangle['cells']) == (sphere['points'],sphere['cells'])
            assert joined['points'] == triangle['points']+cone['points']
            assert joined['cells'] == triangle['cells']+cone['cells']
            native_state = page.request.get(endpoint+urlencode({'path':report['state']}),headers=headers)
            assert native_state.ok and b'ServerManagerState' in native_state.body()
            snapshot, flipbook = verify_tools(page,catalog,editor,run,endpoint,headers,expect)
            for width in (1440,390):
                page.set_viewport_size({'width':width,'height':900})
                run.scroll_into_view_if_needed()
                assert editor.evaluate('(node) => node.scrollWidth <= node.clientWidth + 2')
                page.screenshot(path=str(root/f'pipeline-editor-{width}.png'))
            streaming = verify_particles(page,catalog,root,endpoint,headers,expect)
            receipt.update(passed=True,job_id=job['job_id'],widths=[1440,390],nodes=report['nodes'],
                           snapshot=snapshot,flipbook=flipbook,streaming=streaming)
        finally:
            if not receipt['passed']:
                page.screenshot(path=str(root/'pipeline-editor-failed.png'))
            (root/'pipeline-editor.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf8')
            browser.close()


def verify_tools(page, catalog, editor, run, endpoint, headers, expect):
    def submit():
        with page.expect_response(lambda reply: reply.ok and reply.request.method=='POST'
                                  and reply.url.endswith('/paraview/pipeline')) as queued:
            run.click()
        return queued.value.json()

    def read(path):
        response = page.request.get(endpoint+urlencode({'path':path}),headers=headers)
        assert response.ok
        return response.body()

    plugin = catalog.get_by_label('ParaView 插件',exact=True)
    plugin.select_option('SaveStateAndScreenshot')
    catalog.get_by_role('button',name='保存状态与截图',exact=True).click()
    expect(editor.get_by_label('输出',exact=True)).to_have_value('snapshot')
    snapshot_job = submit()
    expect(editor.get_by_role('img',name='ParaView 管线截图',exact=True)).to_be_visible(timeout=120000)
    snapshot = json.loads(read(snapshot_job['report_path']))
    hashes = {}
    for label,path in (('下载状态',snapshot['state']),('下载截图',snapshot['inspection']['image'])):
        with page.expect_download() as download:
            editor.get_by_role('button',name=label,exact=True).click()
        data = Path(download.value.path()).read_bytes()
        assert data == read(path)
        hashes[Path(path).name] = hashlib.sha256(data).hexdigest()

    plugin.select_option('FlipBookPlugin')
    catalog.get_by_role('button',name='对照播放',exact=True).click()
    expect(editor.get_by_label('输出',exact=True)).to_have_value('flipbook')
    editor.get_by_label('对照节点',exact=True).select_option(['node_1','node_3'])
    editor.get_by_label('每秒帧数',exact=True).fill('1.5')
    flipbook_job = submit()
    comparison = editor.get_by_role('region',name='ParaView 对照播放',exact=True)
    expect(comparison.get_by_role('img',name='ParaView 动画帧 1',exact=True)).to_be_visible(timeout=120000)
    comparison.press('Space')
    expect(comparison.get_by_role('img',name='ParaView 动画帧 2',exact=True)).to_be_visible()
    comparison.get_by_role('button',name='播放动画',exact=True).click()
    expect(comparison.get_by_role('img',name='ParaView 动画帧 1',exact=True)).to_be_visible(timeout=15000)
    comparison.get_by_role('button',name='暂停动画',exact=True).click()
    animation = json.loads(read(flipbook_job['report_path']))['animation']
    assert animation['fps'] == 1.5 and animation['flipbook'] == ['node_1','node_3']
    assert [(frame['node'],frame['points']) for frame in animation['frames']] == [('node_1',442),('node_3',13)]
    frame_hashes = [hashlib.sha256(read(frame['path'])).hexdigest() for frame in animation['frames']]
    assert len(set(frame_hashes)) == 2
    return {'job_id':snapshot_job['job_id'],'hashes':hashes},{'job_id':flipbook_job['job_id'],'frame_sha256':frame_hashes}


def verify_particles(page, catalog, root, endpoint, headers, expect):
    catalog.get_by_label('ParaView 插件',exact=True).select_option('StreamingParticles')
    catalog.get_by_label('插件条目',exact=True).select_option('sources/RandomPoints')
    form = catalog.locator('form').filter(has=page.get_by_role('checkbox',name='按块载入预览',exact=True))
    for name,value in (('NumberOfLevels','2'),('PointsPerBlock','16'),('Seed','19')):
        form.locator(f'label[title="{name}"] input').fill(value)
    form.get_by_role('checkbox',name='按块载入预览',exact=True).check()
    with page.expect_response(lambda reply: reply.ok and reply.request.method=='POST'
                              and reply.url.endswith('/paraview/pipeline')) as queued:
        form.get_by_role('button',name='应用并生成结果',exact=True).click()
    assert queued.value.request.post_data_json['animation']['streaming'] == 16
    job = queued.value.json()
    preview = form.get_by_role('region',name='ParaView 粒子分块预览',exact=True)
    first = preview.get_by_role('img',name='ParaView 动画帧 1',exact=True)
    expect(first).to_be_visible(timeout=120000)
    expect(preview.locator('output')).to_contain_text('16 个点')
    slider = preview.get_by_role('slider',name='动画帧进度',exact=True)
    slider.press('End')
    last = preview.get_by_role('img',name='ParaView 动画帧 9',exact=True)
    expect(last).to_be_visible()
    expect(preview.locator('output')).to_contain_text('144 个点')
    report = page.request.get(endpoint+urlencode({'path':job['report_path']}),headers=headers).json()
    assert [frame['points'] for frame in report['animation']['frames']] == [16*index for index in range(1,10)]
    for width in (1440,390):
        page.set_viewport_size({'width':width,'height':900})
        last.scroll_into_view_if_needed()
        assert form.evaluate('(node) => node.scrollWidth <= node.clientWidth + 2')
        page.screenshot(path=str(root/f'streaming-{width}.png'))
    return {'job_id':job['job_id'],'frames':9,'first_points':16,'last_points':144}


def main(web, browser_python):
    from testsupport import IsolatedServer

    api = IsolatedServer('ff-d20-node-editor',app='pipeline_editor_fixture:app')
    print('NODE_EDITOR_EVIDENCE '+str(api.root),flush=True)
    (api.root/'pipeline_editor_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/",StaticFiles(directory={str(web)!r},html=True))\n',encoding='utf8')
    with api:
        api.create_case(CASE)
        subprocess.run([str(browser_python),'-B','-X','utf8',__file__,'--edge'],check=True,timeout=270,
                       input=json.dumps({'root':str(api.root),'base':api.base,'case_id':api.case_id,'auth':api.token}),text=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--edge']:
        browser_test(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--web',type=Path,required=True)
        parser.add_argument('--browser-python',type=Path,required=True)
        options = parser.parse_args()
        main(options.web.resolve(strict=True),options.browser_python.resolve(strict=True))
