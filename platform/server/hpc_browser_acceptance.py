"""Real Edge workbench against a live API and SSH scheduler, with receipt loss."""

import asyncio
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect


async def run(fixture):
    root = Path(fixture['root'])
    base = fixture['base']
    api = f'/api/v1/cases/{fixture["case_id"]}/remote-jobs'
    report = {'passed':False, 'actual_model_calls':0, 'mock_llm_calls':0,
              'page_errors':[], 'checks':[]}
    backend_calls = []
    provider_metadata = []
    lost = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel='msedge')
        report['browser_version'] = browser.version
        context = await browser.new_context(accept_downloads=True,locale='zh-CN',viewport={'width':1440,'height':980},
            storage_state={'cookies':[],'origins':[{'origin':base,'localStorage':[
                {'name':'vf_token','value':fixture['token']}]}]})
        page = await context.new_page()

        def observed(request):
            url = urlsplit(request.url)
            if url.hostname not in {'127.0.0.1','localhost'}:
                backend_calls.append(url.hostname)
            if request.method=='GET' and url.path=='/api/v1/llm/providers':
                provider_metadata.append(url.path)
            elif re.search(r'/llm/|/conversations/[^/]+/(messages|goal:run)$|/codex/.*/turns$',url.path):
                backend_calls.append(url.path)

        page.on('request', observed)
        page.on('pageerror', lambda error: report['page_errors'].append(str(error)[:400]))

        async def drop_first_receipt(route):
            if route.request.method != 'POST' or lost:
                await route.continue_()
                return
            response = await route.fetch()
            assert response.status == 202, await response.text()
            lost.append({'key':route.request.headers.get('idempotency-key'),'receipt':await response.json()})
            await route.abort('failed')

        async def open_remote(width):
            if width <= 640:
                await page.get_by_label('工作面板', exact=True).select_option('jobs')
            else:
                await page.locator('.dv-tab').filter(has_text=re.compile(r'^作业$')).click(timeout=20000)
            await page.get_by_role('button',name='远程集群',exact=True).click()
            await expect(page.get_by_role('region',name='远程集群作业')).to_be_visible()

        panel = page.get_by_role('region',name='远程集群作业')
        try:
            await page.goto(base,wait_until='domcontentloaded')
            await page.locator('[data-case-name="Actual Slurm OpenFOAM"]').click(timeout=20000)
            await open_remote(1440)
            await panel.get_by_text('新建远程作业',exact=True).click()
            await panel.get_by_label('脚本路径',exact=True).fill('browser.sh')
            await panel.get_by_label('成果路径（每行一个）',exact=True).fill('browser-result.txt')
            await panel.screenshot(path=str(root/'hpc-desktop-form.png'))
            await page.route(base+api,drop_first_receipt)
            await panel.get_by_role('button',name='提交到集群',exact=True).click()
            await expect(panel.get_by_text(re.compile(r'待确认.*browser.sh'))).to_be_visible(timeout=50000)
            await expect(panel.get_by_role('button',name='核对原提交',exact=True)).to_be_enabled(timeout=50000)
            assert len(lost)==1 and lost[0]['key']
            await page.reload(wait_until='domcontentloaded')
            if not await panel.is_visible():
                await page.locator('[data-case-name="Actual Slurm OpenFOAM"]').click(timeout=20000)
                await open_remote(1440)
            async with page.expect_response(lambda response: urlsplit(response.url).path==api
                    and response.request.method=='POST',timeout=45000) as pending:
                await panel.get_by_role('button',name='核对原提交',exact=True).click()
            accepted = await pending.value
            result = await accepted.json()
            assert accepted.status == 202 and accepted.request.headers['idempotency-key']==lost[0]['key']
            assert result['id']==lost[0]['receipt']['id'] and result['scheduler_id']==lost[0]['receipt']['scheduler_id']
            report['checks'].append('lost real response replays the same scheduler identity after browser reload')
            details = panel.get_by_role('article',name='远程作业状态')
            await expect(details.get_by_text(re.compile(r'^成功 ·'))).to_be_visible(timeout=65000)
            async with page.expect_download() as downloading:
                await details.get_by_role('button',name='下载 browser-result.txt',exact=True).click()
            download = await downloading.value
            copied = root/'browser-result.txt'
            await download.save_as(copied)
            assert copied.read_bytes()==b'VIBEFLOW_SCHEDULER_BROWSER\n'
            report['checks'].append('verified result downloads from actual scheduler archive')

            await page.set_viewport_size({'width':390,'height':844})
            await open_remote(390)
            await expect(details.get_by_role('button',name='下载 browser-result.txt',exact=True)).to_be_visible()
            box = await panel.bounding_box()
            assert box is not None and box['width']<=390
            assert await panel.evaluate('e => e.scrollWidth <= e.clientWidth + 1')
            await panel.screenshot(path=str(root/'hpc-mobile-results.png'))
            report['checks'].append('390 pixel workbench retains selected job and verified output without overflow')
            assert not report['page_errors'], report['page_errors']
            assert not backend_calls, backend_calls
            report['local_provider_metadata_reads'] = len(provider_metadata)
            report['passed'] = True
            report['job_id'] = result['id']
            report['scheduler_id']=result['scheduler_id']
        finally:
            if not report['passed']:
                await page.screenshot(path=str(root/'hpc-browser-failure.png'))
            (root/'hpc-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            await browser.close()


if __name__=='__main__':
    asyncio.run(run(json.load(sys.stdin)))
