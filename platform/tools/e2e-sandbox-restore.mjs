// Production browser/API only. Configure page.restoreFixture from ready.json.
// No request interception or model responses. Stages: setup, restore, offline, layout.
async (page) => {
  const fixture = page.restoreFixture;
  if (!fixture?.web || !fixture.root) throw new Error('Isolated fixture required');
  const report = page.restoreEvidence ??= {checks:[],llm_calls:0,mock_llm_calls:0};
  const check = (name,ok) => {report.checks.push({name,passed:!!ok});if(!ok)throw new Error(name);};
  const dialog = page.getByRole('dialog',{name:'恢复沙箱文件'});
  const form = page.getByRole('region',{name:'写回选中文件内容'});
  const source = 'Ref/browser-output.txt';
  const api = async (method,path,body) => page.evaluate(async ({method,path,body}) => {
    const result = await fetch('/api/v1'+path,{method,headers:{Authorization:'Bearer '+localStorage.getItem('vf_token'),
      ...(body ? {'Content-Type':'application/json'} : {})},body:body ? JSON.stringify(body) : undefined});
    return {status:result.status,text:await result.text()};
  },{method,path,body});
  const reopen = async () => {
    await dialog.getByRole('button',{name:'关闭恢复文件'}).click();
    await page.getByRole('button',{name:'恢复文件',exact:true}).click();
    await dialog.getByLabel('沙箱会话').selectOption(fixture.stopped_session);
    await dialog.locator('[data-recovery-id="'+report.recovery_id+'"]').getByRole('button',{name:'查看内容'}).click();
    await form.waitFor();
  };
  if (page.restoreStage==='setup') {
    await page.goto(fixture.web);
    await page.getByRole('textbox',{name:'邮箱'}).fill(fixture.email);
    await page.getByRole('textbox',{name:'密码'}).fill('isolated-test-123');
    await page.getByRole('button',{name:'登录',exact:true}).click();
    await page.getByText(fixture.case_name,{exact:true}).click();
    await page.getByText('终端',{exact:true}).click();
    await page.getByRole('button',{name:'恢复文件',exact:true}).click();
    await dialog.getByLabel('沙箱会话').selectOption(fixture.stopped_session);
    await dialog.getByRole('button',{name:'创建恢复快照'}).click();
    await dialog.getByRole('button',{name:'下载 ZIP',exact:true}).waitFor({timeout:45000});
    const row=dialog.locator('[data-recovery-id]').filter({has:page.getByRole('button',{name:'下载 ZIP',exact:true})}).first();
    report.recovery_id=await row.getAttribute('data-recovery-id');
    await row.getByRole('button',{name:'查看内容'}).click();
    await form.waitFor();
    check('actual_recovered_snapshot_exposes_file_restore_form',await form.isVisible());
  } else if(page.restoreStage==='restore') {
    page.restorePostCount=0;
    page.restoreListener=request=>{if(request.method()==='POST' && request.url().endsWith('/restore'))page.restorePostCount++;};
    page.on('request',page.restoreListener);
    await form.getByRole('searchbox',{name:'筛选恢复文件'}).fill('browser-output.txt');
    await form.getByRole('checkbox',{name:/Ref\/browser-output.txt/}).check();
    await form.getByRole('button',{name:'预览写回影响'}).click();
    await form.getByText('新增文件',{exact:true}).waitFor();
    check('preview_requires_separate_confirmation',await form.getByRole('button',{name:'确认写回文件内容'}).isDisabled());
    const original = await api('GET','/cases/'+fixture.case_id+'/raw?path='+source);
    check('preview_did_not_write_file',original.status===404);
    check('real_competing_edit_written', (await api('PUT','/cases/'+fixture.case_id+'/file',{path:source,content:'browser competing edit'})).status===200);
    await form.getByRole('checkbox',{name:'我已核对目标，确认只写回以上文件内容。'}).check();
    await form.getByRole('button',{name:'确认写回文件内容'}).click();
    await form.getByRole('alert').filter({hasText:'预览后已变化'}).waitFor();
    check('stale_preview_error_preserves_actual_edit',(await api('GET','/cases/'+fixture.case_id+'/raw?path='+source)).text==='browser competing edit');
    await form.getByRole('button',{name:'预览写回影响'}).click();
    await form.getByText('覆盖当前文件内容',{exact:true}).waitFor();
    await form.getByRole('checkbox',{name:'我已核对目标，确认只写回以上文件内容。'}).check();
    await form.getByRole('button',{name:'确认写回文件内容'}).click();
    await reopen();
    await form.getByRole('status').filter({hasText:'已写回 1 个文件'}).waitFor();
    check('close_reopen_resolves_original_confirmation_without_resubmitting',page.restorePostCount===2);
    check('browser_confirmed_file_matches_actual_shell_proof',(await api('GET','/cases/'+fixture.case_id+'/raw?path='+source)).text===fixture.proof+'\n');
    check('real_later_edit_written',(await api('PUT','/cases/'+fixture.case_id+'/file',{path:source,content:'later browser edit'})).status===200);
    await reopen();
    await form.getByRole('status').filter({hasText:'已写回 1 个文件'}).waitFor();
    check('reopening_last_receipt_does_not_overwrite_later_edit',page.restorePostCount===2 && (await api('GET','/cases/'+fixture.case_id+'/raw?path='+source)).text==='later browser edit');
    page.off('request',page.restoreListener);
  } else if(page.restoreStage==='offline') {
    await form.getByRole('button',{name:'预览写回影响'}).click();
    await form.getByText('覆盖当前文件内容',{exact:true}).waitFor();
    await form.getByRole('checkbox',{name:'我已核对目标，确认只写回以上文件内容。'}).check();
    try {
      await page.context().setOffline(true);
      await form.getByRole('button',{name:'确认写回文件内容'}).click();
      await form.getByRole('alert').filter({hasText:'写回结果尚未确认'}).waitFor();
      check('actual_network_offline_keeps_uncertain_confirmation',await form.getByRole('button',{name:'重试同一次确认'}).isEnabled());
    } finally { await page.context().setOffline(false); }
    const key='vf_recovery_confirmation:'+fixture.stopped_session+':'+report.recovery_id;
    const before=await page.evaluate(key=>JSON.parse(localStorage.getItem(key)).request_id,key);
    await form.getByRole('button',{name:'重试同一次确认'}).click();
    await form.getByRole('status').filter({hasText:'已写回 1 个文件'}).waitFor();
    const after=await page.evaluate(key=>JSON.parse(localStorage.getItem(key)).request_id,key);
    check('explicit_network_retry_preserves_confirmation_identity',before===after);
    check('actual_online_retry_writes_selected_proof',(await api('GET','/cases/'+fixture.case_id+'/raw?path='+source)).text===fixture.proof+'\n');
    report.request_id=after;
  } else if(page.restoreStage==='layout') {
    await form.getByRole('searchbox',{name:'筛选恢复文件'}).fill('long-name');
    await page.emulateMedia({reducedMotion:'reduce'});
    report.screenshots=[];
    for(const theme of ['light','dark']) {
      await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;document.body.dataset.theme=theme;},theme);
      for(const viewport of [{width:375,height:812},{width:812,height:375}]) {
        await page.setViewportSize(viewport);
        await form.getByRole('searchbox').focus();
        await page.waitForFunction(() => {
          const focused=document.activeElement, dialog=document.querySelector('dialog');
          if(!focused || !dialog || !dialog.contains(focused))return false;
          const a=focused.getBoundingClientRect(), b=dialog.getBoundingClientRect();
          return a.top>=b.top && a.bottom<=b.bottom;
        });
        check('restore_'+theme+'_'+viewport.width+'_focused_input_visible',await form.getByRole('searchbox').evaluate(el=>el===document.activeElement));
        check('restore_'+theme+'_'+viewport.width+'_no_horizontal_overflow',await dialog.locator('.terminal-recovery').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
        const path=fixture.root+'/restore-'+theme+'-'+viewport.width+'.png';
        await page.screenshot({path});report.screenshots.push(path);
      }
    }
    await page.setViewportSize({width:1280,height:900});
  } else throw new Error('Unknown stage');
  return report;
}
