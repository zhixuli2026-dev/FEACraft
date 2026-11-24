// Actual production browser/API and actual offline transport. No interception.
async (page) => {
  const fixture=page.retirementFixture;
  if(!fixture?.root || !fixture.web)throw new Error('Isolated fixture required');
  const report=page.retirementEvidence ??= {checks:[],llm_calls:0,mock_llm_calls:0};
  const check=(name,ok)=>{report.checks.push({name,passed:!!ok});if(!ok)throw new Error(name);};
  const dialog=page.getByRole('dialog',{name:'恢复沙箱文件'});
  const form=page.getByRole('region',{name:'结束旧会话记录'});
  const reopen=async()=>{
    await dialog.getByRole('button',{name:'关闭恢复文件'}).click();
    await page.getByRole('button',{name:'恢复文件',exact:true}).click();
    await dialog.getByLabel('沙箱会话').selectOption(fixture.stopped_session);
    await form.waitFor();
  };
  if(page.retirementStage==='setup'){
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
    await form.waitFor();
    check('completed_real_snapshot_exposes_explicit_retirement',await form.getByRole('button',{name:'确认结束旧会话'}).isDisabled());
    report.recovery_id=await form.getByLabel('收尾保留的快照').inputValue();
    check('actual_completed_snapshot_selected',!!report.recovery_id);
  }else if(page.retirementStage==='confirm'){
    page.retirementPosts=0;
    page.retirementListener=r=>{if(r.method()==='POST'&&r.url().endsWith('/retirement'))page.retirementPosts++;};
    page.on('request',page.retirementListener);
    await form.getByRole('checkbox').check();
    check('acknowledgment_enables_confirmation',await form.getByRole('button',{name:'确认结束旧会话'}).isEnabled());
    try{
      await page.context().setOffline(true);
      await form.getByRole('button',{name:'确认结束旧会话'}).click();
      await form.getByRole('alert').filter({hasText:'收尾结果尚未确认'}).waitFor();
      check('actual_offline_request_stays_uncertain',await form.getByRole('button',{name:'重试同一次收尾'}).isEnabled());
    }finally{await page.context().setOffline(false);}
    const count=page.retirementPosts;
    await reopen();
    await form.getByRole('button',{name:'重试同一次收尾'}).waitFor();
    check('reopen_only_queries_and_keeps_same_snapshot',page.retirementPosts===count && await form.getByLabel('收尾保留的快照').inputValue()===report.recovery_id);
    await form.getByRole('button',{name:'重试同一次收尾'}).click();
    await form.getByRole('status').filter({hasText:'旧会话记录已收尾'}).waitFor({timeout:45000});
    check('explicit_retry_commits_actual_retirement',page.retirementPosts===count+1);
    check('unknown_exit_is_reported_as_unknown',await form.getByText(/原退出码：未知/).isVisible());
    await reopen();
    await form.getByRole('status').filter({hasText:'旧会话记录已收尾'}).waitFor();
    check('receipt_survives_reopening_without_repost',page.retirementPosts===count+1);
    const remote=await page.evaluate(async f=>{
      const headers={Authorization:'Bearer '+localStorage.getItem('vf_token')};
      const receipt=await fetch('/api/v1/terminal/sessions/'+f.stopped_session+'/retirement',{headers});
      const file=await fetch('/api/v1/cases/'+f.case_id+'/raw?path=Ref/browser-output.txt',{headers});
      return {receipt:await receipt.json(),fileStatus:file.status};
    },fixture);
    check('browser_receipt_matches_production_api',remote.receipt.status==='retired' && remote.receipt.recovery_id===report.recovery_id);
    check('retirement_does_not_publish_snapshot_files',remote.fileStatus===404);
    report.receipt=remote.receipt;
    page.off('request',page.retirementListener);
  }else if(page.retirementStage==='layout'){
    report.screenshots=[];
    for(const theme of ['light','dark']){
      await page.evaluate(value=>{document.documentElement.dataset.theme=value;document.body.dataset.theme=value;},theme);
      for(const viewport of [{width:375,height:812},{width:812,height:375}]){
        await page.setViewportSize(viewport);
        await form.scrollIntoViewIfNeeded();
        check('retirement_'+theme+'_'+viewport.width+'_no_overflow',await dialog.locator('.terminal-recovery').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
        const path=fixture.root+'/retirement-'+theme+'-'+viewport.width+'.png';
        await page.screenshot({path});report.screenshots.push(path);
      }
    }
    await page.setViewportSize({width:1280,height:900});
  }else throw new Error('Unknown retirement stage');
  return report;
}
