const { chromium } = require(process.env.VIBEFLOW_PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  const config = JSON.parse(process.env.VF_TEST_SETUP);
  const browser = await chromium.launch({executablePath: process.env.VIBEFLOW_BROWSER_EXECUTABLE});
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.addInitScript(({token, userId, workspaceId, caseId}) => {
      localStorage.setItem("vf_token",token);
      sessionStorage.setItem("vf-workspace-selection-v1:"+userId, JSON.stringify({workspaceId,caseId}));
    }, {...config, token:process.env.VF_TEST_SESSION});
    await page.goto(config.origin, {waitUntil:"domcontentloaded"});
    const tab = page.locator(".dv-tab").filter({hasText:/^作业$/});
    await tab.click();
    const cancel = page.getByRole("button",{name:"取消作业 "+config.jobId, exact:true});
    // Hold the real request in transit while the real job is completing.
    await page.route("**/jobs/"+config.jobId+":cancel", async route => {
      fs.writeFileSync(config.marker,"release");
      const url = config.origin+"/api/v1/jobs/"+config.jobId;
      const deadline = Date.now()+35000;
      for (;;) {
        const r = await page.request.get(url,{headers:{Authorization:"Bearer "+process.env.VF_TEST_SESSION}});
        assert.equal(r.status(),200);
        const job = await r.json();
        if (job.status==="succeeded" && job.attempt.process_stop?.verified) break;
        assert.ok(Date.now()<deadline,"Original job did not settle");
        await new Promise(resolve=>setTimeout(resolve,150));
      }
      await route.continue();
    });
    const response = page.waitForResponse(r=>r.request().method()==="POST" && r.url().endsWith("/jobs/"+config.jobId+":cancel"));
    await cancel.click();
    const reply = await (await response).json();
    assert.equal(reply.ok,false);
    assert.ok(reply.message);
    const notice = page.getByRole("status").filter({hasText:reply.message});
    await notice.waitFor();
    assert.ok(!(await page.getByText("取消请求已受理，正在核验停止与收尾状态。",{exact:true}).count()));
    fs.writeFileSync(path.join(config.folder,"browser-result.json"),JSON.stringify({
      job_id:config.jobId,passed:true,reply,displayed:await notice.innerText(),mock_llm_calls:0
    },null,2));
  } finally {
    await browser.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
