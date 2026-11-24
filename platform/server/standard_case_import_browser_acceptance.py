"""Import authored StandardCase archives from the browser and solve on real WSL."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, quote, urlsplit
import zipfile


WRAPPED = 'A25 imported cavity'
FLAT = 'FlatExternal'
ORIGINAL = 'A25 existing case'
NOTE = 'Ref/author-note.txt'
MARKER = 'External engineering review - archive marker 25'
BINARY = 'Ref/calibration.bin'
GEOMETRY = 'A-geo/geometry.py'
RUN = 'C-run/case-1'
RESULT = RUN + '/0.1/U'


def make_zip(path, files, root=None):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, payload in files.items():
            name = f'{root}/{relative}' if root else relative
            archive.writestr(name, payload)


def prepared_archives(api, source):
    from app.services.casedict import set_block
    from cae_fixtures import cavity_2d

    contents = set_block(source, 'caseMeta', {'caseName':'External cavity calibration'})
    files = {path:content.encode() for path, content in cavity_2d().items()}
    files.update({'0-caseDict/caseDict':contents.encode(), NOTE:(MARKER+'\n').encode(),
                  GEOMETRY:b'# Retain this author-maintained geometry entry point.\n',
                  BINARY:bytes(range(256))*7+b'\x00\xff'})
    archives = api.root/'archives'
    archives.mkdir()
    make_zip(archives/'WrappedExternal.zip', files, root='Field calibration')
    make_zip(archives/(FLAT+'.zip'), files)
    (archives/'Corrupt.zip').write_bytes(b'This file was saved without a ZIP central directory.\n')
    make_zip(archives/'MissingDict.zip', {NOTE:files[NOTE]})
    make_zip(archives/'MalformedDict.zip', {'0-caseDict/caseDict':b'caseMeta { unfinished {'})
    make_zip(archives/'ConflictingNames.zip', {'0-caseDict/caseDict':files['0-caseDict/caseDict'],
            NOTE:files[NOTE], 'Ref/Author-note.txt':b'An alternate filename is ambiguous.\n'})
    return files


async def browser(data):
    from playwright.async_api import async_playwright, expect

    root, base = Path(data['root']), data['base']
    report = {'passed':False, 'imports':[], 'rejections':[], 'model_requests':[]}
    upload_path = f'/api/v1/workspaces/{data["workspace_id"]}/cases:import'

    async with async_playwright() as play:
        chromium = await play.chromium.launch(channel=data['channel'])
        report['browser_version'] = chromium.version
        context = await chromium.new_context(locale='zh-CN', viewport={'width':1680,'height':1000},
            storage_state={'cookies':[], 'origins':[{'origin':base,
                'localStorage':[{'name':'vf_token','value':data['token']}]}]})
        page = await context.new_page()

        def model_request(request):
            parts = urlsplit(request.url)
            if request.method == 'POST' and (parts.hostname == 'api.deepseek.com'
                    or re.search(r'/(conversations/[^/]+/(messages|goal:run)|codex/.*/turns)$', parts.path)):
                report['model_requests'].append(parts.path)

        def case_button(name):
            return page.locator('[data-case-name='+json.dumps(name)+']')

        async def upload(filename, *, name='', status=200):
            await page.get_by_role('textbox', name='新算例名', exact=True).fill(name)
            async with page.expect_response(
                lambda r: r.request.method == 'POST' and urlsplit(r.url).path == upload_path
            ) as pending:
                async with page.expect_file_chooser() as selected:
                    await page.get_by_title('导入 StandardCase ZIP', exact=True).click()
                await (await selected.value).set_files(root/'archives'/filename)
            response = await pending.value
            payload = await response.json()
            assert response.status == status, (filename, response.status, payload)
            return payload

        async def selected_import(filename, name, typed_name=''):
            payload = await upload(filename, name=typed_name)
            assert payload['name'] == name and payload['dict_revision'] == 1, payload
            await expect(case_button(name)).to_have_attribute('aria-pressed','true')
            source = page.locator('[data-path='+json.dumps(NOTE)+']')
            await expect(source).to_be_visible()
            await expect(page.locator('[data-path="Field calibration"]')).to_have_count(0)
            async with page.expect_response(lambda r: r.request.method=='GET'
                and urlsplit(r.url).path==f'/api/v1/cases/{payload["id"]}/file'
                and parse_qs(urlsplit(r.url).query).get('path')==[NOTE]) as pending:
                await source.click()
            opened = await pending.value
            assert opened.status == 200 and (await opened.json())['content'] == MARKER+'\n'
            await expect(page.locator('[data-editor-path='+json.dumps(NOTE)+'][data-active="true"]')).to_be_visible()
            await expect(page.get_by_text(MARKER, exact=True)).to_be_visible()
            report['imports'].append({'archive':filename, 'case_id':payload['id'], 'name':name,
                'inspection_file':NOTE, 'files':payload['files'], 'dict_revision':payload['dict_revision']})

        page.on('request', model_request)
        try:
            await page.goto(base, wait_until='domcontentloaded')
            await case_button(ORIGINAL).click(timeout=20000)
            await selected_import('WrappedExternal.zip', WRAPPED, WRAPPED)
            await selected_import(FLAT+'.zip', FLAT)
            await page.screenshot(path=root/'import-owned-case.png')
            for filename, name, status, fragment in (
                ('Corrupt.zip','Corrupt package',400,'不是有效 ZIP 文件'),
                ('MissingDict.zip','Missing standard dictionary',400,'缺少 0-caseDict/caseDict'),
                ('MalformedDict.zip','Malformed standard dictionary',422,'caseDict 无法解析'),
                ('ConflictingNames.zip','Conflicting file names',400,'重复或大小写冲突'),
                ('WrappedExternal.zip',WRAPPED,409,'已存在'),
            ):
                response = await upload(filename, name=name, status=status)
                assert fragment in response['detail'], response
                await expect(page.locator('.vf-project-error')).to_contain_text(fragment)
                await expect(case_button(FLAT)).to_have_attribute('aria-pressed','true')
                report['rejections'].append({'archive':filename, 'status':status, 'message':response['detail']})
            assert not report['model_requests']
            report['passed'] = True
        finally:
            (root/'browser-import.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            await context.close()
            await chromium.close()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_file(api, case_id, path):
    return api.call('GET',f'/cases/{case_id}/raw?path='+quote(path,safe=''),raw=True)


def real_solve(api, case_id):
    case = f'/cases/{case_id}'
    shell = 'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd '+RUN+' && '
    outputs = []
    for kind, command in (('mesh','blockMesh'),('solve','icoFoam')):
        request = {'type':kind,'runtime':'wsl','command':shell+command}
        if kind == 'solve':
            request['run_dir'] = RUN
        job = api.call('POST',case+'/jobs',request)
        done = api.wait_job(job['id'],timeout=120)
        deadline = time.monotonic()+35
        while done.get('completion_pending'):
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Job {job["id"]} output publication did not settle')
            time.sleep(.1)
            done = api.call('GET',f'/jobs/{job["id"]}')
        transcript = api.call('GET',f'/jobs/{job["id"]}/log?limit=262144')
        log = transcript['chunk']
        (api.root/(command+'.log')).write_text(log,encoding='utf-8')
        assert transcript['available'] and not transcript['has_more']
        assert 'Version:  2406' in log and re.search(r'^Exec\s*:\s*'+command+r'\b',log,re.MULTILINE)
        assert '\nEnd\n' in log.replace('\r\n','\n')
        outputs.append({'id':job['id'], 'kind':kind,'command':command,'runtime':'wsl','status':done['status']})
    output = read_file(api,case_id,RESULT)
    assert b'volVectorField' in output and b'object' in output and len(output)>100
    return {'jobs':outputs, 'result':RESULT, 'size':len(output), 'sha256':digest(output)}


def verify(args):
    import httpx
    from app.services.process_identity_native import probe
    from testsupport import IsolatedServer, SERVER

    bundle = args.dist.resolve(strict=True)
    index = bundle/'index.html'
    script = bundle / re.search(r'\bsrc="/([^"]+\.js)"',index.read_text(encoding='utf-8')).group(1)
    api = IsolatedServer('standard-case-import-browser', app='standard_case_import_fixture:app')
    temporary = api.root/'temporary'
    temporary.mkdir()
    os.environ.update(TEMP=str(temporary), TMP=str(temporary))
    (api.root/'standard_case_import_fixture.py').write_text(
        'import os\nfrom fastapi import Depends\nfrom fastapi.staticfiles import StaticFiles\n'
        'from app.deps import get_current_user\nfrom app.main import app\n'
        'from app.services.process_identity_native import observe\n'
        '@app.get("/fixture/process")\ndef process(user=Depends(get_current_user)):\n'
        '    return observe(os.getpid())\n'
        '@app.post("/fixture/process:stop")\ndef stop(data:dict,user=Depends(get_current_user)):\n'
        '    assert data["pid"] == os.getpid()\n    os._exit(79)\n'
        'app.mount("/", StaticFiles(directory='+repr(str(bundle))+', html=True))\n',encoding='utf-8')
    report = {'passed':False, 'real_model_requests':0, 'mock_llm_calls':0,
              'bundle':{'index_sha256':digest(index.read_bytes()), 'main_sha256':digest(script.read_bytes())}}
    print('STANDARD_CASE_IMPORT_BROWSER '+str(api.root),flush=True)
    try:
        with api:
            original = api.create_case(ORIGINAL)
            process = api.call('GET','/fixture/process',api_prefix=False)
            try:
                original_dict = api.call('GET',api.prefix+'/caseDict')
                original_path = NOTE
                original_bytes = b'Existing owner source remains independent.\n'
                api.put(original_path,original_bytes.decode())
                files = prepared_archives(api,original_dict['content'])
                workspace_id = api.call('GET','/workspaces')[0]['id']
                launch = {'root':str(api.root),'base':api.base,'token':api.token,
                          'workspace_id':workspace_id,'channel':args.channel}
                subprocess.run([str(args.browser_python.resolve(strict=True)),'-B','-X','utf8',str(Path(__file__)),
                    '--browser'], input=json.dumps(launch),text=True,timeout=100,check=True)
                report['browser'] = json.loads((api.root/'browser-import.json').read_text(encoding='utf-8'))
                case_list = f'/workspaces/{workspace_id}/cases'
                accepted = api.call('GET',case_list)
                assert {item['name'] for item in accepted} == {ORIGINAL,WRAPPED,FLAT}
                assert len(accepted) == 3
                for result in report['browser']['imports']:
                    case_id = result['case_id']
                    visible = {item['path'] for item in api.call('GET',f'/cases/{case_id}/tree')['entries']}
                    assert files.keys() <= visible and RESULT not in visible
                    assert result['files'] == len(files)
                    assert api.call('GET',f'/cases/{case_id}/caseDict')['content'].encode()==files['0-caseDict/caseDict']
                    result['file_sha256'] = {}
                    for path, original_data in files.items():
                        actual = read_file(api,case_id,path)
                        assert actual == original_data, path
                        result['file_sha256'][path] = digest(actual)
                other = api.call('POST','/auth/register',{'name':'Other account','email':'other-archive@example.com',
                    'password':'isolated-test-123'})
                authorization = {'Authorization':'Bearer '+other['token']}
                with httpx.Client(trust_env=False,timeout=25) as client, (api.root/'archives'/'WrappedExternal.zip').open('rb') as archive:
                    denied = client.post(api.base+'/api/v1'+case_list+':import',headers=authorization,
                        files={'file':('WrappedExternal.zip',archive,'application/zip')},data={'name':'Foreign upload'})
                api.call('GET',f'/cases/{report["browser"]["imports"][0]["case_id"]}',expect=404,extra_headers=authorization)
                assert denied.status_code == 404 and api.call('GET',case_list) == accepted
                report['foreign_import_status'] = denied.status_code
                report['solver'] = real_solve(api,report['browser']['imports'][0]['case_id'])
                assert read_file(api,original['id'],original_path) == original_bytes
                assert api.call('GET',api.prefix+'/caseDict') == original_dict
                report['original_case_unchanged'] = True
            finally:
                with httpx.Client(trust_env=False,timeout=10) as client:
                    try:
                        client.post(api.base+'/fixture/process:stop',json={'pid':process['pid']},
                                    headers={'Authorization':'Bearer '+api.token})
                    except httpx.TransportError:
                        pass
                api.proc.wait(timeout=12)
        report['api_exit'] = probe(process)
        assert report['api_exit']['state'] == 'exited'
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1600]}
        raise
    finally:
        paths = (Path(__file__).resolve(),SERVER/'app/services/case_import.py',SERVER/'app/routers/workspaces.py',
                 SERVER.parent/'web/src/panels/TreePanel.tsx',SERVER.parent/'web/src/store.ts')
        report['source_sha256'] = {path.relative_to(SERVER.parent).as_posix():digest(path.read_bytes()) for path in paths}
        (api.root/'standard-case-import-browser.json').write_text(
            json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('STANDARD_CASE_IMPORT_BROWSER_OK '+str(api.root),flush=True)


if __name__ == '__main__':
    if '--browser' in sys.argv:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist',type=Path,required=True)
        parser.add_argument('--browser-python',type=Path,required=True)
        parser.add_argument('--channel',default='msedge')
        verify(parser.parse_args())
