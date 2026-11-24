"""Render live subscription and quota data in Edge at desktop and mobile sizes."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from testsupport import IsolatedServer, SERVER


def render(data):
    from playwright.sync_api import expect, sync_playwright

    output = Path(data['evidence'])
    observed = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        for width, height in ((1440, 900), (390, 844)):
            page = browser.new_page(viewport={'width': width, 'height': height}, timezone_id='Asia/Shanghai')
            page.add_init_script('localStorage.setItem("vf_token", '+json.dumps(data['token'])+');')
            page.goto(data['url'] + '/', wait_until='domcontentloaded')
            if width == 390:
                page.get_by_label('工作面板').select_option('settings')
            else:
                page.locator('.dv-tab').filter(has_text='设置').first.click(timeout=30000)
            quota = page.get_by_test_id('effective-quotas')
            expect(quota).to_contain_text('今日 LLM 请求', timeout=30000)
            expect(quota).to_contain_text('并发作业')
            expect(quota).to_contain_text('并发可视化会话')
            expect(quota).to_contain_text('存储（MiB）')
            expect(quota).to_contain_text('2 / 500')
            expect(quota).to_contain_text('08:00 重置')
            expect(page.locator('#quota-heading')).to_contain_text('免费版')
            details = quota.evaluate('(node) => ({width: node.clientWidth, scroll: node.scrollWidth, '
                                     'tiles: [...node.children].map(x => [x.clientWidth, x.scrollWidth])})')
            assert details['scroll'] <= details['width'] + 1, details
            assert all(scroll <= client + 1 for client, scroll in details['tiles']), details
            quota.screenshot(path=str(output/f'quota-{width}.png'))
            observed.append({'viewport': width, 'rendered': details, 'effective_plan': 'free', 'llm_today': 2})
            page.close()
        browser.close()
    (output/'browser-result.json').write_text(json.dumps({'passed': True, 'observations': observed,
                                                          'model_requests': 0}, indent=2), encoding='utf8')


def verify(dist, browser_python):
    dist = dist.resolve(strict=True)
    assert (dist/'index.html').is_file()
    api = IsolatedServer('ff-j08-quota-view', app='quota_view_fixture:app')
    (api.root/'quota_view_fixture.py').write_text(
        'from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf8')
    with api:
        api.create_case('Quota display acceptance')
        user = api.call('GET', '/auth/me')['id']
        org = api.call('GET', '/org/mine')['org']['id']
        plans = api.call('GET', '/org/plans')
        assert any(plan['code'] == 'pro' for plan in plans)
        today = datetime.now(timezone.utc).replace(tzinfo=None)
        with sqlite3.connect(api.root/'test.sqlite') as db:
            db.execute("update subscriptions set status='superseded' where org_id=?", (org,))
            db.execute('insert into subscriptions (org_id, plan_id, status, created_at, expires_at) '
                       "select ?, id, 'active', ?, ? from plans where code='pro'",
                       (org, today-timedelta(days=31), today-timedelta(days=1)))
            db.execute("insert into model_quota_days (user_id, day, kind, used) values (?, ?, 'translation', 2)",
                       (user, today.date()))
        subprocess.run([str(browser_python), str(Path(__file__).resolve()), '--browser'],
                       input=json.dumps({'url': api.base, 'token': api.token, 'evidence': str(api.root)}),
                       text=True, timeout=120, check=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    if sys.argv[1:] == ['--browser']:
        render(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        args = parser.parse_args()
        verify(args.dist, args.browser_python)
