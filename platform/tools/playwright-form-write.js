// Execute via Playwright MCP against a disposable ui_test_server preview.
async (page) => {
  page.setDefaultTimeout(15000);
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const url = new URL(page.url());
  assert(url.hostname === "127.0.0.1" && !["8000", "5173"].includes(url.port), "Disposable preview required");
  const checks = [], writes = [];
  const request = (method, path, body) => page.evaluate(async ({method,path,body}) => {
    const r = await fetch("/api/v1"+path, {method,headers:{"Content-Type":"application/json",
      Authorization:"Bearer "+localStorage.getItem("vf_token")},
      ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    return {status:r.status, body:await r.json()};
  }, {method,path,body});
  const fixture = await page.evaluate(async () => {
    const auth = await fetch("/api/v1/auth/register", {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({email:`forms-${Date.now()}@example.com`,name:"forms-test",password:"isolated-forms-123"})}).then(r=>r.json());
    if (!auth.token) throw new Error("Registration failed");
    localStorage.setItem("vf_token",auth.token);
    const headers = {Authorization:"Bearer "+auth.token,"Content-Type":"application/json"};
    const workspaces = await fetch("/api/v1/workspaces",{headers}).then(r=>r.json());
    const cases = [];
    for (const name of ["form-write-browser", "form-other-case"]) {
      const c = await fetch(`/api/v1/workspaces/${workspaces[0].id}/cases`, {method:"POST",headers,body:JSON.stringify({name})}).then(r=>r.json());
      cases.push(c.id);
    }
    return {caseId:cases[0],otherId:cases[1]};
  });
  const prefix = `/cases/${fixture.caseId}`, other = `/cases/${fixture.otherId}`;
  const dict = async (p=prefix) => (await request("GET",p+"/caseDict")).body;
  const setMeta = async (p, probe) => {
    const before = await dict(p);
    const r = await request("PUT",p+"/caseDict/block",{block:"caseMeta",entries:{...before.parsed.caseMeta,draftProbe:probe}});
    assert(r.status===200,JSON.stringify(r));
  };
  await setMeta(prefix,"BASE"); await setMeta(other,"OTHER CASE");
  const entry = id => ({title:`Mesh ${id}`,stage:"mesh",category:"meshQuality",severity:"error",status:"open",blocking:true,message:"negative Jacobian"});
  assert((await request("PUT",prefix+"/caseDict/block",{block:"errors",entries:{entries:{"mesh-1":entry(1),"mesh-2":entry(2)}}})).status===200,"Seed errors");
  const texPath="E-report/latex/main.tex", texBase="\\documentclass{article}\n\\begin{document}\nORIGINAL\n\\end{document}\n";
  await request("PUT",prefix+"/file",{path:texPath,content:texBase,expected_sha256:null});
  const block = page.getByTestId("block-editor");
  const field = () => block.locator('input[placeholder="键"][value="draftProbe"]').locator("..").locator("input").nth(1);
  const apply = () => block.getByRole("button",{name:"应用该块",exact:true}).click();
  const idle = () => page.waitForFunction(()=>document.querySelector('[data-testid="block-editor"]')?.textContent.includes("应用该块"));
  const dirty = (testId, value) => page.waitForFunction(({testId,value})=>document.querySelector(`[data-testid="${testId}"]`)?.getAttribute("data-dirty")===String(value),{testId,value});
  const review = async scope => {
    await scope.getByRole("button",{name:"查看服务器版本",exact:true}).click();
    await scope.getByTestId("form-server-version").waitFor();
  };
  const keep = scope => scope.getByRole("button",{name:"保留草稿，基于此版本继续编辑",exact:true}).click();
  const reload = async scope => {
    page.once("dialog",dialog=>dialog.accept());
    await scope.getByRole("button",{name:"使用服务器版本",exact:true}).click();
  };
  const record = r => {
    if (["PUT","PATCH"].includes(r.method()) && r.url().startsWith(url.origin+"/api/v1"+prefix)) writes.push({method:r.method(),url:r.url(),body:r.postDataJSON()});
  };
  page.on("request",record);
  let release;
  const routes = [];
  const barrier = async (suffix, matches, afterCommit=true) => {
    let reached, handled;
    const ready = new Promise(resolve=>reached=resolve), blocked = new Promise(resolve=>release=resolve);
    const done = new Promise(resolve=>handled=resolve);
    const pattern = "**/api/v1"+prefix+suffix;
    const handler = async route => {
      if (!matches(route.request())) return route.continue();
      const response = afterCommit ? await route.fetch() : null;
      reached(); await blocked;
      try {
        if (response) await route.fulfill({response}); else await route.continue();
      } finally { handled(); }
    };
    await page.route(pattern,handler); routes.push([pattern,handler]);
    return {ready, finish:async()=>{release();await done;await page.unroute(pattern,handler);}};
  };
  try {
    await page.setViewportSize({width:1440,height:1000});
    await page.reload();
    await page.locator('[data-case-name="form-write-browser"]').click();
    await page.locator('.dv-tab').filter({hasText:/^caseDict$/}).click();
    await field().waitFor();
    await field().fill("LOCAL 中文草稿🙂");
    await page.getByTestId("error-investigate-mesh-2").click();
    await page.waitForFunction(()=>document.querySelector('[data-testid="error-status-mesh-2"]')?.textContent==="investigating");
    assert(await field().inputValue()==="LOCAL 中文草稿🙂","Panel refresh discarded dirty block");
    await apply(); await block.getByRole("alert").filter({hasText:"服务器内容已变化"}).waitFor();
    assert((await dict()).parsed.caseMeta.draftProbe==="BASE","Stale block overwrote server");
    await review(block);
    const viewed = await dict();
    await request("PUT",prefix+"/caseDict",{content:viewed.content+"\n// change after review\n"});
    await keep(block); await idle(); await apply();
    await block.getByRole("alert").filter({hasText:"服务器内容已变化"}).waitFor();
    await review(block);
    await page.screenshot({path:"tmp/mainline-N2-forms-evidence/form-conflict.png"});
    await keep(block); await idle(); await apply(); await dirty("block-editor",false); await idle();
    assert((await dict()).parsed.caseMeta.draftProbe==="LOCAL 中文草稿🙂","Reviewed block save failed");
    assert(writes.some(w=>w.body.expected_revision>=0&&w.body.expected_sha256?.length===64),"Missing exact version conditions");
    checks.push("dirty_block_survives_panel_refresh_real_409_review_does_not_force_overwrite");

    const blockHold = await barrier("/caseDict/block",r=>r.method()==="PUT"&&r.postDataJSON().entries?.draftProbe==="SUBMITTED");
    await field().fill("SUBMITTED"); await apply(); await blockHold.ready;
    assert(await block.getByRole("button",{name:"保存中…",exact:true}).isDisabled(),"Duplicate block submit enabled");
    await field().fill("LATER KEYSTROKES"); await blockHold.finish(); await idle();
    await dirty("block-editor",true);
    assert(await field().inputValue()==="LATER KEYSTROKES","Receipt erased new block input");
    assert((await dict()).parsed.caseMeta.draftProbe==="SUBMITTED","Wrong committed block");
    await apply(); await dirty("block-editor",false); await idle();
    assert((await dict()).parsed.caseMeta.draftProbe==="LATER KEYSTROKES","Block receipt baseline unusable");
    checks.push("block_save_single_inflight_receipt_preserves_later_input_next_save_succeeds");

    const row = page.getByTestId("error-entry-mesh-1");
    await page.getByTestId("error-resolution-mesh-1").fill("LOCAL RESOLUTION");
    await field().fill("REFRESH VIA BLOCK"); await apply(); await dirty("block-editor",false); await idle();
    assert(await page.getByTestId("error-resolution-mesh-1").inputValue()==="LOCAL RESOLUTION","Error draft lost on block refresh");
    await page.getByTestId("error-fixed-mesh-1").click();
    await row.getByRole("alert").filter({hasText:"服务器内容已变化"}).waitFor();
    await review(row); await reload(row); await dirty("error-entry-mesh-1",false);
    assert(await page.getByTestId("error-resolution-mesh-1").inputValue()==="","Explicit error reload failed");
    await page.getByTestId("error-resolution-mesh-1").fill("FIRST RESOLUTION");
    const errorHold = await barrier("/caseDict/errors/mesh-1",r=>r.method()==="PATCH");
    const patchCount = writes.filter(w=>w.method==="PATCH").length;
    await page.getByTestId("error-fixed-mesh-1").click(); await errorHold.ready;
    assert(await page.getByTestId("error-fixed-mesh-1").isDisabled(),"Duplicate error action enabled");
    await page.getByTestId("error-resolution-mesh-1").fill("LATER RESOLUTION"); await errorHold.finish();
    await page.waitForFunction(()=>!document.querySelector('[data-testid="error-fixed-mesh-1"]')?.disabled);
    assert(writes.filter(w=>w.method==="PATCH").length===patchCount+1,"Duplicate error PATCH");
    await dirty("error-entry-mesh-1",true);
    assert((await dict()).parsed.errors.entries["mesh-1"].resolution==="FIRST RESOLUTION","Wrong committed error content");
    assert(await page.getByTestId("error-resolution-mesh-1").inputValue()==="LATER RESOLUTION","Error receipt erased new input");
    await page.getByTestId("error-fixed-mesh-1").click(); await dirty("error-entry-mesh-1",false);
    assert((await dict()).parsed.errors.entries["mesh-1"].resolution==="LATER RESOLUTION","Error receipt baseline unusable");
    checks.push("error_draft_survives_other_form_refresh_conflict_reload_and_delayed_patch");

    await page.waitForFunction(()=>!document.querySelector('[data-testid="error-reopen-mesh-1"]')?.disabled);
    await page.getByTestId("error-resolution-mesh-1").fill("");
    await page.getByTestId("error-reopen-mesh-1").click(); await dirty("error-entry-mesh-1",false);
    assert((await dict()).parsed.errors.entries["mesh-1"].resolution==="","Cleared field silently kept old server text");
    checks.push("clearing_optional_error_fields_is_persisted_not_falsely_acknowledged");

    await page.waitForFunction(()=>!document.querySelector('[data-testid="error-propose-mesh-1"]')?.disabled);
    const beforeEmpty = await dict();
    await page.getByTestId("error-propose-mesh-1").click();
    await row.getByRole("alert").filter({hasText:"fixOptions"}).waitFor();
    assert((await dict()).dict_revision===beforeEmpty.dict_revision,"Empty repair evidence advanced revision");
    const fixText='修复"检查"🙂';
    await page.getByTestId("error-fix-mesh-1").fill(fixText);
    await page.getByTestId("error-propose-mesh-1").click(); await dirty("error-entry-mesh-1",false);
    const proposed=await dict();
    assert(proposed.parsed.errors.entries["mesh-1"].fixOptions.text===fixText,"Unicode/quotes changed during form save");
    assert(Number(proposed.parsed.errors.summary.blocking)===2,"Boolean serialization lost blocking errors");
    checks.push("empty_repair_evidence_rejected_unicode_quotes_and_blocking_count_preserved");

    await page.getByTestId("error-resolution-mesh-1").fill("REMOVED ENTRY DRAFT");
    const current = await dict();
    await request("PUT",prefix+"/caseDict/block",{block:"errors",entries:{entries:{"mesh-2":current.parsed.errors.entries["mesh-2"]}}});
    await field().fill("REFRESH REMOVED ENTRY"); await apply();
    await block.getByRole("alert").filter({hasText:"服务器内容已变化"}).waitFor();
    await review(block); await keep(block); await idle(); await apply(); await dirty("block-editor",false); await idle();
    assert(await page.getByTestId("error-resolution-mesh-1").inputValue()==="REMOVED ENTRY DRAFT","Deleted remote entry erased draft");
    assert(await page.getByTestId("error-fixed-mesh-1").isDisabled(),"Missing entry can still submit");
    await review(row);
    assert(await row.getByRole("button",{name:"保留草稿，基于此版本继续编辑",exact:true}).isDisabled(),"Missing entry rebased as existing");
    await reload(row); await row.waitFor({state:"detached"});
    checks.push("remotely_removed_error_retains_draft_until_explicit_discard");

    await field().fill("SWITCH DRAFT");
    page.once("dialog",dialog=>dialog.dismiss());
    await block.getByRole("combobox").selectOption("intent");
    assert(await block.getByRole("combobox").inputValue()==="caseMeta"&&await field().inputValue()==="SWITCH DRAFT","Cancelled block switch lost draft");
    page.once("dialog",dialog=>dialog.accept());
    await block.getByRole("combobox").selectOption("intent");
    await block.getByRole("combobox").selectOption("caseMeta"); await dirty("block-editor",false);
    assert(await field().inputValue()==="REFRESH REMOVED ENTRY","Confirmed block switch did not reset");
    checks.push("dirty_block_switch_requires_explicit_discard_cancel_preserves_input");

    await page.getByTestId("latex-table-headers").fill("Metric,Value");
    await page.getByTestId("latex-table-generate").click(); await page.getByTestId("latex-table-preview").waitFor();
    const snippet = await page.getByTestId("latex-table-preview").innerText();
    const latexHold = await barrier("/file",r=>r.method()==="PUT"&&r.postDataJSON().path===texPath&&!!r.postDataJSON().expected_sha256,false);
    await page.getByTestId("latex-table-insert").click(); await latexHold.ready;
    assert(await page.getByTestId("latex-table-insert").isDisabled(),"Duplicate LaTeX insert enabled");
    await request("PUT",prefix+"/file",{path:texPath,content:texBase.replace("ORIGINAL","EXTERNAL EDIT")});
    await latexHold.finish(); await page.getByTestId("latex-status").filter({hasText:"源文件已变化"}).waitFor();
    assert((await request("GET",prefix+"/file?path="+encodeURIComponent(texPath))).body.content.includes("EXTERNAL EDIT"),"Insert overwrote concurrent TeX edit");
    assert(await page.getByTestId("latex-table-preview").innerText()===snippet,"Conflict lost table snippet");
    const backup = writes.find(w=>w.body.path?.includes(".table-backup-"));
    assert(backup?.body.expected_sha256===null&&backup.body.content===texBase,"Backup is not create-only exact source");
    await page.getByTestId("latex-table-insert").click();
    await page.getByTestId("latex-status").filter({hasText:"表格已插入"}).waitFor();
    const savedTex=(await request("GET",prefix+"/file?path="+encodeURIComponent(texPath))).body.content;
    assert(savedTex.includes("EXTERNAL EDIT")&&savedTex.includes(snippet),"Retry lost current TeX or failed to insert snippet");
    await page.screenshot({path:"tmp/mainline-N2-forms-evidence/form-latex-saved.png"});
    checks.push("latex_insert_backups_create_only_conflict_keeps_source_and_snippet_retry_uses_fresh_hash");

    const loadHold = await barrier("/caseDict",r=>r.method()==="GET");
    await field().fill("LATE CASE RESPONSE"); await apply(); await loadHold.ready;
    await page.locator('[data-case-name="form-other-case"]').click();
    await page.waitForFunction(()=>document.querySelector('input[value="OTHER CASE"]')!==null);
    await loadHold.finish();
    await page.waitForTimeout(150);
    assert(await field().inputValue()==="OTHER CASE","Old case refresh contaminated new case form");
    checks.push("old_case_save_refresh_cannot_replace_new_case_panel");
    const result={passed:true,count:checks.length,checks,caseId:fixture.caseId,browser:page.context().browser().version(),
      isolated:true,real_api:true};
    await page.evaluate(result=>window.__formWriteEvidence=result,result);
    return result;
  } catch(error) {
    await page.evaluate(result=>window.__formWriteEvidence=result,{passed:false,checks,error:String(error)});
    throw error;
  } finally {
    if (release) release();
    for (const [pattern,handler] of routes) await page.unroute(pattern,handler);
    page.off("request",record);
  }
}
