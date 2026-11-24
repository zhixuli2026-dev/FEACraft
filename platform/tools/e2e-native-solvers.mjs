import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { basename, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const { chromium } = await import(process.env.VIBEFLOW_TEST_PLAYWRIGHT_MODULE || 'playwright');

const [base, root, mode] = process.argv.slice(2);
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(base));
const examples = fileURLToPath(new URL('../server/examples/', import.meta.url));
const load = path => readFile(join(examples, path), 'utf8');
const deck = (await readFile(join(examples, '../calculix_smoke.py'), 'utf8')).split("DECK = '''")[1].split("'''")[0];
const transient = await load('moose/thermal/transient.i');
const exportDeck = await load('code_aster/axial/axial.export');
const originalComm = await load('code_aster/axial/axial.comm');
const fixtures = {
  'C-run/calculix/beam.inp': deck,
  'C-run/su2/naca0012.cfg': (await load('su2/inv_NACA0012.cfg')).replace('ITER= 250','ITER= 3')
    .replace('MESH_FILENAME= mesh_NACA0012_inv.su2','MESH_FILENAME= Ref/naca0012.su2'),
  'Ref/naca0012.su2': await load('su2/mesh_NACA0012_inv.su2'),
  'axial.sif': await load('elmer/axial/axial.sif'),
  'C-run/moose/line.i': await load('moose/thermal/line.i'),
  'C-run/moose/part.i': transient.replace('end_time = 0.4', 'end_time = 0.2'),
  'C-run/moose/continued.i': transient,
  'C-run/aster/axial.export': exportDeck + '\nR base state R 0\n',
  'C-run/aster/axial.comm': originalComm,
  'C-run/aster/axial.mail': await load('code_aster/axial/axial.mail'),
  'C-run/aster/continue.export': await load('code_aster/axial/continue.export'),
  'C-run/aster/continue.comm': await load('code_aster/axial/continue.comm'),
};
for (const name of ['mesh.header','mesh.nodes','mesh.elements','mesh.boundary']) {
  fixtures['brick/'+name] = await load('elmer/axial/brick/'+name);
}

const browser = await chromium.launch({channel:'msedge', headless:true});
const page = await browser.newPage({ viewport:{width:1440,height:1000}, acceptDownloads:true });
page.setDefaultTimeout(15000);
const duplicateKeys = [];
page.on('console', message => {
  if (message.text().includes('Encountered two children with the same key')) duplicateKeys.push(message.text());
});
const reports = [];
let token;
const http = (path, {data, method = data ? 'POST' : 'GET', headers = {}, auth = token} = {}) => page.request.fetch(base+'/api/v1'+path, {
  method, data, headers: { ...(auth ? {Authorization:'Bearer '+auth} : {}), ...headers }, timeout:90000,
});
async function json(path, options) {
  const response = await http(path, options);
  if (!response.ok()) throw new Error(`${path} returned ${response.status()} ${await response.text()}`);
  return response.json();
}
const signup = () => json('/auth/register', {auth:null, data:{email:`solver-${randomUUID()}@example.com`,name:'solver acceptance',password:randomUUID()}});
const launcher = page.locator('.execution-launch');
const result = page.locator('.job-solver-results');
const caseName = `solver-browser-${Date.now()}`;
const digest = data => createHash('sha256').update(data).digest('hex');
const pause = ms => new Promise(resolve => setTimeout(resolve,ms));
const isBusy = async response => [409,429,503].includes(response.status()) && (await response.text()).includes('_busy');

try {
  token = (await signup()).token;
  const outsider = (await signup()).token;
  const [ws] = await json('/workspaces');
  const fixture = await json(`/workspaces/${ws.id}/cases`, {data:{name:caseName}});
  const prefix = `/cases/${fixture.id}`;
  const put = async (path, content) => {
    for (let attempt=0; attempt<8; attempt++) {
      const response = await http(`${prefix}/file`, {method:'PUT',data:{path,content}});
      if (response.ok()) return response.json();
      if (!await isBusy(response)) throw new Error(`Could not seed ${path}: ${response.status()} ${await response.text()}`);
      await pause(200+attempt*100);
    }
    throw new Error(`Timed out seeding ${path}`);
  };
  for (const [path, content] of Object.entries(fixtures)) assert.equal((await put(path, content)).ok, true, path);
  await page.addInitScript(value => localStorage.setItem('vf_token',value), token);
  await page.goto(base);
  await page.locator(`[data-case-name="${caseName}"]`).click();
  await page.evaluate(async () => (await import('/src/shell/dock.ts')).activatePanel('caseinfo'));
  await page.getByRole('region', {name:'阶段、输入与工作流'}).waitFor();
  await page.getByText('工程意图修订', {exact:true}).waitFor();
  assert.deepEqual(duplicateKeys, []);
  if (mode === '--case-controls-only') {
    await writeFile(join(root,'case-controls.json'), JSON.stringify({
      passed:true, workflowVisible:true, intentHistoryVisible:true, duplicateKeys, modelCalls:0,
    },null,2));
    await browser.close();
    process.exit(0);
  }
  await page.evaluate(async () => (await import('/src/shell/dock.ts')).activatePanel('jobs'));

  async function launch(solver, file, {restart, loseReply = false, screenshot = false} = {}) {
    assert.equal(await launcher.evaluate(el => el.open),false);
    await launcher.locator(':scope > summary').click();
    await launcher.getByLabel('任务方式').selectOption('native');
    await launcher.getByLabel('开源求解器').selectOption(solver);
    await launcher.getByLabel('求解输入文件').selectOption(file);
    if (restart) {
      await launcher.getByText('从历史发布续算', {exact:true}).click();
      await launcher.getByLabel('发布 ID').fill(restart.publication_id);
      await launcher.getByLabel('续算文件路径').fill(restart.checkpoint_path);
    }
    if (screenshot) {
      await page.screenshot({path:join(root,'native-launch.png')});
      await page.setViewportSize({width:390,height:844});
      const compact = await launcher.locator('fieldset').evaluate(el => getComputedStyle(el).gridTemplateColumns);
      assert(/^(?:\d+(?:\.\d+)?px|minmax\(0px, 1fr\))$/.test(compact), compact);
      await launcher.getByLabel('求解输入文件').focus();
      await page.screenshot({path:join(root,'native-launch-compact.png')});
      await page.setViewportSize({width:1440,height:1000});
    }
    await launcher.getByRole('button',{name:'核对提交',exact:true}).click();
    await launcher.getByRole('heading',{name:'确认任务'}).waitFor();
    assert((await launcher.innerText()).includes(file));
    const requestSeen = page.waitForRequest(request => new URL(request.url()).pathname === `/api/v1${prefix}/${solver}:solve`
      && request.method()==='POST');
    const receiptSeen = page.waitForResponse(response => response.url().endsWith(loseReply ? '/execution-submission' : `/${solver}:solve`)
      && response.request().method()==='POST', { timeout:90000 });
    if (loseReply) await page.route(`**/${solver}:solve`, async route => {
      let real = await route.fetch();
      for (let trial=0; trial<6 && await isBusy(real); trial++) {
        await pause(250+trial*100);
        real = await route.fetch();
      }
      if (real.ok()) await route.abort();
      else {
        console.error('Native request was rejected before reply could be dropped',real.status(),await real.text());
        await route.fulfill({response:real});
      }
    }, {times:1});
    await launcher.getByRole('button',{name:'确认提交',exact:true}).click();
    let request = await requestSeen;
    let receipt = await receiptSeen;
    let retries = 0;
    while (!loseReply && await isBusy(receipt) && retries < 4) {
      retries++;
      const retry = launcher.getByRole('button',{name:'重试原提交',exact:true});
      const next = page.waitForResponse(response => response.url().endsWith(`/${solver}:solve`) && response.request().method()==='POST');
      await retry.click();
      receipt = await next;
      const retried = receipt.request();
      assert.equal(retried.headers()['idempotency-key'],request.headers()['idempotency-key']);
      request = retried;
    }
    assert(receipt.ok(),await receipt.text());
    const reply = await receipt.json();
    if (loseReply) assert.equal(reply.found,true);
    const jobId = reply.job_id ?? reply.id;
    assert.equal(reply.case_id,fixture.id);
    assert(Number.isInteger(jobId));
    const key = request.headers()['idempotency-key'];
    const submitted = request.postDataJSON();
    const lookup = {kind:'native',key,body:{solver,input_path:file,...(restart ? {restart} : {})}};
    assert.deepEqual(submitted,{[solver==='su2' ? 'config_path':'input_path']:file,...(restart ? {restart}:{})});
    assert.equal((await json(`${prefix}/execution-submission`,{data:lookup})).id,jobId);
    assert.equal((await http(`${prefix}/execution-submission`,{data:{...lookup,body:{...lookup.body,input_path:file+'x'}}})).status(),409);
    assert.equal((await json(`${prefix}/${solver}:solve`,{data:submitted,headers:{'Idempotency-Key':key}})).job_id,jobId);
    let job;
    for (let index=0; index<240; index++) {
      job = await json(`/jobs/${jobId}`);
      if (!['queued','running'].includes(job.status)) break;
      await pause(500);
    }
    assert.equal(job.status,'succeeded',job.log_backlog);
    assert.equal(job.attempt.process_stop?.verified,true);
    assert.equal(job.attempt.process_stop?.scope_empty,true);
    const pubId = job.attempt.outputs.publication.id;
    assert.equal(job.attempt.handler.key,`${solver}.solve`);
    const trace = await json(`${prefix}/provenance/artifacts/${pubId}`);
    assert.equal(trace.publication.job_id,jobId);
    await result.locator('summary').waitFor({timeout:30000});
    if (await result.evaluate(el=>!el.open)) await result.locator('summary').click();
    await result.getByLabel('发布 ID').waitFor();
    assert.equal(await result.getByLabel('发布 ID').inputValue(),pubId);
    const row = {solver,jobId,pubId,file,archiveFiles:Object.keys(trace.files),lostReplyRecovered:loseReply,busyRetries:retries,stopVerified:true};
    console.log(JSON.stringify(row));
    reports.push(row);
    return {job,trace,row};
  }

  async function download(study, file) {
    const receipt = study.trace.files[file];
    assert(receipt,file);
    const wait = page.waitForEvent('download',{timeout:20000});
    wait.catch(()=>{});
    const response = page.waitForResponse(value=>new URL(value.url()).searchParams.get('path')===file
      && value.url().includes('/provenance/artifacts/'));
    await result.getByRole('button',{name:`下载原件 ${file}`,exact:true}).click();
    const received = await response;
    if (!received.ok()) throw new Error(`${received.status()} ${await received.text()}`);
    const output = await wait;
    assert.equal(output.suggestedFilename(),basename(file));
    const bytes = await readFile(await output.path());
    assert.equal(bytes.length,receipt.size);
    assert.equal(digest(bytes),receipt.sha256);
    study.row.browserDownload = {file,bytes:bytes.length,sha256:digest(bytes)};
    return bytes;
  }
  async function preview(study, file, pattern) {
    await result.getByRole('button',{name:`预览 ${file}`,exact:true}).click();
    const output = result.getByLabel(`${file} 的内容`,{exact:true});
    await output.waitFor();
    const text = await output.textContent();
    assert.match(text,pattern);
    study.row.preview = file;
    return text;
  }
  const ccx = await launch('calculix','C-run/calculix/beam.inp',{screenshot:true});
  const frd = ccx.row.archiveFiles.find(file=>file.endsWith('.frd'));
  const original = await download(ccx,frd);
  assert.match(original.toString(),/DISP/);
  const artifactRoute = `${prefix}/provenance/artifacts/${ccx.row.pubId}/file?path=${encodeURIComponent(frd)}`;
  assert.equal((await http(artifactRoute,{auth:outsider})).status(),404);
  assert.equal((await http(artifactRoute,{auth:null})).status(),401);
  assert.equal((await http(artifactRoute.replace(encodeURIComponent(frd),encodeURIComponent('../not-published')))).status(),404);
  const head = await http(artifactRoute,{method:'HEAD'});
  assert.equal(Number(head.headers()['content-length']),original.length);
  const range = await http(artifactRoute,{headers:{'Range':'bytes=2-11','X-Vibeflow-File-Version':head.headers()['x-vibeflow-file-version']}});
  assert.equal(range.status(),206);
  assert.deepEqual(await range.body(),original.subarray(2,12));
  assert.equal((await http(artifactRoute,{headers:{'X-Vibeflow-File-Version':'0'.repeat(64)}})).status(),409);
  const su2 = await launch('su2','C-run/su2/naca0012.cfg');
  await preview(su2,su2.row.archiveFiles.find(file=>file.endsWith('history.csv')),/rms|Iter/i);
  await download(su2,su2.row.archiveFiles.find(file=>/\.vtu$/.test(file)));
  const elmer = await launch('elmer','axial.sif');
  await download(elmer,elmer.row.archiveFiles.find(file=>file.endsWith('.vtu')));
  const moose = await launch('moose','C-run/moose/line.i');
  const line = await preview(moose,moose.row.archiveFiles.find(file=>file.endsWith('.csv')),/midpoint/);
  assert(Math.abs(Number(line.trim().split('\n').at(-1).split(',').at(-1))-50)<1e-8);
  await download(moose,moose.row.archiveFiles.find(file=>file.endsWith('.e')));
  const aster = await launch('code-aster','C-run/aster/axial.export');
  await preview(aster,aster.row.archiveFiles.find(file=>file.endsWith('.mess')),/ASTER/i);
  await download(aster,aster.row.archiveFiles.find(file=>file.endsWith('.rmed')));
  await page.screenshot({path:join(root,'native-results.png')});
  await put('C-run/calculix/beam.inp',deck.replaceAll('3,-1000','3,-2000'));
  const newer = await launch('calculix','C-run/calculix/beam.inp',{loseReply:true});
  const changed = await download(newer,frd);
  assert.notEqual(digest(changed),digest(original));
  await page.getByRole('button',{name:new RegExp(`^#${ccx.row.jobId} `)}).click();
  await result.locator('summary').click();
  await result.getByText('第 1 版 · 历史发布',{exact:true}).waitFor();
  assert.deepEqual(await download(ccx,frd),original);
  const resumedAster = await launch('code-aster','C-run/aster/continue.export',{restart:{
    publication_id:aster.row.pubId,checkpoint_path:aster.row.archiveFiles.find(file=>file.endsWith('_base.zip')),
  }});
  await preview(resumedAster,resumedAster.row.archiveFiles.find(file=>file.endsWith('.mess')),/ASTER/i);
  const part = await launch('moose','C-run/moose/part.i');
  const resumedMoose = await launch('moose','C-run/moose/continued.i',{restart:{
    publication_id:part.row.pubId,checkpoint_path:part.row.archiveFiles.find(file=>file.endsWith('_checkpoint.zip')),
  }});
  const continued = await preview(resumedMoose,resumedMoose.row.archiveFiles.find(file=>file.endsWith('.csv')),/midpoint/);
  assert(continued.trim().split('\n').at(-1).startsWith('0.4,'),continued);
  for (const study of [resumedAster,resumedMoose]) assert.equal(study.job.attempt.workspace.publications.length,1);
  await writeFile(join(root,'native-solver-browser.json'),JSON.stringify({passed:true,storage:'FS/SQLite',modelCalls:0,
    reports,compactLayoutVerified:true,historicalDownloadPinned:true,unauthorizedDenied:true,unpublishedPathDenied:true,rangeVerified:true,
    versionMismatchDenied:true,codeAsterAndMooseRestart:true},null,2));
  console.log(`PASS ${reports.length} real native solver jobs`);
} catch (error) {
  console.error(error.message);
  await page.screenshot({path:join(root,'native-test-failure.png')}).catch(()=>{});
  await writeFile(join(root,'native-test-progress.json'),JSON.stringify(reports,null,2));
  throw error;
} finally { await browser.close(); }
