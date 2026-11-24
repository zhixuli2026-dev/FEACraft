// Drive the built UI against the isolated live CLI and provider.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const [setupPath, playwrightPath, edgePath] = process.argv.slice(2);
const { chromium } = require(playwrightPath);
const setup = JSON.parse(fs.readFileSync(setupPath, 'utf8'));
const root = path.dirname(setupPath);
const sourceId = setup.conversation_id;
const route = cid => '/codex/conversations/' + cid;
const terminal = ['completed', 'cancelled', 'failed', 'interrupted', 'recovery_required', 'evidence_invalid'];
const newMarker = 'NEW_' + crypto.randomUUID().replaceAll('-', '');

(async () => {
  const browser = await chromium.launch({ executablePath: edgePath, headless: true });
  const page = await browser.newPage({ viewport: { width: 720, height: 940 } });
  const receipt = { passed: false, runtime: setup.runtime, provider: 'deepseek',
    model: setup.model, edge: browser.version(), database: setup.database, storage: setup.storage,
    scenario: setup.scenario, simulated_llm: false, turns: [] };
  const save = () => fs.writeFileSync(path.join(root, 'browser-result.json'), JSON.stringify(receipt, null, 2));
  const step = name => { receipt.step = name; save(); };
  const requests = [];
  page.on('request', request => {
    if (request.method() === 'POST') requests.push(new URL(request.url()).pathname);
  });
  const convo = page.getByTestId('agent-conversation');
  const backend = page.getByTestId('agent-backend');
  const panel = page.getByTestId('native-chat');
  const resume = panel.getByRole('checkbox', { name: '恢复原生线程' });
  const nativeFork = panel.getByTestId('native-fork');
  const records = panel.getByLabel('查看原生回合');
  const turnHandoff = panel.getByTestId('native-turn-handoff');
  let token;

  async function wait(label, fn, timeout = 145000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const value = await fn();
      if (value) return value;
      await new Promise(resolve => setTimeout(resolve, 300));
    }
    throw new Error('Timed out: ' + label);
  }
  async function call(method, url, data, status = 200, raw = false) {
    const response = await page.request.fetch(setup.url + '/api/v1' + url, {
      method, data, headers: { Authorization: 'Bearer ' + token }, timeout: 25000,
    });
    assert.equal(response.status(), status, method + ' ' + url + ': ' + (await response.text()).slice(0, 600));
    return raw ? await response.text() : await response.json();
  }
  async function open(cid) {
    await convo.selectOption(String(cid));
    if (await backend.inputValue() !== setup.runtime) await backend.selectOption(setup.runtime);
    await panel.waitFor();
  }
  async function postClick(locator, url, expected) {
    const [response] = await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1' + url && r.request().method() === 'POST'),
      locator.click(),
    ]);
    assert.equal(response.status(), 200, await response.text());
    if (expected) assert.deepEqual(response.request().postDataJSON(), expected);
    const value = await response.json();
    await wait('branch selected', async () => Number(await convo.inputValue()) === (value.conversation_id ?? value.id));
    return value;
  }
  async function latest(cid) {
    return (await call('GET', route(cid) + '/turns')).data[0];
  }
  async function send(cid, prompt, label) {
    const previous = await latest(cid);
    const input = panel.getByTestId('native-input');
    await input.fill(prompt, { timeout: 30000 });
    await panel.getByRole('button', { name: '发送', exact: true }).click();
    const turn = await wait('committed real turn ' + label, async () => {
      const item = await latest(cid);
      const error = await panel.locator('.native-notice[role="alert"]').first().textContent({ timeout: 100 }).catch(() => '');
      assert.ok(!error, error);
      return item && item.id !== previous?.id && item;
    });
    receipt.turns.push({ label, id: turn.id }); save();
    return turn;
  }
  async function completed(turn, expected, requireNoTools = true) {
    const final = await wait('native turn terminal', async () => {
      const value = await call('GET', '/codex/turns/' + turn.id);
      if (requireNoTools) assert.notEqual(value.phase, 'awaiting_input');
      return terminal.includes(value.phase) && value;
    });
    assert.equal(final.phase, 'completed', JSON.stringify(final));
    assert.equal(final.cleanup_verified, true);
    const raw = await call('GET', '/codex/turns/' + turn.id + '/events', undefined, 200, true);
    const events = raw.split('\n').filter(line => line.startsWith('data:')).map(line => JSON.parse(line.slice(5)));
    const commands = events.filter(e => e.type === 'item.completed' && e.payload.type === 'commandExecution');
    const approvals = await call('GET', '/codex/turns/' + turn.id + '/approvals');
    const answer = events.filter(e => e.type === 'text.delta').map(e => e.payload.text).join('');
    assert.equal(answer, expected);
    if (requireNoTools) {
      assert.equal(commands.length, 0);
      assert.equal(approvals.length, 0);
    }
    Object.assign(receipt.turns.find(item => item.id === turn.id), { thread_id: final.thread_id,
      expected_response: true, cleanup_verified: true, commands: commands.length, approvals: approvals.length });
    save();
    return { final, events, commands };
  }
  async function verifyHandoff(cid, head) {
    const branch = await call('GET', '/conversations/' + cid);
    assert.equal(branch.forked_from_id, null);
    assert.deepEqual(branch.pending_tools, []);
    assert.deepEqual(branch.tool_calls, []);
    assert.deepEqual((await call('GET', route(cid) + '/turns')).data, []);
    assert.equal(branch.events.length, 1);
    const payload = branch.events[0].payload;
    assert.equal(payload.kind, 'handoff');
    assert.equal(payload.handoff_mode, 'bounded_text');
    assert.equal(payload.source_conversation_id, sourceId);
    assert.equal(payload.covers_max_id, head);
    assert.ok(!payload.text.includes(newMarker));
    assert.deepEqual(Object.keys(payload).sort(), ['covers_max_id', 'handoff_mode', 'kind', 'source_conversation_id', 'text']);
    return payload.text;
  }
  function handoffReceipt(events, text) {
    const handoffs = events.filter(e => e.type === 'context.handoff');
    assert.equal(handoffs.length, 1);
    assert.equal(handoffs[0].payload.sha256, crypto.createHash('sha256').update(
      JSON.stringify(text), 'utf8').digest('hex'));
    assert.equal(handoffs[0].payload.characters, Array.from(text).length);
    assert.equal(handoffs[0].payload.mode, 'bounded_text');
  }

  try {
    step('login');
    await page.goto(setup.url);
    await page.getByLabel('邮箱', { exact: true }).fill(setup.email);
    await page.getByLabel('密码', { exact: true }).fill('isolated-test-123');
    await page.getByRole('button', { name: '登录', exact: true }).click();
    const panes = page.getByLabel('工作面板', { exact: true });
    await panes.waitFor({ timeout: 30000 });
    await page.getByRole('button', { name: '选择算例', exact: true }).click();
    await page.getByText(setup.case_title, { exact: true }).filter({ visible: true }).first().click();
    await panes.selectOption('agent');
    await open(sourceId);
    token = await page.evaluate(() => localStorage.getItem('vf_token'));
    assert.ok(token);

    step('source starts the real file read');
    const command = setup.runtime === 'windows'
      ? 'Get-Content -LiteralPath Ref/cli-inference-proof.txt' : 'cat Ref/cli-inference-proof.txt';
    const sourcePrompt = '请实际用命令工具执行 ' + command +
      '，读取后只回复文件中的完整验证字符串。不要改文件，不要猜测，不要添加解释。';
    const source = await send(sourceId, sourcePrompt, 'source');
    const pendingRead = async () => (await call('GET', '/codex/turns/' + source.id + '/approvals'))
      .find(item => item.phase === 'pending');
    const initialApproval = setup.runtime === 'windows'
      ? await wait('file read requests an approval', async () => {
          const value = await call('GET', '/codex/turns/' + source.id);
          assert.ok(!terminal.includes(value.phase), JSON.stringify(value));
          return await pendingRead();
        })
      : await pendingRead();
    const sourceHistory = await call('GET', '/conversations/' + sourceId);
    const sourceUser = sourceHistory.events.find(e => e.payload.native_execution_id === source.id && e.payload.role === 'user');
    assert.ok(sourceUser);
    function validateApproval(pending) {
      assert.equal(pending.method, 'item/commandExecution/requestApproval');
      assert.deepEqual(pending.params.commandActions.map(a => a.type), ['read']);
      const norm = value => value.replaceAll('\\', '/').replace(/\/+/g, '/').toLowerCase();
      if (setup.runtime === 'windows') {
        const wrapped = pending.params.command.match(/^"([^"]+)" -NoProfile -Command '([^']+)'$/);
        if (pending.params.command !== command) {
          assert.ok(wrapped);
          assert.equal(norm(wrapped[1]), norm(setup.shell));
          assert.equal(wrapped[2], command);
        }
        assert.equal(norm(pending.params.cwd), norm(path.dirname(path.dirname(setup.proof))));
        assert.equal(norm(pending.params.commandActions[0].path), norm(setup.proof));
      } else {
        const drive = root[0].toLowerCase();
        const mountedRoot = '/mnt/' + drive + root.slice(2).replaceAll('\\', '/');
        let wslProof = setup.proof && '/mnt/' + drive + setup.proof.slice(2);
        if (!wslProof) {
          assert.ok(pending.params.cwd.startsWith(mountedRoot + '/files/.staging/'));
          assert.match(pending.params.cwd.slice(mountedRoot.length), /^\/files\/\.staging\/job-\d+-[a-f0-9]{32}$/);
          wslProof = pending.params.cwd + '/Ref/cli-inference-proof.txt';
          assert.equal(fs.readFileSync(drive.toUpperCase() + ':' + wslProof.slice(6), 'utf8'), setup.nonce + '\n');
        }
        assert.ok([command, "/usr/bin/bash -c '" + command + "'"].includes(pending.params.command));
        assert.equal(pending.params.cwd, path.posix.dirname(path.posix.dirname(wslProof)));
        assert.equal(pending.params.commandActions[0].path, wslProof);
      }
    }

    step('active round text handoff');
    if (initialApproval) {
      validateApproval(initialApproval);
      await page.getByRole('region', { name: '待回应请求' }).waitFor({ timeout: 30000 });
    }
    await turnHandoff.scrollIntoViewIfNeeded();
    assert.equal(await nativeFork.isEnabled(), false);
    await page.screenshot({ path: path.join(root, 'active-round-controls.png') });
    const phaseBeforeHandoff = (await call('GET', '/codex/turns/' + source.id)).phase;
    assert.ok(!terminal.includes(phaseBeforeHandoff), phaseBeforeHandoff);
    const mid = await postClick(turnHandoff, '/conversations/' + sourceId + '/handoff',
      { before_answer_turn_id: source.id });
    const midText = await verifyHandoff(mid.id, sourceUser.id);
    assert.equal(midText, 'User:\n' + sourcePrompt);
    assert.ok(!midText.includes(setup.nonce));
    if (initialApproval) {
      assert.equal((await call('GET', '/codex/turns/' + source.id)).phase, 'awaiting_input');
      assert.equal((await pendingRead()).id, initialApproval.id);
    }
    const modelRequestsAtHandoff = requests.filter(p => /\/codex\/sessions\/.+\/turns$/.test(p));
    assert.equal(modelRequestsAtHandoff.length, 1);
    receipt.mid = { id: mid.id, boundary: sourceUser.id, source_phase: phaseBeforeHandoff,
      approval_still_pending: initialApproval ? true : null, automatic_model_requests: 0 };
    save();

    step('approve only the inspected read and complete the source');
    await open(sourceId);
    const approval = await wait('source has completed or awaits a checked approval', async () => {
      const value = await call('GET', '/codex/turns/' + source.id);
      if (terminal.includes(value.phase)) return { pending: null };
      const pending = await pendingRead();
      return pending && { pending };
    });
    if (approval.pending) {
      validateApproval(approval.pending);
      await call('POST', '/codex/turns/' + source.id + '/approvals/' + approval.pending.id + ':respond', { decision: 'accept' });
    }
    const original = await completed(source, setup.nonce, false);
    assert.equal(original.commands.length, 1);
    assert.equal(original.commands[0].payload.exitCode, 0);
    assert.ok(original.commands[0].payload.aggregatedOutput.includes(setup.nonce));
    const proofAnswer = (await call('GET', '/conversations/' + sourceId)).events.find(e => e.can_fork
      && e.payload.native_execution_id === source.id);
    assert.ok(proofAnswer);

    const recall = '请只回复此前答案中的完整文件验证字符串。根据历史回答，不调用工具，不添加其他文字。';
    if (setup.scenario === 'full') {
      step('native fork button and native continuation');
      await wait('native fork enabled after cleanup', () => nativeFork.isEnabled(), 30000);
      const fork = await postClick(nativeFork, route(sourceId) + '/fork');
      const native = await wait('native fork registered', async () => {
        const status = await call('GET', '/codex/forks/' + fork.id);
        return terminal.includes(status.phase) && status;
      });
      assert.equal(native.phase, 'completed', JSON.stringify(native));
      assert.equal(native.source_turn_id, source.id);
      assert.equal(native.boundary_event_id, proofAnswer.id);
      assert.notEqual(native.thread_id, original.final.thread_id);
      assert.equal(native.cleanup_verified, true);
      assert.equal(requests.filter(p => /\/codex\/sessions\/.+\/turns$/.test(p)).length, 1);
      await wait('branch defaults to native resume', () => resume.isChecked(), 30000);
      const nativeReply = await completed(await send(native.conversation_id, recall, 'native branch'), setup.nonce);
      assert.equal(nativeReply.final.thread_id, native.thread_id);
      assert.ok(!nativeReply.events.some(e => e.type === 'context.handoff'));
      const workspaces = await call('GET', '/test/fork-workspace/' + native.id + '/' + nativeReply.final.id);
      const remote = setup.storage === 'authenticated SeaweedFS';
      assert.deepEqual(workspaces, { same_source: true, separate_sessions: true,
        fork_used_private_workspace: remote, fork_staged_case: false, continuation_staged_case: remote });
      receipt.native = { fork_id: native.id, boundary: proofAnswer.id, cleanup_verified: true,
        thread_id: native.thread_id, automatic_model_requests: 0, workspaces };
      save();
    }

    const checkOlderAnswer = setup.scenario !== 'mid';
    if (checkOlderAnswer) {
      step('new real source round and old answer handoff');
      await open(sourceId);
      const newer = await send(sourceId, '不要调用工具。只回复以下字符串：' + newMarker, 'newer source');
      await completed(newer, newMarker);
      await records.selectOption(source.id);
      assert.equal(await nativeFork.isEnabled(), false);
      const oldAnswer = panel.locator('article').filter({ hasText: setup.nonce }).getByTestId('native-answer-handoff');
      await oldAnswer.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(root, 'old-answer-controls.png') });
      const older = await postClick(oldAnswer, '/conversations/' + sourceId + '/handoff',
        { head_event_id: proofAnswer.id });
      const olderText = await verifyHandoff(older.id, proofAnswer.id);
      assert.equal(olderText, 'User:\n' + sourcePrompt + '\n\nAssistant:\n' + setup.nonce);
      assert.equal(await resume.isChecked(), false);
      const oldReply = await completed(await send(older.id, recall, 'old answer text branch'), setup.nonce);
      handoffReceipt(oldReply.events, olderText);
      assert.notEqual(oldReply.final.thread_id, original.final.thread_id);
      if (receipt.native) assert.notEqual(oldReply.final.thread_id, receipt.native.thread_id);
      receipt.older = { id: older.id, boundary: proofAnswer.id, excludes_newer_history: true,
        persisted_handoff_delivered: true, thread_id: oldReply.final.thread_id };
    }

    step('active snapshot can be continued after the source finishes');
    await open(mid.id);
    assert.equal(await resume.isChecked(), false);
    const control = 'MID_' + crypto.randomUUID().replaceAll('-', '');
    const midReply = await completed(await send(mid.id, '不要调用工具。只回复控制字符串：' + control, 'active text branch'), control);
    handoffReceipt(midReply.events, midText);
    assert.notEqual(midReply.final.thread_id, original.final.thread_id);
    if (receipt.older) assert.notEqual(midReply.final.thread_id, receipt.older.thread_id);
    if (receipt.native) assert.notEqual(midReply.final.thread_id, receipt.native.thread_id);
    receipt.mid.persisted_handoff_delivered = true;
    receipt.mid.thread_id = midReply.final.thread_id;
    const newestSource = await call('GET', '/conversations/' + sourceId);
    const originalRounds = checkOlderAnswer ? 2 : 1;
    assert.equal(newestSource.events.filter(e => e.can_fork).length, originalRounds);
    assert.equal((await call('GET', route(sourceId) + '/turns')).data.length, originalRounds);
    await call('POST', '/conversations/' + sourceId + '/handoff', { head_event_id: sourceUser.id }, 409);
    await call('POST', '/conversations/' + mid.id + '/handoff', { head_event_id: proofAnswer.id }, 404);
    await call('POST', '/conversations/' + mid.id + '/handoff', { before_answer_turn_id: source.id }, 404);
    await call('POST', '/conversations/' + sourceId + '/handoff', {
      head_event_id: proofAnswer.id, before_answer_turn_id: source.id }, 422);
    const owner = token;
    const stranger = await call('POST', '/auth/register', {
      email: crypto.randomUUID() + '@example.com', name: 'Other user', password: 'isolated-test-123',
    });
    try {
      token = stranger.token;
      await call('POST', '/conversations/' + sourceId + '/handoff', { head_event_id: proofAnswer.id }, 404);
    } finally {
      token = owner;
    }
    receipt.rejected_invalid_boundary_requests = 5;
    receipt.source_command_executed_once = true;
    receipt.passed = true;
    step('complete');
  } catch (error) {
    receipt.failure = error.stack;
    await page.screenshot({ path: path.join(root, 'browser-failure.png') }).catch(() => {});
    process.exitCode = 1;
  } finally {
    save();
    await browser.close();
  }
})().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
