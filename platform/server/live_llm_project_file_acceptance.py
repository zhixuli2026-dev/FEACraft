"""Exercise Project and Open Editors against the built UI and real renderers."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlencode, urlsplit
from zipfile import ZipFile

import httpx


TEXT_FILES = {
    'Ref/edit-a.txt': 'FIRST ORIGINAL',
    'Ref/edit-b.txt': 'SECOND ORIGINAL',
    'Ref/slow.txt': 'LATE FILE IN FIRST CASE',
    'Ref/方法.MD': '# 路由 Markdown\n\n保留原始编辑内容',
    'Ref/table.CSV': 'quantity,value\npressure,42\n',
    'Ref/reading.html': '<h1>ROUTED HTML</h1><a href="edit-a.txt">打开编辑文件</a>',
    'Ref/invalid.msh': 'The mesh is invalid.',
    'Ref/invalid.step': 'The shape is invalid.',
    'Ref/tetra.msh': '$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Nodes\n4\n1 0 0 0\n2 1 0 0\n3 0 1 0\n4 0 0 1\n$EndNodes\n$Elements\n1\n1 4 0 1 2 3 4\n$EndElements\n',
}


def pdf_bytes():
    command = b'BT /F1 16 Tf 20 120 Td (ROUTED PDF) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 160] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length %d >>\nstream\n%s\nendstream' % (len(command), command)]
    body = bytearray(b'%PDF-1.4\n')
    offsets = []
    for index, value in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(b'%d 0 obj\n%s\nendobj\n' % (index, value))
    start = len(body)
    xref = b''.join(b'%010d 00000 n \n' % offset for offset in offsets)
    return bytes(body) + b'xref\n0 6\n0000000000 65535 f \n' + xref + b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % start


def docx_bytes():
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         '<w:body><w:p><w:r><w:t>ROUTED DOCX</w:t></w:r></w:p></w:body></w:document>')
    return stream.getvalue()


def foam_fixture():
    fields = {
        'system/controlDict': ('dictionary', 'application icoFoam; startFrom startTime; startTime 0; stopAt endTime; endTime 1; deltaT 1; writeControl timeStep; writeInterval 1;'),
        'constant/polyMesh/points': ('vectorField', '4\n((0 0 0) (1 0 0) (0 1 0) (0 0 1))'),
        'constant/polyMesh/faces': ('faceList', '4\n(3(0 2 1) 3(0 1 3) 3(0 3 2) 3(1 2 3))'),
        'constant/polyMesh/owner': ('labelList', '4\n(0 0 0 0)'),
        'constant/polyMesh/neighbour': ('labelList', '0\n()'),
        'constant/polyMesh/boundary': ('polyBoundaryMesh', '1\n(walls { type wall; nFaces 4; startFace 0; })'),
        '0/p': ('volScalarField', 'dimensions [0 2 -2 0 0 0 0]; internalField uniform 1; boundaryField { walls { type zeroGradient; } }'),
    }
    result = {'preview.foam': ''}
    for path, (kind, text) in fields.items():
        name = path.rsplit('/', 1)[-1]
        result[path] = f'FoamFile {{ version 2.0; format ascii; class {kind}; object {name}; }}\n{text}\n'
    return result


async def browser_check(data):
    from playwright.async_api import async_playwright, expect

    root = Path(data['root'])
    async with async_playwright() as play:
        browser = await play.chromium.launch(channel=os.getenv('VIBEFLOW_TEST_BROWSER_CHANNEL') or None,
                                             args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context = await browser.new_context(viewport={'width':1500,'height':960}, locale='zh-CN',
                                            permissions=['clipboard-read','clipboard-write'])
        await context.add_init_script('if (location.origin === ' + json.dumps(data['base']) + ') localStorage.setItem("vf_token", ' + json.dumps(data['token']) + ');')
        page = await context.new_page()
        await page.goto(data['base'], wait_until='domcontentloaded')
        checks = []
        file_requests = []
        def record(response):
            url = urlsplit(response.url)
            if url.path.endswith(('/file','/raw','/msh-preview','/import-scene','/viz:sessions')):
                file_requests.append({'method':response.request.method,'endpoint':url.path,'path':parse_qs(url.query).get('path'),
                                      'status':response.status})
        page.on('response',record)
        stale = asyncio.Event()
        release = asyncio.Event()

        def row(path):
            return page.locator('[data-path=' + json.dumps(path, ensure_ascii=False) + ']')

        def tab(path):
            return page.locator('[data-editor-path=' + json.dumps(path, ensure_ascii=False) + ']')

        async def open_row(path):
            parts = path.split('/')
            for index in range(1, len(parts)):
                folder = row('/'.join(parts[:index]))
                await expect(folder).to_be_visible(timeout=30000)
                if await folder.get_attribute('aria-expanded') == 'false':
                    await folder.click()
            await row(path).click(timeout=30000)

        async def select_case(name):
            await page.locator('[data-case-name=' + json.dumps(name) + ']').click(timeout=30000)

        async def editor_has(path, content):
            await expect(tab(path)).to_have_attribute('data-active','true',timeout=20000)
            await page.wait_for_function('(text) => window.monaco?.editor.getModels().at(-1)?.getValue() === text',arg=content)

        async def route_delay(route):
            response = await route.fetch()
            assert response.ok
            stale.set()
            await asyncio.wait_for(release.wait(),35)
            await route.fulfill(response=response)

        async def viz_open(path, *, requested):
            endpoint = f'/cases/{data["case_id"]}/viz:sessions'
            async with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith(endpoint),timeout=95000) as started:
                await open_row(path)
            response = await started.value
            assert response.ok, (response.status, (await response.text())[:200])
            session = await response.json()
            assert requested(session), {key:session[key] for key in ('run_dir','file_path')}
            await expect(page.locator('iframe[title="可视化"]')).to_have_attribute('src',re.compile(f'/viz-proxy/{session["id"]}/'))
            frame = page.frame_locator('iframe[title="可视化"]')
            await expect(frame.locator('canvas').first).to_be_visible(timeout=50000)
            assert await frame.locator('canvas').first.evaluate('(canvas) => canvas.width > 0 && canvas.height > 0')
            result = await context.request.delete(data['base']+f'/api/v1/viz:sessions/{session["id"]}',
                                                  headers={'Authorization':'Bearer '+data['token']}, timeout=35000)
            assert result.ok, result.status

        try:
            await select_case('Project first')
            await open_row('Ref/edit-a.txt')
            await editor_has('Ref/edit-a.txt','FIRST ORIGINAL')
            await page.evaluate('() => window.monaco.editor.getModels().at(-1).setValue("第一份草稿")')
            page.once('dialog', lambda dialog:dialog.dismiss())
            await tab('Ref/edit-a.txt').get_by_role('button',name='关闭 Ref/edit-a.txt').click()
            await select_case('Project first')
            await editor_has('Ref/edit-a.txt','第一份草稿')
            await row('Ref/edit-b.txt').focus()
            await page.keyboard.press('Enter')
            await editor_has('Ref/edit-b.txt','SECOND ORIGINAL')
            await page.evaluate('() => window.monaco.editor.getModels().at(-1).setValue("第二份草稿")')
            await page.get_by_role('button',name='全部保存',exact=True).click()
            await expect(page.get_by_test_id('open-editors')).to_have_attribute('data-dirty-count','0',timeout=25000)
            await tab('Ref/edit-a.txt').get_by_role('button',name='edit-a.txt',exact=True).click()
            await editor_has('Ref/edit-a.txt','第一份草稿')
            await page.get_by_role('button',name='复制相对路径',exact=True).click()
            assert await page.evaluate('navigator.clipboard.readText()') == 'Ref/edit-a.txt'
            await page.get_by_role('button',name='复制路径',exact=True).click()
            await page.get_by_text('已复制存储路径',exact=True).wait_for()
            assert await page.evaluate('navigator.clipboard.readText()') == data['storage_prefix']+'/Ref/edit-a.txt'
            await page.get_by_role('button',name='加入聊天',exact=True).click()
            await expect(page.get_by_test_id('agent-input')).to_have_value('@Ref/edit-a.txt ')
            await open_row('Ref/edit-b.txt')
            await page.get_by_role('button',name='加入聊天',exact=True).click()
            await expect(page.get_by_test_id('agent-input')).to_have_value('@Ref/edit-a.txt @Ref/edit-b.txt ')
            await open_row('Ref/edit-a.txt')
            await tab('Ref/edit-b.txt').get_by_role('button',name='关闭 Ref/edit-b.txt').click()
            await expect(tab('Ref/edit-b.txt')).to_have_count(0)
            await page.get_by_role('button',name='全部关闭',exact=True).click()
            await expect(page.get_by_test_id('open-editors')).to_have_count(0)
            checks.append('editor keyboard open, same-case reselection, save, clipboard, chat attachments and close')

            slow = '**/file?path=Ref%2Fslow.txt'
            await page.route(slow, route_delay)
            await open_row('Ref/slow.txt')
            await asyncio.wait_for(stale.wait(),25)
            await open_row('Ref/edit-b.txt')
            await editor_has('Ref/edit-b.txt','第二份草稿')
            async with page.expect_response(lambda r:parse_qs(urlsplit(r.url).query).get('path') == ['Ref/slow.txt']):
                release.set()
            await page.unroute(slow, route_delay)
            await expect(tab('Ref/slow.txt')).to_have_count(0)
            checks.append('late editor result cannot create a tab or replace another active file')

            async with page.expect_response(lambda response: urlsplit(response.url).path.endswith('/file')
                                           and parse_qs(urlsplit(response.url).query).get('path') == ['Ref/方法.MD']) as markdown:
                await open_row('Ref/方法.MD')
            response = await markdown.value
            assert response.ok, (response.status,(await response.text())[:250])
            await page.get_by_test_id('markdown-preview-toggle').click()
            await expect(page.get_by_test_id('markdown-preview')).to_contain_text('路由 Markdown')
            await open_row('Ref/image.PNG')
            image = page.get_by_test_id('image-img')
            await expect(image).to_be_visible(timeout=25000)
            assert await image.evaluate('(img) => img.naturalWidth') == 1
            await open_row('Ref/table.CSV')
            await expect(page.get_by_test_id('csv-viewer')).to_contain_text('pressure',timeout=25000)
            await open_row('Ref/sample.PDF')
            await expect(page.get_by_test_id('pdf-page-scroll').locator('canvas')).to_be_visible(timeout=30000)
            await expect(page.get_by_test_id('pdf-page-scroll')).to_contain_text('ROUTED PDF',timeout=25000)
            await open_row('Ref/sample.DOCX')
            await expect(page.get_by_test_id('document-content')).to_contain_text('ROUTED DOCX',timeout=25000)
            await open_row('Ref/reading.html')
            await expect(page.get_by_test_id('document-content')).to_contain_text('ROUTED HTML',timeout=25000)
            await page.get_by_role('link',name='打开编辑文件').click()
            await editor_has('Ref/edit-a.txt','第一份草稿')
            checks.append('Markdown, PNG, CSV, rendered PDF, DOCX, HTML and internal document navigation')

            await open_row('Ref/shape.STP')
            await expect(page.get_by_test_id('geo-status')).to_contain_text('STEP 已载入',timeout=50000)
            await expect(page.locator('[data-object-name="shape.STP"]')).to_have_count(1)
            await open_row('Ref/invalid.step')
            await expect(page.get_by_test_id('geo-status')).to_contain_text('STEP 导入失败',timeout=50000)
            await expect(page.locator('[data-object-name="shape.STP"]')).to_have_count(1)
            await open_row('Ref/invalid.msh')
            failed = page.get_by_test_id('project-file-open-status')
            await expect(failed).to_contain_text('无法打开 invalid.msh',timeout=50000)
            await page.screenshot(path=root/'mesh-error.png')
            async with page.expect_response(lambda r: urlsplit(r.url).path.endswith('/file')
                                            and parse_qs(urlsplit(r.url).query).get('path') == ['Ref/invalid.msh'],
                                            timeout=50000) as original:
                await failed.get_by_role('button',name='打开原文件').click()
            response = await original.value
            assert response.ok, (response.status, (await response.text())[:250])
            await expect(tab('Ref/invalid.msh')).to_have_attribute('data-active','true')
            checks.append('native STEP success and failure, Gmsh error recovery and original-file access')

            stale.clear();release.clear()
            delayed = '**/geometry/msh-preview'
            await page.route(delayed, route_delay)
            await open_row('Ref/tetra.msh')
            await asyncio.wait_for(stale.wait(),45)
            await select_case('Project second')
            await open_row('Ref/other.txt')
            await editor_has('Ref/other.txt','SECOND CASE ACTIVE')
            async with page.expect_response(lambda r:r.url.endswith('/geometry/msh-preview')):
                release.set()
            await page.unroute(delayed, route_delay)
            await editor_has('Ref/other.txt','SECOND CASE ACTIVE')
            assert await page.locator('iframe[title="可视化"]').count() == 0
            checks.append('late Gmsh conversion cannot activate visualization in a different case')

            if data['render']:
                await select_case('Project first')
                await viz_open('Ref/tetra.msh', requested=lambda r:r['file_path'].endswith('/preview.vtu'))
                await viz_open('Ref/direct.VTU', requested=lambda r:r['file_path']=='Ref/direct.VTU')
                await viz_open('preview.foam', requested=lambda r:r['run_dir']=='.' and r['file_path'] is None)
                checks.append('actual trame canvas for Gmsh, VTK and root OpenFOAM')

            viewer = await browser.new_context(viewport={'width':1300,'height':900}, locale='zh-CN',
                                               permissions=['clipboard-read','clipboard-write'])
            await viewer.add_init_script('if (location.origin === ' + json.dumps(data['base']) + ') localStorage.setItem("vf_token", ' + json.dumps(data['viewer_token']) + ');')
            page = await viewer.new_page()
            await page.goto(data['base'], wait_until='domcontentloaded')
            await select_case('Project first')
            await open_row('Ref/edit-a.txt')
            await editor_has('Ref/edit-a.txt','第一份草稿')
            await page.get_by_role('button',name='复制路径',exact=True).click()
            await page.get_by_text('已复制存储路径',exact=True).wait_for()
            assert await page.evaluate('navigator.clipboard.readText()') == data['storage_prefix']+'/Ref/edit-a.txt'
            checks.append('shared viewer copies the actual owner storage path')
        except Exception:
            await page.screenshot(path=root/'project-file-failure.png')
            raise
        finally:
            release.set()
            (root/'project-file-browser.json').write_text(json.dumps({'browser':browser.version,'checks':checks,
                'file_requests':file_requests},indent=2),encoding='utf8')
            await browser.close()


def scenario(api, **_):
    import cadquery as cq
    import meshio
    from live_llm_job_repeat_acceptance import put, write

    # The common model-readback runner creates its own case first.
    first = api.create_case('Project first')
    workspace = api.call('GET','/workspaces')[0]['id']
    other = write(api,'POST',f'/workspaces/{workspace}/cases',{'name':'Project second'})
    write(api,'PUT',f'/cases/{other["id"]}/file',{'path':'Ref/other.txt','content':'SECOND CASE ACTIVE'})
    render = os.getenv('VIBEFLOW_TEST_PROJECT_RENDER') == '1'
    for path, contents in {**TEXT_FILES, **(foam_fixture() if render else {})}.items():
        put(api,path,contents)
    payload = api.root/'shape.stp'
    cq.exporters.export(cq.Workplane('XY').box(1,2,3),str(payload),exportType='STEP')
    put(api,'Ref/shape.STP',payload.read_text(encoding='utf8'))
    if render:
        vtu = api.root/'direct.vtu'
        meshio.write_points_cells(vtu,[[0,0,0],[1,0,0],[0,1,0],[0,0,1]], [('tetra',[[0,1,2,3]])],binary=False)
        put(api,'Ref/direct.VTU',vtu.read_text(encoding='utf8'))

    samples = {'image.PNG': ('image/png',base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jG1sAAAAASUVORK5CYII=')),
               'sample.PDF': ('application/pdf',pdf_bytes()),
               'sample.DOCX': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document',docx_bytes())}
    headers = {'Authorization':'Bearer '+api.token}
    with httpx.Client(base_url=api.base+'/api/v1',headers=headers,trust_env=False,timeout=40) as client:
        for name, (content_type, contents) in samples.items():
            response = client.post(api.prefix+'/upload',params={'path':'Ref/'+name},files={'file':(name,contents,content_type)})
            assert response.is_success, (name,response.status_code,response.text[:250])
    case = api.call('GET',api.prefix)
    assert first['id'] == case['id']
    outsider = api.call('POST','/auth/register',{'email':'project-files-outsider@example.com','name':'Project outsider',
                                               'password':'isolated-project-outsider-123'})
    other_headers = {'Authorization':'Bearer '+outsider['token']}
    api.call('GET',api.prefix,extra_headers=other_headers,expect=404)
    api.call('GET',api.prefix+'/file?'+urlencode({'path':'Ref/edit-a.txt'}),extra_headers=other_headers,expect=404)
    api.call('GET',api.prefix+'/geometry/import-scene?'+urlencode({'path':'Ref/shape.STP'}),extra_headers=other_headers,expect=404)
    org = api.call('GET','/org/mine')['org']['id']
    api.call('POST',f'/org/{org}/members',{'email':'project-files-outsider@example.com','role':'viewer'},expect=201)
    api.call('POST',f'/org/{org}/invitation:accept',{},extra_headers=other_headers)
    api.call('PUT',f'/workspaces/{workspace}/sharing',{'org_id':org,'role':'viewer','enabled':True})
    assert api.call('GET',api.prefix,extra_headers=other_headers)['storage_prefix'] == case['storage_prefix']
    api.call('PUT',api.prefix+'/file',{'path':'Ref/edit-a.txt','content':'forbidden'},extra_headers=other_headers,expect=404)
    args = {'root':str(api.root),'base':api.base,'token':api.token,'storage_prefix':case['storage_prefix'],
            'case_id':case['id'],'render':render,'viewer_token':outsider['token']}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],str(Path(__file__).resolve()),'--browser'],
                   input=json.dumps(args),text=True,timeout=330,check=True)
    for path, contents in [('Ref/edit-a.txt','第一份草稿'),('Ref/edit-b.txt','第二份草稿')]:
        assert api.call('GET',api.prefix+'/file?'+urlencode({'path':path}))['content'] == contents
    if render:
        root_markers = {item['path'] for item in api.call('GET',api.prefix+'/tree')['entries']
                        if '/' not in item['path'] and item['path'].lower().endswith('.foam')}
        assert root_markers == {'preview.foam'}, root_markers
    report = {'browser':json.loads((api.root/'project-file-browser.json').read_text()),
              'persisted_drafts':2,'foreign_account_denied':True,'native_viz':render}
    (api.root/'project-file-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser_check(json.load(sys.stdin)))
    else:
        from live_llm_job_repeat_acceptance import main

        bundle = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
        assert (bundle/'index.html').is_file()
        fixture = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='+repr(str(bundle))+', html=True))\n'
        asyncio.run(main(selected_override=scenario,fixture_override=fixture,store_volume_max=6))
