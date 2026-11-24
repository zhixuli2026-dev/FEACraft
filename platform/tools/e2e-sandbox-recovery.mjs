// Run via Playwright browser_run_code_unsafe(filename). Set page.recoveryFixture
// from sandbox_recovery_ui_fixture.py's ready.json and page.recoveryStage first.
// Uses the production frontend and real API/WSL/files only. No request interception,
// provider substitution or inference. The driver persists returned evidence.
async (page) => {
  const fixture = page.recoveryFixture;
  if (!fixture || !page.url().startsWith(fixture.web + '/')) throw new Error('Isolated recovery fixture required');
  const evidence = page.recoveryEvidence ??= { checks: [], llm_calls: 0, simulated_llm_calls: 0 };
  const check = (name, ok, detail) => {
    evidence.checks.push({ name, passed: !!ok, ...(detail === undefined ? {} : {detail}) });
    if (!ok) throw new Error(name);
  };
  const dialog = page.getByRole('dialog', {name:'恢复沙箱文件'});
  const open = async () => { await page.getByRole('button', {name:'恢复文件', exact:true}).click(); };
  const select = async id => {
    await dialog.getByLabel('沙箱会话').selectOption(id);
    await page.waitForFunction(() => {
      const el = document.querySelector('.terminal-recovery-condition');
      return el && el.getAttribute('aria-busy') === 'false';
    });
  };
  if (page.recoveryStage === 'create') {
    await select(fixture.live_session);
    if ((await dialog.innerText()).includes('会话状态正在变化')) {
      check('concurrent_record_change_refuses_creation', await dialog.getByRole('button', {name:'创建恢复快照'}).isDisabled());
      await dialog.getByRole('button', {name:'重新核验'}).click();
      await page.waitForFunction(() => document.querySelector('.terminal-recovery-condition')?.getAttribute('aria-busy') === 'false');
    }
    check('live_original_session_refuses_creation', await dialog.getByRole('button', {name:'创建恢复快照'}).isDisabled()
      && (await dialog.innerText()).includes('原会话仍在运行'));
    await page.keyboard.press('Escape');
    check('escape_closes_and_returns_focus', await dialog.count() === 0 && await page.getByRole('button', {name:'恢复文件',exact:true}).evaluate(el => el === document.activeElement));
    await open();
    check('dialog_heading_receives_focus', await dialog.getByRole('heading', {name:'恢复沙箱文件'}).evaluate(el => el === document.activeElement));
    await page.keyboard.press('Shift+Tab');
    check('backward_tab_stays_in_dialog', await dialog.evaluate(el => el.contains(document.activeElement)));
    await select(fixture.stopped_session);
    await dialog.getByText('生成失败', {exact:true}).waitFor();
    check('real_failed_snapshot_has_no_download', await dialog.locator('[data-recovery-id]').filter({hasText:'生成失败'}).getByRole('button', {name:'下载 ZIP',exact:true}).count() === 0);
    check('dead_original_session_allows_creation', await dialog.getByRole('button', {name:'创建恢复快照'}).isEnabled());
    page.recoveryPosts = 0;
    page.recoveryPreviousIds = await dialog.locator('[data-recovery-id]').evaluateAll(rows => rows.map(row => row.getAttribute('data-recovery-id')));
    page.recoveryRequestListener = request => {
      if (request.method() === 'POST' && /\/recoveries$/.test(request.url())) page.recoveryPosts++;
    };
    page.on('request', page.recoveryRequestListener);
    await dialog.getByRole('button', {name:'创建恢复快照'}).click();
    const busy = dialog.getByRole('button', {name:'正在核验并生成…'});
    await busy.waitFor();
    check('create_reports_busy_and_disables_repeat', await busy.isDisabled());
    await busy.evaluate(el => { el.click(); el.click(); });
    await dialog.getByRole('button', {name:'关闭恢复文件'}).click();
    await open();
    await select(fixture.stopped_session);
    check('close_reopen_does_not_repost', page.recoveryPosts === 1);
  } else if (page.recoveryStage === 'download') {
    await page.waitForFunction(previous => Array.from(document.querySelectorAll('[data-recovery-id]'))
      .some(row => !previous.includes(row.getAttribute('data-recovery-id')) && row.textContent.includes('下载 ZIP')),
      page.recoveryPreviousIds, {timeout:45000});
    check('one_actual_creation_after_close_reopen', page.recoveryPosts === 1);
    page.off('request', page.recoveryRequestListener);
    const row = dialog.locator('[data-recovery-id]').filter({has:page.getByRole('button', {name:'下载 ZIP',exact:true})}).first();
    evidence.recovery_id = await row.getAttribute('data-recovery-id');
    if (await row.getByRole('button', {name:'收起内容'}).count()) await row.getByRole('button', {name:'收起内容'}).click();
    await row.getByRole('button', {name:'查看内容'}).click();
    await dialog.getByRole('list', {name:'快照内容'}).waitFor();
    check('manifest_is_paginated', await dialog.getByRole('list', {name:'快照内容'}).locator('li').count() === 20);
    evidence.manifest_pages = [];
    while (true) {
      evidence.manifest_pages.push(await dialog.getByRole('list', {name:'快照内容'}).innerText());
      const next = dialog.getByRole('button', {name:'下一页'});
      if (await next.count() === 0 || await next.isDisabled()) break;
      await next.click();
    }
    const content = evidence.manifest_pages.join('\n');
    check('actual_file_empty_directory_and_mode_visible', content.includes('Ref/browser-output.txt') && content.includes('Ref/browser-empty') && content.includes('权限 600'));
    await dialog.getByRole('button', {name:'上一页'}).click();
    const downloadEvent = page.waitForEvent('download', {timeout:45000});
    await row.getByRole('button', {name:'下载 ZIP',exact:true}).click();
    const download = await downloadEvent;
    evidence.download_path = fixture.root + '/browser-recovery.zip';
    await download.saveAs(evidence.download_path);
    check('actual_browser_saved_zip', await download.failure() === null);
    evidence.detail = await page.evaluate(async ({sid,id}) => {
      const result = await fetch('/api/v1/terminal/sessions/' + sid + '/recoveries/' + id,
        {headers:{Authorization:'Bearer ' + localStorage.getItem('vf_token')}});
      if (!result.ok) throw new Error('Actual receipt unavailable');
      return result.json();
    }, {sid:fixture.stopped_session,id:evidence.recovery_id});
  } else if (page.recoveryStage === 'corrupt') {
    let downloads = 0;
    const observed = () => { downloads++; };
    page.on('download', observed);
    try {
      const row = dialog.locator('[data-recovery-id="' + evidence.recovery_id + '"]');
      const response = page.waitForResponse(r => r.url().endsWith('/' + evidence.recovery_id + '/download'));
      await row.getByRole('button', {name:'下载 ZIP',exact:true}).click();
      check('actual_corrupt_archive_http_409', (await response).status() === 409);
      await dialog.getByRole('alert').filter({hasText:'未通过校验'}).waitFor();
      check('corrupt_archive_error_visible_no_browser_download', downloads === 0);
    } finally { page.off('download', observed); }
  } else if (page.recoveryStage === 'restart') {
    await dialog.getByRole('button', {name:'关闭恢复文件'}).click();
    await page.reload();
    await page.getByText('Sandbox recovery browser acceptance',{exact:true}).click();
    await page.getByText('终端',{exact:true}).click();
    await open();
    await select(fixture.stopped_session);
    const row = dialog.locator('[data-recovery-id="' + evidence.recovery_id + '"]');
    await row.getByRole('button', {name:'下载 ZIP',exact:true}).waitFor();
    check('snapshot_visible_after_actual_api_restart', await row.count() === 1);
    const event = page.waitForEvent('download', {timeout:45000});
    await row.getByRole('button', {name:'下载 ZIP',exact:true}).click();
    const download = await event;
    await download.saveAs(fixture.root + '/browser-after-restart.zip');
    check('download_available_after_actual_restart', await download.failure() === null);
    await dialog.getByRole('button', {name:'关闭恢复文件'}).click();
    await page.getByText('No sandbox history', {exact:true}).click();
    await open();
    await dialog.getByText('当前算例没有沙箱会话记录。使用隔离沙箱产生的工作副本会显示在这里。').waitFor();
    check('different_case_shows_empty_history', await dialog.locator('[data-recovery-id]').count() === 0);
    await dialog.getByRole('button', {name:'关闭恢复文件'}).click();
  } else if (page.recoveryStage === 'layout') {
    evidence.layouts = [];
    await page.emulateMedia({reducedMotion:'reduce'});
    for (const theme of ['light','dark']) {
      await page.evaluate(theme => {
        document.documentElement.dataset.theme = theme; document.body.dataset.theme = theme;
      }, theme);
      for (const viewport of [{width:375,height:812},{width:812,height:375},{width:1280,height:900}]) {
        await page.setViewportSize(viewport);
        const metrics = await dialog.evaluate(el => {
          const section = el.querySelector('.terminal-recovery');
          const box = el.getBoundingClientRect();
          const controls = [...el.querySelectorAll('button,select')];
          return {x:box.x,y:box.y,width:box.width,height:box.height,
            clientWidth:section.clientWidth,scrollWidth:section.scrollWidth,
            minControlHeight:Math.min(...controls.map(b => b.getBoundingClientRect().height))};
        });
        check('layout_' + theme + '_' + viewport.width,
          metrics.x >= 0 && metrics.y >= 0 && metrics.width <= viewport.width && metrics.height <= viewport.height
          && metrics.scrollWidth <= metrics.clientWidth + 1 && metrics.minControlHeight >= 44, metrics);
        const focusVisible = await dialog.evaluate(el => {
          const section = el.querySelector('.terminal-recovery');
          return [...el.querySelectorAll('button:not(:disabled),select:not(:disabled)')].every(control => {
            control.focus();
            const box = control.getBoundingClientRect(), container = section.getBoundingClientRect();
            return box.top >= container.top && box.bottom <= container.bottom && box.left >= container.left && box.right <= container.right;
          });
        });
        check('focused_controls_visible_' + theme + '_' + viewport.width, focusVisible);
        await dialog.locator('.terminal-recovery').evaluate(el => {el.scrollTop=0;});
        const screenshot = fixture.root + '/recovery-' + theme + '-' + viewport.width + '.png';
        await page.screenshot({path:screenshot});
        evidence.layouts.push({theme,viewport,metrics,screenshot});
      }
    }
    await page.setViewportSize({width:375,height:812});
    await dialog.evaluate(el => {
      const entries = [el,...el.querySelectorAll('*')].map(node => [node, getComputedStyle(node).fontSize, node.getAttribute('style')]);
      window.recoveryFontStyles = entries.map(([node,,style]) => [node,style]);
      for (const [node,size] of entries) node.style.fontSize = parseFloat(size)*2 + 'px';
    });
    check('double_text_has_no_horizontal_overflow', await dialog.locator('.terminal-recovery').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    await page.screenshot({path:fixture.root + '/recovery-double-text.png'});
    await page.evaluate(() => {for (const [node,style] of window.recoveryFontStyles) {
      if (style === null) node.removeAttribute('style'); else node.setAttribute('style',style);
    } delete window.recoveryFontStyles;});
    await page.setViewportSize({width:1280,height:900});
  } else throw new Error('Unknown recovery stage');
  return evidence;
}
