// FF-J21 authenticated REST reads in a disposable API. Timing excludes setup requests.
import { check, fail, sleep } from 'k6';
import execution from 'k6/execution';
import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';

const users = Number(__ENV.VIBEFLOW_K6_READ_USERS || '50');
if (![1, 10, 50].includes(users)) throw new Error('Use 1, 10, or 50 users for the REST comparison');
const rounds = 6;
const measured = new Counter('vibeflow_read_requests');
const readTime = new Trend('vibeflow_read_duration_ms', true);
const accountTime = new Trend('vibeflow_read_account_ms', true);
const treeTime = new Trend('vibeflow_read_tree_ms', true);
const waitingTime = new Trend('vibeflow_read_waiting_ms', true);
const connectTime = new Trend('vibeflow_read_connecting_ms', true);
const base = __ENV.VIBEFLOW_K6_BASE || '';

export const options = {
  scenarios: {
    authenticated_reads: {
      executor: 'per-vu-iterations',
      vus: users,
      iterations: rounds,
      maxDuration: '2m',
    },
  },
  setupTimeout: '2m',
  thresholds: {
    'http_req_failed{phase:read}': ['rate==0'],
    'checks{phase:read}': ['rate==1'],
    vibeflow_read_requests: [`count==${users * rounds * 2}`],
  },
  summaryTrendStats: ['count', 'med', 'p(95)', 'p(99)', 'max'],
};

function accountRequests(page, build) {
  return http.batch(page.map(build)).map((response) => {
    if (response.status !== 200) fail(`Fixture setup returned HTTP ${response.status}`);
    return response.json();
  });
}

function post(route, body, token) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  return ['POST', base + route, JSON.stringify(body), { headers, tags: { phase: 'setup', name: route } }];
}

export function setup() {
  if (!/^http:\/\/(?:127\.0\.0\.1|vf-j21-linux-[0-9a-f]{32}-api):[1-9][0-9]{0,4}\/api\/v1$/.test(base)) {
    fail('VIBEFLOW_K6_BASE must identify an isolated local API');
  }
  const run = Date.now();
  const accounts = [];
  for (let offset = 0; offset < users; offset += 4) {
    const length = Math.min(4, users - offset);
    const emails = Array.from({ length }, (_, i) => `load-k6-${run}-${offset + i}@example.com`);
    const registered = accountRequests(emails, (email) => post('/auth/register', {
      email,
      password: 'isolated-load-k6-123',
      name: 'k6 read participant',
    }));
    const workspaces = accountRequests(registered, ({ token }) => post('/workspaces', { name: 'Read load' }, token));
    const cases = accountRequests(workspaces, ({ id }, index) =>
      post(`/workspaces/${id}/cases`, { name: 'Read load case' }, registered[index].token));
    accounts.push(...cases.map(({ id }, index) => ({ id, email: emails[index], token: registered[index].token })));
  }
  return accounts;
}

export default function reads(accounts) {
  const account = accounts[execution.vu.idInTest - 1];
  const headers = { Authorization: `Bearer ${account.token}` };
  const accountInfo = http.get(`${base}/auth/me`, { headers, tags: { phase: 'read', name: 'account' } });
  check(accountInfo, { 'own account returned': (r) => r.status === 200 && r.json('email') === account.email },
    { phase: 'read' });
  const tree = http.get(`${base}/cases/${account.id}/tree`, { headers, tags: { phase: 'read', name: 'own_tree' } });
  const entries = tree.status === 200 ? tree.json('entries') : null;
  check(entries, { 'own tree contains entries': (rows) => Array.isArray(rows) && rows.length > 0 }, { phase: 'read' });
  accountTime.add(accountInfo.timings.duration);
  treeTime.add(tree.timings.duration);
  for (const response of [accountInfo, tree]) {
    readTime.add(response.timings.duration);
    waitingTime.add(response.timings.waiting);
    connectTime.add(response.timings.connecting);
  }
  measured.add(2);
  sleep(.1);
}

export function handleSummary(data) {
  const { metrics, options, state } = data;
  return { '/tmp/vibeflow-k6-result.json': JSON.stringify({ metrics, options, state }, null, 2) };
}
