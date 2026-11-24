import assert from 'node:assert/strict';
globalThis.localStorage = { getItem: () => 'isolated-token' };
const { readDocumentSnapshot } = await import('../web/src/utils/documentSnapshot.ts');
const report = 'E-report/build/report.html';
const publication = { id: 'first-version', generation: 1, status: 'committed', current: true, stale: false };
const checks = [];
let calls = [];
function serve(responses) {
  calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push(url);
    assert.equal(options.headers.Authorization, 'Bearer isolated-token');
    const response = responses.shift();
    assert.ok(response, 'Unexpected fallback request: '+url);
    return response;
  };
}
serve([Response.json({ publication }), new Response('original version')]);
let snapshot = await readDocumentSnapshot(7, report);
assert.match(calls[1], /publication_id=first-version$/);
assert.equal(await snapshot.original.text(), 'original version');
globalThis.fetch = () => { throw new Error('Download must not refetch'); };
assert.equal(await snapshot.original.text(), 'original version');
checks.push('publication_selection_pinned_and_original_reusable_without_fetch');

serve([Response.json({ publication: { ...publication, stale: true } }), new Response('historical')]);
snapshot = await readDocumentSnapshot(7, report);
assert.equal(snapshot.publication.stale, true);
checks.push('stale_report_remains_readable_and_labeled');

for (const status of [403, 409, 500]) {
  serve([new Response('', {status})]);
  await assert.rejects(readDocumentSnapshot(7, report), /无法核验报告版本/);
  assert.equal(calls.length, 1);
}
checks.push('selection_errors_never_fall_back_to_alias');
serve([Response.json({ publication }), new Response('', {status: 404})]);
await assert.rejects(readDocumentSnapshot(7, report), /报告版本 1 读取失败/);
assert.equal(calls.length, 2);
checks.push('lost_pinned_version_never_falls_back_to_alias');
serve([Response.json({ publication: { id: 'broken' } })]);
await assert.rejects(readDocumentSnapshot(7, report), /发布版本信息无法核验/);
checks.push('malformed_publication_refused');
serve([new Response('', {status: 404}), new Response('draft')]);
snapshot = await readDocumentSnapshot(7, report);
assert.equal(snapshot.publication, null);
assert.equal(await snapshot.original.text(), 'draft');
assert.match(calls[1], /\/raw\?path=/);
checks.push('unpublished_report_reads_workspace_original');
serve([new Response('ordinary')]);
snapshot = await readDocumentSnapshot(7, 'Ref/ordinary.html');
assert.equal(calls.length, 1);
assert.equal(snapshot.publication, null);
assert.equal(snapshot.path, 'Ref/ordinary.html');
assert.equal(await snapshot.original.text(), 'ordinary');
checks.push('ordinary_document_keeps_working');
serve([Response.json({ publication }), new Response(new Uint8Array([80, 75, 1, 2]))]);
snapshot = await readDocumentSnapshot(7, 'E-report/build/report.docx');
assert.deepEqual([...new Uint8Array(await snapshot.original.arrayBuffer())], [80, 75, 1, 2]);
checks.push('docx_original_bytes_preserved');
serve([Response.json({ publication: { ...publication, current:false, stale:true } }), new Response('old pinned report')]);
snapshot = await readDocumentSnapshot(7, report, undefined, publication.id);
assert.match(calls[0], /results\?publication_id=first-version$/);
assert.equal(await snapshot.original.text(), 'old pinned report');
checks.push('explicit_history_selects_same_version_for_metadata_and_bytes');
serve([new Response('', {status:404})]);
await assert.rejects(readDocumentSnapshot(7, report, undefined, 'missing-history'), /无法核验报告版本/);
assert.equal(calls.length, 1);
checks.push('missing_explicit_history_never_falls_back_to_current_or_workspace');
serve([Response.json({publication})]);
await assert.rejects(readDocumentSnapshot(7, report, undefined, 'different-id'), /发布版本信息无法核验/);
assert.equal(calls.length, 1);
checks.push('history_response_cannot_substitute_another_publication');
console.log('DOCUMENT_SNAPSHOT_OK '+JSON.stringify({ checks, count:checks.length }));
