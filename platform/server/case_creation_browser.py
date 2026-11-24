"""Exercise the shipped workbench with real HTTP mutations and delayed acknowledgements."""
import asyncio
import io
import json
from pathlib import Path
import sys
import zipfile

from playwright.async_api import async_playwright, expect


async def verify(data):
    expect.set_options(timeout=30000)
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            async def page_for(token, width):
                ctx = await browser.new_context(viewport={'width':width, 'height':900})
                await ctx.add_init_script('if(location.origin === '+json.dumps(data['base'])+
                    ')localStorage.setItem("vf_token",'+json.dumps(token)+');')
                page = await ctx.new_page()
                page.set_default_timeout(30000)
                await page.goto(data['base'])
                return page

            page = await page_for(data['owner'], 1500)
            failures = []
            page.on('pageerror', lambda error: failures.append(str(error)))
            async def record(response):
                named = response.url.endswith(('/cases', '/tree', '/cases:import'))
                if '/api/v1/' in response.url and named and (response.request.method == 'POST' or not response.ok):
                    body = await response.json()
                    with Path(data['output']).with_name('case-create-http.jsonl').open('a',encoding='utf8') as log:
                        log.write(json.dumps({'method':response.request.method,'path':response.url.split('/api/v1/',1)[-1],
                            'status':response.status,'detail':body.get('detail'),'id':body.get('id')})+'\n')
            page.on('response', record)
            spaces = page.get_by_role('combobox', name='当前工作区')
            case_input = page.get_by_role('textbox', name='新算例名', exact=True)
            create = page.get_by_role('button', name='创建算例', exact=True)
            async def submit_case(action, status=200):
                # A user may retry a pre-admission busy response without changing the draft.
                for _ in range(20):
                    async with page.expect_response(lambda r: r.url.endswith('/cases') and r.request.method=='POST') as event:
                        await action()
                    response = await event.value
                    body = await response.json()
                    detail = body.get('detail')
                    if response.status==409 and isinstance(detail,dict) and detail.get('code') in {'storage_quota_busy','validation_busy'}:
                        await expect(page.get_by_role('alert').filter(has_text=detail['message'])).to_be_visible()
                        await asyncio.sleep(.2)
                        continue
                    assert response.status==status,(response.status,detail)
                    return
                raise AssertionError('Case creation remained busy')
            await expect(spaces).to_have_value(str(data['ws']))
            await expect(create).to_be_disabled()
            await page.get_by_role('textbox', name='新工作区名').fill('浏览器工程')
            await page.get_by_role('button', name='创建工作区', exact=True).click()
            await expect(spaces.locator('option:checked')).to_have_text('浏览器工程')
            new_ws = await spaces.input_value()
            name = '换热器-α'
            await case_input.fill(name)
            await submit_case(lambda: case_input.press('Enter'))
            row = page.locator('[data-case-name="'+name+'"]')
            await expect(row).to_have_attribute('aria-pressed','true')
            await expect(case_input).to_have_value('')
            await case_input.fill(name)
            await submit_case(create.click,409)
            await expect(page.get_by_role('alert').filter(has_text='同名算例已存在')).to_be_visible()
            await expect(case_input).to_have_value(name)
            await page.screenshot(path=str(Path(data['output']).with_name('case-create-desktop.png')))

            # Hold a genuine POST response after the server committed, then navigate away.
            committed, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            posts = []
            async def pause(route):
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                response = await route.fetch()
                if response.status != 200:
                    await route.fulfill(response=response)
                    raise AssertionError(('delayed create',response.status,(await response.json()).get('detail')))
                posts.append((await response.json())['id'])
                committed.set()
                await asyncio.wait_for(release.wait(), 30)
                await route.fulfill(response=response)
                delivered.set()
            endpoint = f'**/api/v1/workspaces/{new_ws}/cases'
            await page.route(endpoint, pause)
            await case_input.fill('应答延迟的算例')
            await case_input.press('Enter')
            await asyncio.wait_for(committed.wait(),30)
            await expect(create).to_be_disabled()
            await spaces.select_option(str(data['ws']))
            await expect(page.locator('[data-case-name="原有算例"]')).to_be_visible()
            release.set()
            await asyncio.wait_for(delivered.wait(),30)
            await page.unroute(endpoint, pause)
            await expect(page.locator('[data-case-name="应答延迟的算例"]')).to_have_count(0)
            await spaces.select_option(new_ws)
            late = page.locator('[data-case-name="应答延迟的算例"]')
            await expect(late).to_have_count(1)
            await late.focus()
            await late.press('Enter')
            await expect(late).to_have_attribute('aria-pressed','true')
            await page.reload()
            await expect(late).to_have_attribute('aria-pressed','true')
            assert len(posts) == 1

            # Disconnect the read requests after the selection was saved. A retry
            # must reopen the same case and fetch its tree.
            tree_endpoint = f'**/api/v1/cases/{posts[0]}/tree'
            async def offline(route):
                if route.request.method == 'GET':
                    await route.abort('internetdisconnected')
                else:
                    await route.continue_()
            await page.route(endpoint, offline)
            await page.route(tree_endpoint, offline)
            await page.reload()
            list_error = page.get_by_role('alert').filter(has_text='算例列表加载失败')
            await expect(list_error).to_be_visible()
            await expect(create).to_be_disabled()
            await page.unroute(endpoint, offline)
            await list_error.get_by_role('button', name='重新加载').click()
            await expect(late).to_have_attribute('aria-pressed','true')
            tree_error = page.get_by_role('alert').filter(has_text='文件树加载失败')
            await expect(tree_error).to_be_visible()
            await page.unroute(tree_endpoint, offline)
            await tree_error.get_by_role('button', name='重新加载').click()
            await expect(tree_error).to_have_count(0)
            await expect(page.locator('[data-path="0-caseDict/caseDict"]')).to_be_visible()
            await expect(late).to_have_attribute('aria-pressed','true')

            # Exercise the published earlier POST shape with an actual newly written case.
            async def legacy(route):
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                response = await route.fetch()
                if response.status != 200:
                    await route.fulfill(response=response)
                    return
                body = await response.json()
                await route.fulfill(response=response,json={key:body[key] for key in ('id','name','workflow_state')})
            await page.route(endpoint, legacy)
            await case_input.fill('旧接口回执')
            await submit_case(create.click)
            await expect(page.locator('[data-case-name="旧接口回执"]')).to_have_attribute('aria-pressed','true')
            await page.unroute(endpoint, legacy)

            # The create and import controls share the same name input.
            import_input = page.get_by_test_id('case-import-file')
            zip_name = 'shared-form.zip'
            await case_input.fill('导入恢复')
            async def upload(content, expected):
                for _ in range(20):
                    async with page.expect_response(lambda r: r.url.endswith('/cases:import') and r.request.method=='POST') as event:
                        await import_input.set_input_files({'name':zip_name,'mimeType':'application/zip','buffer':content})
                    response = await event.value
                    body = await response.json()
                    detail = body.get('detail')
                    if response.status == 409 and isinstance(detail, dict) and detail.get('code') == 'storage_quota_busy':
                        await expect(import_input).to_be_enabled()
                        await asyncio.sleep(.2)
                        continue
                    assert response.status == expected, (response.status, detail)
                    return body
                raise AssertionError('Case import remained busy')

            await upload(b'invalid ZIP', 400)
            await expect(page.get_by_role('alert').filter(has_text='不是有效 ZIP 文件')).to_be_visible()
            await expect(case_input).to_have_value('导入恢复')
            source = await page.request.get(data['base']+f'/api/v1/cases/{posts[0]}/caseDict',
                                           headers={'Authorization':'Bearer '+data['owner']})
            assert source.ok
            canonical = (await source.json())['content']
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('0-caseDict/caseDict', canonical)
                bundle.writestr('Ref/import.txt', 'STANDARD_CASE_IMPORT')
            await upload(archive.getvalue(), 200)
            await expect(page.locator('[data-case-name="导入恢复"]')).to_have_attribute('aria-pressed','true')
            await expect(page.locator('[data-path="Ref/import.txt"]')).to_be_visible()
            await expect(case_input).to_have_value('')

            viewer = await page_for(data['member'], 1500)
            shared = viewer.get_by_role('combobox', name='当前工作区')
            await shared.select_option(str(data['ws']))
            readable = viewer.locator('[data-case-name="原有算例"]')
            await readable.click()
            await expect(readable).to_have_attribute('aria-pressed','true')
            await expect(viewer.get_by_role('button', name='创建算例',exact=True)).to_be_disabled()
            await expect(viewer.get_by_test_id('case-import-file')).to_be_disabled()
            await page.set_viewport_size({'width':420,'height':840})
            if not await spaces.is_visible():
                await page.get_by_role('button',name='选择算例',exact=True).click()
            await expect(spaces).to_be_visible()
            await case_input.fill('窄屏重试')
            await submit_case(create.click)
            chosen = page.locator('[data-case-name="窄屏重试"]')
            await expect(chosen).to_have_attribute('aria-pressed','true')
            await expect(case_input).to_have_value('')
            assert await case_input.evaluate('(input) => input.getBoundingClientRect().height') >= 44
            await chosen.scroll_into_view_if_needed()
            await page.screenshot(path=str(Path(data['output']).with_name('case-create-narrow.png')))
            assert not failures, failures
            return dict(new_workspace=int(new_ws), late_case=posts[0], duplicate_kept_name=True,
                late_response_left_navigation_intact=True, old_response_selectable=True,
                keyboard_and_reload=True, case_list_and_tree_retry=True, zip_import_retry=True,
                shared_read_only_controls=True, narrow_created=True)
        finally:
            await browser.close()


if __name__ == '__main__':
    settings = json.load(sys.stdin)
    Path(settings['output']).write_text(json.dumps(asyncio.run(verify(settings)),indent=2))
