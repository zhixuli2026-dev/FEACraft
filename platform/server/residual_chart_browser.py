"""Verify the production chart against actual isolated job logs in Chromium."""
import asyncio
import json
from pathlib import Path
import re
import sys

from PIL import Image
from playwright.async_api import async_playwright, expect


async def verify(config):
    root = Path(config['output'])
    expect.set_options(timeout=30000)
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            context = await browser.new_context(viewport={'width':1520,'height':950}, accept_downloads=True)
            await context.add_init_script('if(location.origin==='+json.dumps(config['base'])+
                ')localStorage.setItem("vf_token",'+json.dumps(config['token'])+');')
            page = await context.new_page()
            page.set_default_timeout(30000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(config['base'])
            case = page.locator('[data-case-name]').filter(has_text=config['case'])
            await case.click()
            jobs = page.get_by_role('button', name='作业与日志', exact=True)
            if not await jobs.is_visible():
                await page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^作业$')).first.click()
            await jobs.click()
            chart_button = page.get_by_role('button', name='残差曲线', exact=True)
            log_button = page.get_by_role('button', name='日志', exact=True)
            chart = page.get_by_role('img', name='求解残差曲线')
            summary = page.get_by_test_id('residual-summary')
            linear = page.get_by_role('button', name='线性轴', exact=True)
            log_axis = page.get_by_role('button', name='对数轴', exact=True)
            export = page.get_by_role('button', name='导出 PNG', exact=True)

            async def select(job, show_chart=True):
                selector = page.get_by_role('button',name=re.compile(rf'^#{job}(?:\s|$)'))
                await selector.click()
                await expect(selector).to_have_attribute('aria-pressed','true')
                if show_chart:
                    await chart_button.click()

            async def coords(locator):
                data = (await locator.get_attribute('points')).split()
                return [tuple(map(float,pair.split(','))) for pair in data]

            async def finite_svg():
                assert await chart.evaluate("""svg => [...svg.querySelectorAll('polyline,circle,line,text')].every(el => {
                  const keys = ['points','x','y','x1','y1','x2','y2','cx','cy'];
                  return keys.every(key => !el.hasAttribute(key) ||
                    el.getAttribute(key).split(/[ ,]+/).every(value => Number.isFinite(Number(value))));
                })""")

            normal_job = config['jobs']['normal']
            await select(normal_job)
            await expect(summary).to_contain_text('末时刻 t=0.2')
            await expect(summary).to_contain_text('Courant max 5.00e-1')
            await expect(summary).to_contain_text('耗时 2.0s')
            await expect(summary).to_contain_text('正常结束')
            await expect(chart.locator('polyline')).to_have_count(4)
            await expect(chart.locator('polyline[stroke-dasharray]')).to_have_count(2)
            await finite_svg()
            ux = chart.locator('[data-field="Ux"][data-series="final"] polyline')
            log_points = await coords(ux)
            assert log_points==[(60.0,94.7),(626.0,228.0)],log_points
            await chart.screenshot(path=str(root/'residual-log.png'))
            await linear.click()
            await expect(linear).to_have_attribute('aria-pressed','true')
            linear_points = await coords(ux)
            assert linear_points==[(60.0,208.0),(626.0,227.8)],linear_points
            await finite_svg()
            await log_axis.click()
            assert await coords(ux)==log_points
            async with page.expect_download() as downloading:
                await export.click()
            download = await downloading.value
            assert download.suggested_filename=='residual_t0.2.png'
            png = root/'residual-download.png'
            await download.save_as(str(png))
            with Image.open(png) as output:
                assert output.format=='PNG' and output.size==(1280,520)
                # A screenshot of only axes and a legend must not pass.
                colors = output.convert('RGB')
                assert sum(r > 65 and 120 < g < 205 and b > 216
                    for r,g,b in colors.crop((150,205,1180,442)).getdata()) > 200

            log_endpoint = '**/api/v1/cases/*/logs/parse?**'
            async def offline(route):
                await route.abort('internetdisconnected')
            await page.route(log_endpoint, offline)
            await log_button.click()
            await chart_button.click()
            failure = page.get_by_role('alert').filter(has_text='无法读取日志')
            await expect(failure).to_be_visible()
            await page.unroute(log_endpoint, offline)
            await failure.get_by_role('button',name='重新解析').click()
            await expect(chart).to_be_visible()

            async def legacy(route):
                response = await route.fetch()
                if response.status != 200:
                    await route.fulfill(response=response)
                    return
                body = await response.json()
                body.pop('residuals_detail')
                await route.fulfill(response=response, json=body)
            await page.route(log_endpoint, legacy)
            await log_button.click()
            await chart_button.click()
            await expect(chart.locator('polyline')).to_have_count(2)
            await expect(chart.locator('polyline[stroke-dasharray]')).to_have_count(0)
            await finite_svg()
            await page.unroute(log_endpoint, legacy)

            reached, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def delayed(route):
                if route.request.url.split('path=',1)[-1] != config['normal_encoded_path']:
                    await route.continue_()
                    return
                response = await route.fetch()
                assert response.status==200
                reached.set()
                await asyncio.wait_for(release.wait(),30)
                try:
                    await route.fulfill(response=response)
                finally:
                    delivered.set()
            await page.route(log_endpoint, delayed)
            await log_button.click()
            await chart_button.click()
            await asyncio.wait_for(reached.wait(),30)
            await select(config['jobs']['zero'])
            await expect(summary).to_contain_text('末时刻 t=0.7')
            await expect(summary).to_contain_text('警告 1')
            await expect(summary).to_contain_text('致命错误 1')
            await expect(summary).to_contain_text('未见 End 标记')
            release.set()
            await asyncio.wait_for(delivered.wait(),30)
            await page.unroute(log_endpoint, delayed)
            await expect(summary).to_contain_text('末时刻 t=0.7')
            await expect(page.get_by_text('没有可显示的正残差',exact=False)).to_be_visible()
            await expect(export).to_be_disabled()
            await linear.click()
            zero = chart.locator('[data-field="Ux"][data-series="final"] circle')
            await expect(zero).to_have_attribute('cy','228')
            await finite_svg()
            await page.reload()
            saved_job = page.get_by_role('button', name=re.compile(rf'^#{config["jobs"]["zero"]}(?:\s|$)'))
            await expect(saved_job).to_have_attribute('aria-pressed','true')

            long_job = config['jobs']['long']
            await select(long_job)
            await expect(summary).to_contain_text('残差记录 5000')
            await finite_svg()
            long_trace = chart.locator('[data-field="p"][data-series="final"] polyline')
            long_points = await coords(long_trace)
            assert len(long_points)<2300 and long_points[0][0]==60 and long_points[-1][0]==626
            assert min(y for _,y in long_points)==28 and max(y for _,y in long_points)==228
            await page.set_viewport_size({'width':420,'height':850})
            nav = page.get_by_role('combobox', name='工作面板', exact=True)
            if not await chart_button.is_visible():
                await nav.select_option('jobs')
            await expect(chart).to_be_visible()
            await chart.screenshot(path=str(root/'residual-narrow.png'))
            assert 360 < await chart.evaluate('el => el.getBoundingClientRect().width') <= 420
            assert await export.evaluate('button => button.getBoundingClientRect().height') >= 44
            assert not errors, errors
            return dict(normal=normal_job, log_points=log_points, linear_points=linear_points,
                long_drawn_points=len(long_points), long_input_points=5000,
                zero_linear=True, recovery_after_offline=True, stale_job_response_ignored=True,
                legacy_final_only=True, restored_job_selection=True, narrow_420=True,
                png_width=1280, png_height=520)
        finally:
            await browser.close()


if __name__=='__main__':
    data = json.load(sys.stdin)
    root = Path(data['output'])
    (root/'residual-browser.json').write_text(json.dumps(asyncio.run(verify(data)),indent=2))
