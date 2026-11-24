import assert from 'node:assert/strict';
import { readNativeEvents, acceptNativeEvent } from '../web/src/utils/nativeChat.ts';
const expected = ['turn.admitted','turn.started','stream.done'].map((type,index) => ({
  type, turn_id:'t', seq:index+1, payload:{note:'状态 '+(index+1)},
}));
const encoded = new TextEncoder().encode(': heartbeat\r\n\r\n'+expected.map(e=>'id: '+e.seq+'\r\ndata: '+JSON.stringify(e)+'\r\n\r\n').join(''));
// Split CRLF and every UTF-8 multibyte character across separate chunks.
let events=[];
await readNativeEvents(new Response(new ReadableStream({start(controller){
  for(const byte of encoded) controller.enqueue(new Uint8Array([byte])); controller.close();
}})), e=>{ events=acceptNativeEvent(events,e,'t'); });
assert.deepEqual(events,expected);
assert.equal(acceptNativeEvent(events,expected[2],'t'),events);
assert.throws(()=>acceptNativeEvent(events,{...expected[2],seq:5},'t'),/不连续/);
assert.throws(()=>acceptNativeEvent(events,{...expected[2],turn_id:'another'},'t'),/归属/);
assert.throws(()=>acceptNativeEvent(events,{type:'execution.unavailable'},'t'),/恢复/);
let incomplete=[];
await readNativeEvents(new Response('data: '+JSON.stringify(expected[0])),e=>incomplete.push(e));
assert.equal(incomplete.length,0);
let multiline=[];
await readNativeEvents(new Response('data: {"type":"turn.admitted",\ndata: "seq":1}\n\n'),e=>multiline.push(e));
assert.equal(multiline[0].seq,1);
await assert.rejects(readNativeEvents(new Response('data: invalid\n\n'),()=>{}));
console.log(JSON.stringify({passed:true,count:8,checks:['utf8_and_crlf_chunk_boundaries','sequence_deduplication','gap_rejected','foreign_turn_rejected','unavailable_not_success','incomplete_eof_replayed','multiline_data','invalid_json_not_swallowed']}));
