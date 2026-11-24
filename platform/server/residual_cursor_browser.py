"""Read the real live chart and record its byte-cursor traffic in Chromium."""
import asyncio
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright, expect


async def verify(config):
    root = Path(config['output'])
    expect.set_options(timeout=30000)
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            context = await browser.new_context(viewport={'width':1440,'height':920})
            await context.add_init_script('if(location.origin==='+json.dumps(config['base'])+
                ')localStorage.setItem("vf_token",'+json.dumps(config['token'])+');')
            page = await context.new_page()
            page.set_default_timeout(30000)
            errors, live_reads, full_reads = [], [], []
            live_url = re.compile(rf'/api/v1/jobs/{config["job_id"]}/log/parse(?:\?|$)')

            def observe(response):
                if live_url.search(response.url):
                    query = parse_qs(urlparse(response.url).query)
                    live_reads.append({'offset':int(query.get('offset',['0'])[0]),
                        'fingerprint':query.get('fingerprint',[None])[0],'status':response.status})
                elif '/logs/parse?path=' in response.url:
                    full_reads.append(response.status)

            page.on('response',observe)
            page.on('pageerror',lambda error:errors.append(str(error)))
            await page.goto(config['base'])
            await page.locator('[data-case-name]').filter(has_text=config['case']).click()
            jobs = page.get_by_role('button',name='作业与日志',exact=True)
            if not await jobs.is_visible():
                await page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^作业$')).first.click()
            await jobs.click()
            row = page.get_by_role('button',name=re.compile(rf'^#{config["job_id"]}(?:\s|$)'))
            await row.click()
            await expect(row).to_have_attribute('aria-pressed','true')
            await page.get_by_role('button',name='残差曲线',exact=True).click()
            summary = page.get_by_test_id('residual-summary')
            chart = page.get_by_role('img',name='求解残差曲线')
            linear = page.get_by_role('button',name='线性轴',exact=True)
            await expect(summary.get_by_text('末时刻 t=0.1',exact=True)).to_be_visible()
            await expect(summary.get_by_text('残差记录 1',exact=True)).to_be_visible()
            assert not full_reads
            await linear.click()

            async def brief_outage(route):
                await route.abort('internetdisconnected')

            await page.route(live_url,brief_outage,times=1)
            error = page.get_by_role('alert').filter(has_text='无法读取日志')
            await expect(error).to_be_visible()
            await expect(chart).to_be_visible()
            await expect(linear).to_have_attribute('aria-pressed','true')

            (root/'second-sample').touch()
            await expect(summary.get_by_text('末时刻 t=0.2',exact=True)).to_be_visible()
            await expect(summary.get_by_text('残差记录 2',exact=True)).to_be_visible()
            await expect(error).not_to_be_visible()
            await expect(linear).to_have_attribute('aria-pressed','true')
            await expect(chart.locator('[data-field="Ux"][data-series="final"] polyline')).to_have_count(1)

            (root/'third-sample').touch()
            await expect(summary.get_by_text('残差记录 3',exact=True)).to_be_visible()
            await expect(summary.get_by_text('末时刻 t=0.2',exact=True)).to_be_visible()
            await expect(summary.get_by_text('时刻点 1',exact=True)).to_be_visible()
            await expect(summary.get_by_text('警告 1',exact=True)).to_be_visible()
            assert any(read['offset']>0 and re.fullmatch('[0-9a-f]{64}',read['fingerprint'] or '') for read in live_reads)
            await page.locator('.vf-residual-workbench').screenshot(path=str(root/'incremental-chart-desktop.png'))

            rewrite = await context.request.post(config['base']+f'/api/v1/testing/rewrite-job-log/{config["job_id"]}',
                headers={'Authorization':'Bearer '+config['token']})
            assert rewrite.ok, rewrite.status
            await expect(summary.get_by_text('末时刻 t=3.1',exact=True)).to_be_visible()
            await expect(summary.get_by_text('残差记录 1',exact=True)).to_be_visible()
            await expect(chart.locator('[data-field="Ux"]')).to_have_count(0)
            await expect(chart.locator('[data-field="changed"]')).to_have_count(2)
            conflict = next(index for index,read in enumerate(live_reads) if read['status']==409)
            assert live_reads[conflict]['offset']>0
            assert any(read['offset']==0 and read['status']==200 for read in live_reads[conflict+1:])

            (root/'finish-sample').touch()
            await expect(summary.get_by_text('末时刻 t=4',exact=True)).to_be_visible(timeout=45000)
            await expect(summary.get_by_text('正常结束',exact=True)).to_be_visible()
            assert full_reads and full_reads[-1]==200
            await page.set_viewport_size({'width':420,'height':850})
            nav = page.get_by_role('combobox',name='工作面板',exact=True)
            if not await chart.is_visible():
                await nav.select_option('jobs')
            await expect(chart).to_be_visible()
            await page.locator('.vf-residual-workbench').screenshot(path=str(root/'incremental-chart-mobile.png'))
            assert 360 < await chart.evaluate('element => element.getBoundingClientRect().width') <= 420
            stopped = len(live_reads)
            await asyncio.sleep(2.4)
            assert len(live_reads)==stopped and not errors, (live_reads,errors)
            return {'live_reads':live_reads,'terminal_reads':full_reads, 'separator':'CRLF',
                'pending_line_rejoined':True,'fallback_time_precedence':True,'axis_kept_during_append':True,
                'network_retry_preserved_chart':True,'rewritten_prefix_rebuilt_chart':True,
                'full_terminal_line_without_newline':True,'poll_stopped_after_terminal':True,'mobile_420':True}
        finally:
            await browser.close()


if __name__=='__main__':
    config = json.load(sys.stdin)
    result = asyncio.run(verify(config))
    (Path(config['output'])/'residual-cursor-browser.json').write_text(json.dumps(result,indent=2),encoding='utf8')
