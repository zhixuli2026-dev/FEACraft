import { check, sleep } from 'k6';
import execution from 'k6/execution';
import http from 'k6/http';
import { Trend } from 'k6/metrics';

const accounts = JSON.parse(open('/tmp/accounts.json'));
const base = __ENV.VIBEFLOW_MIXED_BASE;
const start = Number(__ENV.VIBEFLOW_MIXED_START);
if (!/^http:\/\/vf-j14-roll-[0-9a-f]{12}-web\/api\/v1$/.test(base)) {
  throw new Error('Run only against a disposable mixed-load ingress');
}
const begun = new Trend('mixed_read_started_epoch_ms');
const ended = new Trend('mixed_read_ended_epoch_ms');
const exchanges = new Trend('mixed_http_exchange_ms');
export const options = {
  scenarios: { reads: { executor: 'per-vu-iterations', vus: accounts.length, iterations: 6, maxDuration: '3m' } },
  thresholds: { http_req_failed: ['rate==0'], checks: ['rate==1'] },
  summaryTrendStats: ['count', 'min', 'max', 'p(95)'],
};

export default function read() {
  const account = accounts[execution.vu.idInTest - 1];
  const headers = { Authorization: `Bearer ${account.token}` };
  const pending = start - Date.now();
  if (pending > 0) sleep(pending / 1000);
  begun.add(Date.now());
  const accountAt = Date.now();
  const me = http.get(base + '/auth/me', { headers, tags: { name: 'account' } });
  const accountDone = Date.now();
  if (check(me, { 'account identity': (response) => response.status === 200 && response.json('email') === account.email })) {
    exchanges.add(accountDone - accountAt, { began_ms: String(accountAt) });
  }
  const treeAt = Date.now();
  const tree = http.get(base + `/cases/${account.id}/tree`, { headers, tags: { name: 'tree' } });
  const treeDone = Date.now();
  if (check(tree, { 'case storage': (response) => response.status === 200 && !!response.json('entries').length })) {
    exchanges.add(treeDone - treeAt, { began_ms: String(treeAt) });
  }
  ended.add(Date.now());
  sleep(0.1);
}

export function handleSummary(data) {
  return { '/tmp/mixed-rest-summary.json': JSON.stringify(data.metrics) };
}
