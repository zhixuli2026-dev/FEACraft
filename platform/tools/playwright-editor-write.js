// Run through Playwright browser_run_code_unsafe(filename=...) after opening an isolated ui_test_server URL.
async (page) => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const url = new URL(page.url());
  assert(url.hostname === "127.0.0.1" && !["8000","5173"].includes(url.port), "Use the disposable UI preview");
  const fixture = await page.evaluate(async () => {
    const response = await fetch("/api/v1/auth/register", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({email:`editor-${Date.now()}@example.com`,name:"editor-test",password:"isolated-editor-123"})});
    const auth = await response.json();
    if (!auth.token) throw new Error("Registration failed");
    localStorage.setItem("vf_token", auth.token);
    const headers = {Authorization:"Bearer "+auth.token,"Content-Type":"application/json"};
    const workspaces = await fetch("/api/v1/workspaces",{headers}).then(r=>r.json());
    const c = await fetch(`/api/v1/workspaces/${workspaces[0].id}/cases`, {method:"POST",headers,
      body:JSON.stringify({name:"editor-write-browser"})}).then(r=>r.json());
    await fetch(`/api/v1/cases/${c.id}/file`, {method:"PUT",headers,
      body:JSON.stringify({path:"D-post/editor-write.txt",content:"BASE"})});
    return {caseId:c.id};
  });
  const prefix = `/api/v1/cases/${fixture.caseId}`;
  const request = (method, path, body) => page.evaluate(async ({method,path,body}) => {
    const result = await fetch(path, {method, headers:{Authorization:"Bearer "+localStorage.getItem("vf_token"),
      "Content-Type":"application/json"}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    return {status:result.status, body:await result.json()};
  }, {method,path,body});
  const edit = text => page.evaluate(value => window.monaco.editor.getModels().at(-1).setValue(value), text);
  const currentText = () => page.evaluate(() => window.monaco.editor.getModels().at(-1).getValue());
  const save = () => page.getByRole("button", {name:"保存 (Ctrl+S)", exact:true}).click();
  const checks = [];
  const writes = [];
  const record = request => {
    if (request.method() === "PUT" && request.url().startsWith(url.origin+prefix)) writes.push(request.postDataJSON());
  };
  page.on("request", record);
  let release;
  try {
    await page.reload();
    await page.locator('[data-case-name="editor-write-browser"]').click();
    await page.locator('[data-path="D-post/editor-write.txt"]').click();
    await page.waitForFunction(() => window.monaco?.editor.getModels().at(-1)?.getValue() === "BASE");
    await edit("LOCAL 草稿🙂");
    await request("PUT", prefix+"/file", {path:"D-post/editor-write.txt",content:"EXTERNAL"});
    await save();
    await page.getByTestId("editor-save-error").waitFor();
    assert(await currentText() === "LOCAL 草稿🙂", "Conflict discarded the draft");
    assert((await request("GET", prefix+"/file?path=D-post/editor-write.txt")).body.content === "EXTERNAL", "Stale draft overwrote server");
    assert(writes.at(-1).expected_sha256?.length === 64, "Editor did not send a baseline hash");
    await page.getByRole("button", {name:"查看服务器版本",exact:true}).click();
    await page.getByTestId("editor-server-version").getByText("EXTERNAL",{exact:true}).waitFor();
    await page.setViewportSize({width:1440,height:1000});
    await page.screenshot({path:"tmp/mainline-N2-editor-evidence/editor-conflict.png"});
    assert(await currentText() === "LOCAL 草稿🙂", "Viewing server changed local draft");
    await page.getByRole("button", {name:"保留草稿，基于此版本继续编辑",exact:true}).click();
    await save();
    await page.waitForFunction(() => document.querySelector('[data-editor-path="D-post/editor-write.txt"]')?.getAttribute("data-dirty") === "false");
    assert((await request("GET", prefix+"/file?path=D-post/editor-write.txt")).body.content === "LOCAL 草稿🙂", "Reviewed save failed");
    checks.push("real_HTTP_409_keeps_CJK_emoji_draft_review_then_conditional_save_succeeds");

    let reached;
    const ready = new Promise(resolve => reached=resolve);
    const barrier = new Promise(resolve => release=resolve);
    const route = async route => {
      if (route.request().method() !== "PUT" || route.request().postDataJSON()?.content !== "SUBMITTED") return route.continue();
      const response = await route.fetch(); // Server has committed; hold only the response to the editor.
      reached();
      await barrier;
      await route.fulfill({response});
    };
    await page.route("**"+prefix+"/file", route);
    await edit("SUBMITTED");
    await save();
    await ready;
    assert(await page.getByRole("button",{name:"保存中…",exact:true}).isDisabled(), "Saving button allows duplicate submissions");
    await edit("TYPED WHILE WAITING");
    release();
    await page.waitForFunction(() => [...document.querySelectorAll("button")].some(b=>b.textContent === "保存 (Ctrl+S)" && !b.disabled));
    assert(await currentText() === "TYPED WHILE WAITING", "Delayed receipt erased later keystrokes");
    assert((await request("GET", prefix+"/file?path=D-post/editor-write.txt")).body.content === "SUBMITTED", "Unexpected saved content");
    await page.unroute("**"+prefix+"/file", route);
    await save();
    await page.waitForFunction(() => document.querySelector('[data-editor-path="D-post/editor-write.txt"]')?.getAttribute("data-dirty") === "false");
    assert((await request("GET", prefix+"/file?path=D-post/editor-write.txt")).body.content === "TYPED WHILE WAITING", "Second save did not use acknowledged baseline");
    checks.push("delayed_real_save_receipt_preserves_new_input_and_next_save_uses_new_baseline");

    const before = (await request("GET", prefix+"/caseDict")).body;
    await page.locator('[data-path="0-caseDict/caseDict"]').click();
    await page.waitForFunction(() => window.monaco?.editor.getModels().at(-1)?.getValue().includes("workflowState A"));
    await edit(before.content+"\n// LOCAL DICTIONARY\n");
    await request("PUT", prefix+"/caseDict", {content:before.content+"\n// EXTERNAL DICTIONARY\n", expected_revision:before.dict_revision});
    await save();
    await page.getByTestId("editor-save-error").waitFor();
    assert(writes.at(-1).expected_revision === before.dict_revision && writes.at(-1).expected_sha256 === before.content_sha256,
      "caseDict did not submit the exact opened revision/hash");
    assert((await currentText()).includes("LOCAL DICTIONARY"), "Dictionary draft lost");
    await page.getByRole("button",{name:"查看服务器版本",exact:true}).click();
    await page.getByTestId("editor-server-version").waitFor();
    page.once("dialog", dialog=>dialog.accept());
    await page.getByRole("button",{name:"使用服务器版本",exact:true}).click();
    await page.waitForFunction(() => document.querySelector('[data-editor-path="0-caseDict/caseDict"]')?.getAttribute("data-dirty") === "false");
    assert((await currentText()).includes("EXTERNAL DICTIONARY"), "Explicit reload failed");
    await edit((await currentText())+"\n// AFTER REVIEW\n");
    await save();
    await page.waitForFunction(() => document.querySelector('[data-editor-path="0-caseDict/caseDict"]')?.getAttribute("data-dirty") === "false");
    assert((await request("GET",prefix+"/caseDict")).body.dict_revision === before.dict_revision+2, "Dictionary receipt/revision did not advance once");
    checks.push("caseDict_revision_conflict_explicit_reload_stays_clean_then_saves_once");
    await page.screenshot({path:"tmp/mainline-N2-editor-evidence/editor-saved.png"});
    await edit((await currentText()).replace("workflowState A", "workflowState B"));
    await save();
    await page.getByTestId("editor-save-error").waitFor();
    const gateError = await page.getByTestId("editor-save-error").textContent();
    assert(gateError.includes("不得修改 workflowState") && !gateError.includes("服务器文件已变化"), "Stage gate was shown as a file conflict");
    assert((await request("GET",prefix+"/caseDict")).body.workflow_state === "A", "Ordinary save bypassed stage gate");
    assert((await currentText()).includes("workflowState B"), "Rejected draft was discarded");
    checks.push("engineering_stage_gate_message_is_distinct_from_file_conflict_and_keeps_draft");
    const result = {passed:true, checks, count:checks.length, caseId:fixture.caseId, origin:url.origin,
      browser:page.context().browser()?.version(),
      input:"Playwright with actual Monaco model changes and real API; one response held after server commit"};
    await page.evaluate(value => {window.__editorWriteEvidence = value;}, result);
    return result;
  } finally {
    release?.();
    page.off("request", record);
  }
}
