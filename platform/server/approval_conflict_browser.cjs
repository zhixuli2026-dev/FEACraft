const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require(process.env.VIBEFLOW_PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const config = JSON.parse(process.env.VF_TEST_SETUP);
  const viteDir = path.dirname(require.resolve('vite/package.json', { paths: [process.cwd()] }));
  const { preview } = await import(pathToFileURL(path.join(viteDir, 'dist/node/index.js')));
  const server = await preview({ preview: { port: 0, host: '127.0.0.1' }, logLevel: 'error' });
  try {
    const browser = await chromium.launch({ executablePath: process.env.VIBEFLOW_BROWSER_EXECUTABLE });
    try {
      const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
      await page.addInitScript(({ userId, workspaceId, caseId, token }) => {
        localStorage.setItem('vf_token', token);
        sessionStorage.setItem('vf-workspace-selection-v1:'+userId, JSON.stringify({workspaceId, caseId}));
      }, { ...config, token: process.env.VF_TEST_SESSION });
      const origin = 'http://127.0.0.1:'+server.httpServer.address().port;
      const openAgent = async () => {
        await page.locator('.dv-tab').filter({ hasText: /^Agent$/ }).click();
        await page.getByTestId('agent-conversation').selectOption(String(config.conversationId));
      };
      let approvals = 0;
      const isApproval = r => r.method() === 'POST' && decodeURIComponent(new URL(r.url()).pathname) === config.approvalPath;
      page.on('request', r => { if (isApproval(r)) approvals++; });
      await page.goto(origin);
      await openAgent();
      const approve = page.getByRole('button', {name: '批准执行', exact: true});
      const response = page.waitForResponse(r => isApproval(r.request()), {timeout: 90000});
      await approve.click();
      const stream = await response;
      assert.equal(stream.status(), 200);
      const events = (await stream.text()).split(/\r?\n/).filter(s => s.startsWith('data:')).map(s => JSON.parse(s.slice(5)));
      const outcome = events.find(e => e.type === 'tool.result');
      assert.equal(outcome.ok, false);
      const errorCard = page.locator('[data-testid="chat-tool-card"][data-status="error"]').filter({hasText: outcome.result});
      await errorCard.waitFor();
      const live = await errorCard.innerText();
      await page.reload();
      await openAgent();
      await errorCard.waitFor();
      assert.equal(await approve.count(), 0);
      assert.equal(approvals, 1);
      const reloaded = await errorCard.innerText();
      await errorCard.screenshot({path: path.join(config.folder, 'approval-conflict.png')});
      fs.writeFileSync(path.join(config.folder, 'browser-conflict.json'),
        JSON.stringify({approvals, live, reloaded, browser: browser.version(), passed: true}, null, 2));
      fs.writeFileSync(path.join(config.folder, 'race-approved-events.json'), JSON.stringify(events, null, 2));
    } finally { await browser.close(); }
  } finally { await server.close(); }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
