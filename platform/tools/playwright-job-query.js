// Run via Playwright MCP against platform/tools/job_query_ui_server.py (disposable fixture).
async (page) => {
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const url = new URL(page.url());
  assert(url.hostname === "127.0.0.1" && !["8000", "5173"].includes(url.port), "Disposable preview required");
  const fixture = await page.evaluate(async () => {
    const auth = await fetch("/api/v1/auth/register", {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({email:`query-${Date.now()}@example.com`,name:"query-test",password:"isolated-query-123"})}).then(r=>r.json());
    if (!auth.token) throw new Error("Registration failed");
    localStorage.setItem("vf_token", auth.token);
    const headers = {Authorization:"Bearer "+auth.token,"Content-Type":"application/json"};
    const ws = await fetch("/api/v1/workspaces",{headers}).then(r=>r.json());
    const c = await fetch(`/api/v1/workspaces/${ws[0].id}/cases`,{method:"POST",headers,
      body:JSON.stringify({name:"query-acceptance"})}).then(r=>r.json());
    return await fetch(`/api/v1/fixture/${c.id}`,{method:"POST",headers}).then(r=>r.json());
  });
  await page.reload();
  await page.locator('[data-case-name="query-acceptance"]').click();
  await page.locator('.dv-tab').filter({hasText:/^作业$/}).click();
  const job = (index, command) => page.getByRole("button",{name:`#${fixture.jobs[index]} ${command}`,exact:true});
  const meta = page.getByTestId("job-metadata");
  await job(5,"modern finalizing · 收尾中").focus();
  await page.keyboard.press("Enter");
  await meta.locator("summary").filter({hasText:"需要恢复检查"}).waitFor();
  assert(!(await meta.getAttribute("open")), "Metadata should leave space for logs by default");
  await meta.locator("summary").click();
  await meta.getByText("需要恢复检查，尚未自动重新运行。结果发布尚未完成。",{exact:true}).waitFor();
  const modern = await meta.innerText();
  for (const key of ["run_id","step_id","attempt_id"]) assert(modern.includes(fixture[key]),"Missing "+key);
  await job(0,"legacy succeeded").click();
  await page.getByText("旧日志 succeeded\nEnd",{exact:true}).waitFor();
  await meta.locator("summary").click();
  await meta.getByText("历史作业：未记录流程与尝试信息",{exact:true}).waitFor();
  await page.getByRole("button",{name:"批量",exact:true}).click();
  await page.getByTestId("batch-row").waitFor();
  assert((await page.getByTestId("batch-row").innerText()).includes("1/6 成功"),"Legacy batch counts changed");
  await page.getByRole("button",{name:"批量",exact:true}).click();
  const failure = `**/api/v1/jobs/${fixture.jobs[1]}/log-stream`;
  await page.route(failure,route=>route.fulfill({status:503,body:"fixture log unavailable"}));
  await job(1,"legacy failed").click();
  await page.getByText("日志连接中断，请重新选择任务重试。",{exact:true}).waitFor();
  await page.unroute(failure);
  await job(0,"legacy succeeded").click();
  await page.getByText("旧日志 succeeded\nEnd",{exact:true}).waitFor();
  await job(1,"legacy failed").click();
  await page.getByText("旧日志 failed\nEnd",{exact:true}).waitFor();
  return {passed:true,checks:["keyboard_job_selection","durable_run_step_attempt_phase",
    "recovery_notice","collapsed_metadata_preserves_log_space","legacy_log_and_metadata",
    "historical_batch_counts","log_error_and_reselection"],fixture};
}
