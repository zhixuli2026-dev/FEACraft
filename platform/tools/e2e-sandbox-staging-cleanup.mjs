// Production S3 sandbox, real browser transport, no routes or model substitutes.
async (page) => {
  const fixture = page.stagingFixture;
  if (!fixture?.root || !fixture.web) throw new Error('Isolated S3 fixture required');
  const report = page.stagingEvidence ??= { checks: [], llm_calls: 0, mock_llm_calls: 0 };
  const check = (name, ok) => { report.checks.push({ name, passed: !!ok }); if (!ok) throw new Error(name); };
  const dialog = page.getByRole('dialog', { name: '恢复沙箱文件' });
  const form = page.getByRole('region', { name: '清理旧会话暂存', exact: true });
  const reopen = async () => {
    await dialog.getByRole('button', { name: '关闭恢复文件' }).click();
    await page.getByRole('button', { name: '恢复文件', exact: true }).click();
    await dialog.getByLabel('沙箱会话').selectOption(fixture.stopped_session);
    await form.waitFor();
  };
  if (page.stagingStage === 'confirm') {
    await page.goto(fixture.web);
    await page.getByRole('textbox', { name: '邮箱' }).fill(fixture.email);
    await page.getByRole('textbox', { name: '密码' }).fill('isolated-test-123');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByText(fixture.case_name, { exact: true }).click();
    await page.getByText('终端', { exact: true }).click();
    await page.getByRole('button', { name: '恢复文件', exact: true }).click();
    await dialog.getByLabel('沙箱会话').selectOption(fixture.stopped_session);
    await form.waitFor();
    check('confirmation_required_before_cleanup', await form.getByRole('button', { name: '确认清理下载暂存' }).isDisabled());
    page.stagingPosts = 0;
    page.stagingListener = r => { if (r.method() === 'POST' && r.url().endsWith('/staging-cleanup')) page.stagingPosts++; };
    page.on('request', page.stagingListener);
    await form.getByRole('checkbox').check();
    check('acknowledgement_enables_cleanup', await form.getByRole('button', { name: '确认清理下载暂存' }).isEnabled());
    try {
      await page.context().setOffline(true);
      await form.getByRole('button', { name: '确认清理下载暂存' }).click();
      await form.getByRole('alert').filter({ hasText: '暂存清理结果尚未确认' }).waitFor();
      check('offline_result_stays_uncertain', await form.getByRole('button', { name: '重试同一次暂存清理' }).isEnabled());
    } finally { await page.context().setOffline(false); }
    const count = page.stagingPosts;
    await reopen();
    await form.getByRole('button', { name: '重试同一次暂存清理' }).waitFor();
    check('reopening_only_queries', page.stagingPosts === count);
    await form.getByRole('button', { name: '重试同一次暂存清理' }).click();
    await form.getByRole('status').filter({ hasText: '下载暂存已清理' }).waitFor({ timeout: 45000 });
    check('explicit_retry_completes_real_cleanup_once', page.stagingPosts === count + 1);
    await reopen();
    await form.getByRole('status').filter({ hasText: '下载暂存已清理' }).waitFor();
    check('completed_receipt_survives_reopening', page.stagingPosts === count + 1);
    const remote = await page.evaluate(async f => {
      const headers = { Authorization: 'Bearer ' + localStorage.getItem('vf_token') };
      const result = await fetch('/api/v1/terminal/sessions/' + f.stopped_session + '/staging-cleanup', { headers });
      const file = await fetch('/api/v1/cases/' + f.case_id + '/raw?path=Ref/retirement.txt', { headers });
      const archive = await fetch('/api/v1/terminal/sessions/' + f.stopped_session + '/recoveries/' + f.recovery_id + '/download', { headers });
      return { receipt: await result.json(), file: file.status, archive: archive.status,
        pending: localStorage.getItem('vf_terminal_staging_cleanup:' + f.stopped_session) };
    }, fixture);
    check('actual_api_receipt_confirms_only_original_cache_removed', remote.receipt.original_staging_removed && !remote.receipt.image_removed);
    check('snapshot_download_survives_cleanup_and_project_unchanged', remote.archive === 200 && remote.file === 404);
    check('local_pending_cleared_after_receipt', remote.pending === null);
    report.receipt = remote.receipt;
    page.off('request', page.stagingListener);
  } else if (page.stagingStage === 'layout') {
    report.screenshots = [];
    for (const theme of ['light', 'dark']) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; document.body.dataset.theme = value; }, theme);
      for (const viewport of [{ width: 375, height: 812 }, { width: 812, height: 375 }]) {
        await page.setViewportSize(viewport);
        await form.scrollIntoViewIfNeeded();
        check('staging_' + theme + '_' + viewport.width + '_no_overflow', await dialog.locator('.terminal-recovery').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
        const path = fixture.root + '/staging-' + theme + '-' + viewport.width + '.png';
        await page.screenshot({ path }); report.screenshots.push(path);
      }
    }
    await page.setViewportSize({ width: 1280, height: 900 });
  } else throw new Error('Unknown browser stage');
  return report;
}
