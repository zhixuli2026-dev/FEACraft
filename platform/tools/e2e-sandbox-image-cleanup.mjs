// Production S3 sandbox, real browser transport, no routes or model substitutes.
async (page) => {
  const fixture = page.imageFixture;
  if (!fixture?.root || !fixture.web) throw new Error('Isolated S3 fixture required');
  const report = page.imageEvidence ??= { checks: [], llm_calls: 0, mock_llm_calls: 0 };
  const check = (name, ok) => { report.checks.push({ name, passed: !!ok }); if (!ok) throw new Error(name); };
  const dialog = page.getByRole('dialog', { name: '恢复沙箱文件' });
  const form = page.getByRole('region', { name: '清理原沙箱镜像', exact: true });
  const reopen = async () => {
    await dialog.getByRole('button', { name: '关闭恢复文件' }).click();
    await page.getByRole('button', { name: '恢复文件', exact: true }).click();
    await dialog.getByLabel('沙箱会话').selectOption(fixture.stopped_session);
    await form.waitFor();
  };
  if (page.imageStage === 'confirm') {
    if (!await dialog.isVisible()) throw new Error('Open actual recovery dialog after staging checks');
    await form.waitFor();
    check('confirmation_required_before_cleanup', await form.getByRole('button', { name: '确认删除原沙箱镜像' }).isDisabled());
    page.imagePosts = 0;
    page.imageListener = r => { if (r.method() === 'POST' && r.url().endsWith('/image-cleanup')) page.imagePosts++; };
    page.on('request', page.imageListener);
    await form.getByRole('checkbox').check();
    check('acknowledgement_enables_cleanup', await form.getByRole('button', { name: '确认删除原沙箱镜像' }).isEnabled());
    try {
      await page.context().setOffline(true);
      await form.getByRole('button', { name: '确认删除原沙箱镜像' }).click();
      await form.getByRole('alert').filter({ hasText: '镜像清理结果尚未确认' }).waitFor();
      check('offline_result_stays_uncertain', await form.getByRole('button', { name: '重试同一次镜像清理' }).isEnabled());
    } finally { await page.context().setOffline(false); }
    const count = page.imagePosts;
    await reopen();
    await form.getByRole('button', { name: '重试同一次镜像清理' }).waitFor();
    check('reopening_only_queries', page.imagePosts === count);
    await form.getByRole('button', { name: '重试同一次镜像清理' }).click();
    await form.getByRole('status').filter({ hasText: '原沙箱镜像已清理' }).waitFor({ timeout: 45000 });
    check('explicit_retry_completes_real_cleanup_once', page.imagePosts === count + 1);
    await reopen();
    await form.getByRole('status').filter({ hasText: '原沙箱镜像已清理' }).waitFor();
    check('completed_receipt_survives_reopening', page.imagePosts === count + 1);
    const remote = await page.evaluate(async f => {
      const headers = { Authorization: 'Bearer ' + localStorage.getItem('vf_token') };
      const result = await fetch('/api/v1/terminal/sessions/' + f.stopped_session + '/image-cleanup', { headers });
      const file = await fetch('/api/v1/cases/' + f.case_id + '/raw?path=Ref/retirement.txt', { headers });
      const archive = await fetch('/api/v1/terminal/sessions/' + f.stopped_session + '/recoveries/' + f.recovery_id + '/download', { headers });
      return { receipt: await result.json(), file: file.status, archive: archive.status,
        pending: localStorage.getItem('vf_terminal_image_cleanup:' + f.stopped_session) };
    }, fixture);
    check('actual_api_receipt_confirms_image_absent_and_snapshot_retained', remote.receipt.image_absent && remote.receipt.snapshot_retained && remote.receipt.metadata_retained);
    check('snapshot_download_survives_cleanup_and_project_unchanged', remote.archive === 200 && remote.file === 404);
    check('local_pending_cleared_after_receipt', remote.pending === null);
    report.receipt = remote.receipt;
    page.off('request', page.imageListener);
  } else if (page.imageStage === 'layout') {
    report.screenshots = [];
    for (const theme of ['light', 'dark']) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; document.body.dataset.theme = value; }, theme);
      for (const viewport of [{ width: 375, height: 812 }, { width: 812, height: 375 }]) {
        await page.setViewportSize(viewport);
        await form.scrollIntoViewIfNeeded();
        check('image_' + theme + '_' + viewport.width + '_no_overflow', await dialog.locator('.terminal-recovery').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
        const path = fixture.root + '/image-' + theme + '-' + viewport.width + '.png';
        await page.screenshot({ path }); report.screenshots.push(path);
      }
    }
    await page.setViewportSize({ width: 1280, height: 900 });
  } else throw new Error('Unknown browser stage');
  return report;
}
