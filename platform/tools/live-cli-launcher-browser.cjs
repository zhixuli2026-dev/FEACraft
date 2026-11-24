// Actual browser, network, terminal and model; no routing or inference stubs.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const [root, playwrightPath, chromePath, reviewPath] = process.argv.slice(2);
const review = JSON.parse(fs.readFileSync(reviewPath, 'utf8'));
assert.equal(review.classification, 'real_llm');
for (const [name, expected] of Object.entries(review.sources)) {
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(name)).digest('hex'), expected, name);
}
const { chromium } = require(playwrightPath);
const info = JSON.parse(fs.readFileSync(path.join(root, 'browser.json'), 'utf8'));
const write = (name, data) => fs.writeFileSync(path.join(root, name), JSON.stringify(data, null, 2));
(async () => {
  const browser = await chromium.launch({executablePath: chromePath, headless: true});
  const context = await browser.newContext({viewport:{width:1600,height:1000}});
  const page = await context.newPage();
  const evidence = {passed:false, engine:'Chromium', desktop:{width:1600,height:1000}, mobile:{width:390,height:844},
    network_substitution:false, mock_llm_calls:0, commands:[], page_errors:[]};
  let output = '';
  page.on('pageerror', e => evidence.page_errors.push(e.message));
  page.on('websocket', ws => {
    if (!new URL(ws.url()).pathname.endsWith('/ws/terminal')) return;
    ws.on('framesent', ({payload}) => {
      try {
        const value = JSON.parse(String(payload));
        if (value.type==='input' && value.data.includes(info.expected_command)) evidence.commands.push(value.data);
      } catch {}
    });
    ws.on('framereceived', ({payload}) => {
      if (typeof payload === 'string') {
        try { const value=JSON.parse(payload); if(value.type==='ready') evidence.session_id=value.session; } catch {}
      } else output=(output+payload.toString('utf8')).slice(-100000);
    });
  });
  async function until(check, seconds=180) {
    const end=Date.now()+seconds*1000;
    while (!(await check())) {
      const failedExit=output.match(/VF_(?:KIMI|OPENCODE|CLAUDE|CODEX)_COMPLETED_([1-9][0-9]*)/);
      if(failedExit)throw Error('Actual CLI exited with '+failedExit[1]);
      if (Date.now()>end) throw Error('Actual browser condition timed out');
      await new Promise(r=>setTimeout(r,200));
    }
  }
  async function contractState() {
    return page.evaluate(async id=>{
      const response=await fetch('/api/v1/cases/'+id+'/cli-contract',{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}});
      if(!response.ok)throw Error('Cannot read actual contract');return response.json();
    },info.case_id);
  }
  async function settings() {
    if(page.viewportSize().width<900)await page.getByLabel('工作面板',{exact:true}).selectOption('settings');
    else await page.locator('.dv-tab').filter({hasText:/^设置$/}).click();
    const controls=page.getByTestId('cli-contracts');await controls.scrollIntoViewIfNeeded();return controls;
  }
  try {
    await page.goto(info.url);
    await page.getByLabel('邮箱', {exact:true}).fill(info.email);
    await page.getByLabel('密码', {exact:true}).fill('isolated-test-123');
    await page.getByRole('button',{name:'登录',exact:true}).click();
    await page.getByText('CLI custom installation acceptance',{exact:true}).first().click({timeout:30000});
    if(info.contracts) {
      const controls=await settings();
      await controls.getByLabel('CLI 工具',{exact:true}).selectOption('kimi');
      await controls.getByLabel('补充工作约定',{exact:true}).fill('When asked for the project verification label, reply with '+info.contract_label+'.');
      await controls.getByRole('button',{name:'保存契约并备份',exact:true}).click();
      await until(async()=>{const value=await contractState();return value.selected==='kimi'&&value.instructions.includes(info.contract_label);},60);
      await controls.getByRole('button',{name:'保存契约并备份',exact:true}).waitFor();
      await controls.getByLabel('CLI 工具',{exact:true}).selectOption('claude');
      await controls.getByLabel('补充工作约定',{exact:true}).fill('Temporary Claude instructions for actual restore acceptance.');
      await controls.getByRole('button',{name:'保存契约并备份',exact:true}).click();
      await until(async()=> (await contractState()).selected==='claude',60);
      const changed=await contractState();const back=changed.backups.find(x=>x.selected==='kimi');assert.ok(back);
      await controls.getByLabel('恢复备份',{exact:true}).selectOption(back.id);
      await controls.getByRole('button',{name:'恢复所选备份',exact:true}).click();
      await until(async()=>{const value=await contractState();return value.selected==='kimi'&&value.instructions.includes(info.contract_label);},60);
      evidence.contract_restore_verified=true;
      await controls.getByRole('status').filter({hasText:'已恢复所选备份'}).waitFor();
      await page.screenshot({path:path.join(root,'contract-desktop-restored.png'),fullPage:true});
    }
    await page.locator('.dv-tab').filter({hasText:/^终端$/}).click();
    const panel=page.getByTestId('terminal-panel');
    const launch=panel.getByRole('button',{name:(info.harness || 'kimi')+' ▸',exact:true});
    await launch.waitFor({state:'visible',timeout:120000});
    evidence.provider_runs=[];
    for(const providerId of info.provider_ids || [null]) {
    if(providerId) await panel.getByLabel(info.provider_label || 'Kimi API 配置',{exact:true}).selectOption(String(providerId));
    output='';
    await page.screenshot({path:path.join(root,'launcher-desktop-before.png'),fullPage:true});
    await launch.click();
    await until(()=>output.includes(info.completion_marker || 'VF_KIMI_COMPLETED_0'));
    evidence.sent_command=evidence.commands[0];
    evidence.completion_marker=true;
    await page.screenshot({path:path.join(root,'launcher-desktop-complete.png'),fullPage:true});
    assert.ok(evidence.session_id);
    await panel.getByRole('button',{name:'关闭终端 '+evidence.session_id.slice(0,8),exact:true}).click();
    await until(async()=>page.evaluate(async sid=>{
      const response=await fetch('/api/v1/terminal/sessions/'+sid,{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}});
      if(!response.ok)return false;
      const value=await response.json();return value.phase==='closed' && value.cleanup_verified===true;
    },evidence.session_id),60);
    evidence.closed_via_ui=true;
    if(providerId) {
      const actual=await page.evaluate(async ({id,file})=>{
        const r=await fetch('/api/v1/cases/'+id+'/raw?path='+encodeURIComponent(file),{headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')}});
        if(!r.ok)throw Error('Actual CLI output unavailable');return r.text();
      },{id:info.case_id,file:info.output_file || 'kimi-output.jsonl'});
      evidence.provider_runs.push({profile_id:providerId,session_id:evidence.session_id,output:actual});
    }
    }
    assert.deepEqual(evidence.commands,(info.provider_ids || [null]).map(()=>info.expected_command+'\r'));
    await page.setViewportSize({width:390,height:844});
    if(info.contracts) {
      const controls=await settings();
      await controls.getByLabel('恢复备份',{exact:true}).selectOption(info.initial_backup);
      await controls.getByRole('button',{name:'恢复所选备份',exact:true}).click();
      await until(async()=> (await contractState()).selected===null,60);
      await controls.getByRole('status').filter({hasText:'已恢复所选备份'}).waitFor();
      await controls.screenshot({path:path.join(root,'contract-mobile-restored.png')});
    }
    await page.getByLabel('工作面板',{exact:true}).selectOption('terminal');
    await launch.waitFor({state:'visible'});
    await page.screenshot({path:path.join(root,'launcher-mobile.png'),fullPage:true});
    evidence.mobile_layout=await page.evaluate(()=>({width:innerWidth,documentWidth:document.documentElement.scrollWidth}));
    assert.ok(evidence.mobile_layout.documentWidth<=evidence.mobile_layout.width);
    assert.equal(evidence.page_errors.length,0);
    evidence.passed=true;write('browser-done.json',evidence);
    console.log(JSON.stringify({passed:true,session_id:evidence.session_id,commands:evidence.commands.length,closed_via_ui:true}));
  } catch(error) {
    evidence.failure=String(error);write('browser-failed.json',evidence);
    fs.writeFileSync(path.join(root,'browser-failure-body.txt'),await page.locator('body').innerText());
    await page.screenshot({path:path.join(root,'browser-failure.png'),fullPage:true});
    console.error('Browser acceptance failed; evidence preserved');process.exitCode=1;
  } finally {await browser.close();}
})();
