const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

const script = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8').split('<script>')[1].split('</script>')[0]
  .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
const record = (type, payload, ms = 0) => ({type, payload, timestamp: new Date(ms).toISOString()});
const ctx = id => record('turn_context', {turn_id:id, model:'test', model_context_window:200});
const message = (role, text, id = 't1', ms = 0) => record('response_item', {type:'message', role,
  content:[{type:role === 'user' ? 'input_text' : 'output_text', text}],
  internal_chat_message_metadata_passthrough:{turn_id:id}}, ms);
const records = [record('session_meta', {id:'thread', cwd:'/project', dynamic_tools:[]}), ctx('t1'),
  message('user', '中文🙂', 't1', 1),
  record('response_item', {type:'custom_tool_call', name:'exec', call_id:'call1', input:'text("ok")'}, 2),
  record('token_usage_record', {turn_id:'t1', response_id:'r1', usage:{input_tokens:10, cached_input_tokens:5, output_tokens:2, total_tokens:12}}, 3),
  message('user', 'second input', 't1', 20),
  record('token_usage_record', {turn_id:'t1', response_id:'r2', usage:{input_tokens:20, cached_input_tokens:10, output_tokens:3, total_tokens:23}}, 21),
  message('assistant', 'reply', 't1', 25), ctx('t2'), message('user', 'third input', 't2', 30),
  record('response_item', {type:'custom_tool_call_output', call_id:'call1', output:'late output',
    internal_chat_message_metadata_passthrough:{turn_id:'t1'}}, 40),
  record('event_msg', {type:'item_completed', turn_id:'t1', item:{id:'call1', type:'CommandExecution',
    duration:{secs:0, nanos:1234000}, exit_code:0, aggregated_output:'done'}}, 45)];
const fixture = records.map(JSON.stringify).join('\n') + '\n';

function viewer() {
  const boxes = new Map(), timeline = [{dataset:{t:'0'}}];
  const box = selector => {
    if(selector.startsWith('.turn[')) return null;
    if(!boxes.has(selector)) boxes.set(selector, {innerHTML:'', textContent:'', scrollTop:0, scrollLeft:0,
      querySelectorAll:query => selector === '#tl' && query === 'i' ? timeline : [], querySelector:() => null});
    return boxes.get(selector);
  };
  const context = vm.createContext({console, fixture, records, TextDecoder, AbortController, setTimeout, clearTimeout,
    localStorage:{getItem:() => null},
    document:{querySelector:box, querySelectorAll:() => []}});
  const run = code => vm.runInContext(code, context);
  run(script);
  return {context, run, box, timeline};
}

test('timeline navigation expands only the requested round and collapse-all remains effective', () => {
  const v = viewer(); v.run('S.parsed=parseRollout(fixture);S.expanded=new Set();renderInsp=()=>{};renderTimeline();');
  v.timeline[0].onclick();
  assert.equal((v.box('#stream').innerHTML.match(/class="turn-b"/g) || []).length, 1);
  v.run('renderFilterBar()'); v.box('#colall').onclick();
  assert.equal((v.box('#stream').innerHTML.match(/class="turn-b"/g) || []).length, 0);
});

test('all decoded chunk boundaries preserve complete records, late associations, usage and additive times', () => {
  const v = viewer(), expected = v.run('JSON.stringify(parseRollout(fixture))');
  for(const width of [1, 7, 64, 1024]) {
    v.context.width = width;
    v.run('var stream=createRolloutStream();for(let i=0;i<fixture.length;i+=width)stream.feed(fixture.slice(i,i+width));stream.finish();');
    assert.equal(v.run('JSON.stringify(stream.build())'), expected);
    assert.equal(v.run('stream.records.length'), records.length);
    assert.equal(v.run('stream.pending'), '');
    assert.equal(v.run('stream.canAppend'), true);
  }
  v.run('var parsed=stream.build()');
  assert.equal(v.run('parsed.totals.durMs'), v.run('parsed.turns.reduce((n,t)=>n+t.spanMs,0)'));
  assert.equal(v.run('parsed.totals.toolMs'), v.run('parsed.turns.reduce((n,t)=>n+t.toolMs,0)'));
});

test('complete EOF without newline is included while incomplete EOF waits for a later append', () => {
  const v = viewer();
  v.run('var complete=createRolloutStream();complete.feed(fixture.trimEnd());complete.finish();');
  assert.equal(v.run('JSON.stringify(complete.build())'), v.run('JSON.stringify(parseRollout(fixture))'));
  assert.equal(v.run('complete.canAppend'), false);
  assert.throws(() => v.run('complete.feed("\\n")'), /末行|快照/);
  v.run('var partial=createRolloutStream();partial.feed(fixture+\'{"type":"turn_context","payload":{"turn_id":"t3"}\');partial.finish();');
  assert.equal(v.run('partial.records.length'), records.length);
  assert.equal(v.run('partial.canAppend'), true);
  v.run('partial.feed("}\\n");partial.finish();');
  assert.equal(v.run('partial.build().turns.length'), 4);
});

test('forked append transactions share old record objects and never reparse or mutate the old snapshot', () => {
  const v = viewer();
  v.run('var original=createRolloutStream();original.feed(fixture);original.finish();var next=original.fork();var first=original.records[0];var parseCount=0;var parseJSON=JSON.parse;JSON.parse=function(...args){parseCount++;return parseJSON.apply(this,args)};');
  v.context.added = JSON.stringify(message('assistant', 'new reply', 't2', 55)) + '\n';
  v.run('next.feed(added);next.finish();JSON.parse=parseJSON;var rebuilt=next.build();');
  assert.equal(v.run('parseCount'), 1);
  assert.equal(v.run('next.records[0]===first'), true);
  assert.equal(v.run('original.records.length'), records.length);
  assert.equal(v.run('next.records.length'), records.length + 1);
  assert.equal(v.run('rebuilt.turns.at(-1).steps.at(-1).text'), 'new reply');
});

test('CRLF, empty and malformed complete lines preserve the existing valid-record policy', () => {
  const v = viewer(); v.context.mixed = '\r\ninvalid\n{broken}\n' + fixture.replaceAll('\n', '\r\n') + '{"unfinished":';
  v.run('var stream=createRolloutStream();for(let i=0;i<mixed.length;i+=17)stream.feed(mixed.slice(i,i+17));stream.finish();');
  assert.equal(v.run('JSON.stringify(stream.build())'), v.run('JSON.stringify(parseRollout(fixture))'));
  assert.equal(v.run('stream.pending'), '{"unfinished":');
});

test('long records span pages without parsing fragments or dropping message and output detail', () => {
  const v = viewer();
  const body = '中文🙂'.repeat(80000) + 'END_OF_LONG_MESSAGE';
  const output = 'result'.repeat(60000) + 'END_OF_LONG_OUTPUT';
  const raw = [ctx('t1'), message('user', body),
    record('response_item', {type:'function_call', name:'run', call_id:'long', arguments:'x'.repeat(60000)}),
    record('response_item', {type:'function_call_output', call_id:'long', output})].map(JSON.stringify).join('\n') + '\n';
  v.context.raw = raw;
  v.run('var stream=createRolloutStream();var parses=0;var originalParse=JSON.parse;JSON.parse=function(...args){parses++;return originalParse.apply(this,args)};for(let i=0;i<raw.length;i+=4096)stream.feed(raw.slice(i,i+4096));stream.finish();JSON.parse=originalParse;var result=stream.build();');
  assert.equal(v.run('parses'), 4);
  assert.equal(v.run('result.turns[0].steps[0].text'), body);
  assert.equal(v.run('result.turns[0].steps[0].raw.payload.content[0].text'), body);
  assert.equal(v.run('result.turns[0].steps[1].args.length'), 60000);
  assert.equal(v.run('result.turns[0].steps[1].out'), output);
  assert.equal(v.run('result.turns[0].steps[1].outputRecords[0].payload.output'), output);
});

test('forked incomplete lines commit independently and repeated semantic builds leave raw records intact', () => {
  const v = viewer();
  v.run('var stream=createRolloutStream();stream.feed(fixture+\'{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"\');var copy=stream.fork();');
  const previous = v.run('stream.pending');
  v.context.added = 'late message"}]}}\n';
  v.run('copy.feed(added);copy.finish();var saved=JSON.stringify(stream.records);function freezeTree(value){if(value&&typeof value==="object"){Object.freeze(value);Object.values(value).forEach(freezeTree)}}stream.records.forEach(freezeTree);var first=stream.build();var second=stream.build();');
  assert.equal(v.run('stream.pending'), previous);
  assert.equal(v.run('stream.records.length'), records.length);
  assert.equal(v.run('copy.records.length'), records.length + 1);
  assert.equal(v.run('JSON.stringify(stream.records)'), v.run('saved'));
  assert.equal(v.run('JSON.stringify(first)'), v.run('JSON.stringify(second)'));
});

test('collapsed rounds render only headers and hidden filters retain selection when a round is expanded', () => {
  const v = viewer();
  v.run('S.parsed=parseRollout(fixture);S.expanded=new Set([2]);S.sel={ti:2,idx:0};S.selTurn=2;renderStream();');
  assert.equal((v.box('#stream').innerHTML.match(/class="turn-h"/g) || []).length, 3);
  assert.equal((v.box('#stream').innerHTML.match(/class="turn-b"/g) || []).length, 1);
  assert.match(v.box('#stream').innerHTML, /class="step k-user sel" data-step="0" data-turn="2"/);
  assert.ok(!v.box('#stream').innerHTML.includes('second input'));
  v.run('S.expanded.add(1);S.hide.add("msg");renderStream();');
  assert.equal((v.box('#stream').innerHTML.match(/class="turn-b"/g) || []).length, 2);
  assert.ok(!v.box('#stream').innerHTML.includes('third input'));
  assert.match(v.box('#stream').innerHTML, /已隐藏 1 条/);
});

function source(text, width = 64) {
  const bytes = Buffer.from(text), state = {reads:0, cancelled:0, released:0}; let offset = 0;
  const reader = {
    async read() {
      state.reads++;
      if(offset === bytes.length) return {done:true};
      const value = bytes.subarray(offset, offset + width); offset += value.length;
      return {value, done:false};
    },
    async cancel() { state.cancelled++; },
    releaseLock() { state.released++; }
  };
  return {body:{getReader:() => reader}, state, reader};
}

test('HTTP and local file streams decode UTF-8 across byte boundaries without allocating a full text response', async () => {
  for(const mode of ['server', 'local']) {
    const v = viewer(), input = source(fixture.trimEnd(), 5);
    v.context.input = input;
    v.context.fetch = async () => ({ok:true, headers:{get:() => '0'}, body:input.body,
      text() { throw new Error('full response text must not be read'); }});
    v.context.file = {stream:() => input.body, text() { throw new Error('full file text must not be read'); }};
    v.run(`S.mode=${JSON.stringify(mode)};S.loadToken=1;`);
    v.context.loaded = await v.run('loadParsed({path:"/self",_file:file},1)');
    assert.equal(v.run('JSON.stringify(loaded)'), v.run('JSON.stringify(parseRollout(fixture))'));
    assert.ok(input.state.reads > 100);
    assert.equal(input.state.cancelled, 0);
    assert.equal(input.state.released, 1);
  }
});

test('complete text fallback remains available when response or File streaming is unsupported', async () => {
  for(const mode of ['server', 'local']) {
    const v = viewer(); v.context.fetch = async () => ({ok:true, headers:{get:() => '0'}, text:async () => fixture});
    v.context.file = {text:async () => fixture}; v.run(`S.mode=${JSON.stringify(mode)};S.loadToken=1;`);
    const parsed = await v.run('loadParsed({path:"/self",_file:file},1)');
    assert.equal(JSON.stringify(parsed), v.run('JSON.stringify(parseRollout(fixture))'));
  }
});

test('an aborted stream cancels its pending read, releases the reader and cannot commit a late result', async () => {
  const v = viewer(), state = {cancelled:0, released:0}; let finishRead;
  const reader = {read:() => new Promise(resolve => {finishRead=resolve;}),
    async cancel() { state.cancelled++; finishRead?.({done:true}); }, releaseLock() { state.released++; }};
  v.context.fetch = async () => ({ok:true, headers:{get:() => '0'}, body:{getReader:() => reader}});
  v.run('S.mode="server";S.loadToken=1;S.crossed=true;var controller=new AbortController();');
  const loading = v.run('loadParsed({path:"/self"},1,controller.signal)');
  await new Promise(resolve => setImmediate(resolve)); v.run('controller.abort()');
  await assert.rejects(loading, error => error.name === 'AbortError');
  assert.equal(state.cancelled, 1); assert.equal(state.released, 1);
  assert.equal(v.run('S.crossed'), true); assert.equal(v.run('S.parsed'), null);
});

test('stale loads and reader errors release resources and retain the previously parsed snapshot', async () => {
  for(const stale of [true, false]) {
    const v = viewer(), input = source(fixture);
    input.reader.read = async () => {
      if(stale) { v.run('S.loadToken++'); return {done:false, value:Buffer.from(fixture)}; }
      throw new Error('disk read failed');
    };
    v.context.fetch = async () => ({ok:true, headers:{get:() => '0'}, body:input.body});
    v.run('S.mode="server";S.loadToken=1;S.parsed=parseRollout(fixture);var saved=S.parsed;');
    await assert.rejects(v.run('loadParsed({path:"/self"},1)'), stale ? /已取消/ : /disk read failed/);
    assert.equal(input.state.cancelled, 1); assert.equal(input.state.released, 1);
    assert.equal(v.run('S.parsed===saved'), true);
  }
});
