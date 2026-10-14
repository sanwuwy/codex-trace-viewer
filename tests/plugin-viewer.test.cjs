const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

const root = path.join(__dirname, '..');
const main = fs.readFileSync(path.join(root, 'index.html'), 'utf8').split('<script>')[1].split('</script>')[0]
  .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
// Keep the full-source protocol regression suite; production uses the outline path tested separately.
const adapter = fs.readFileSync(path.join(root, 'plugins/codex-trace-probe/viewer-bridge.js'), 'utf8')
  .replace("const snapshotFormat = 'outline';", "const snapshotFormat = 'full';");
const ids = ['aaaaaaaa-bbbb-cccc-dddd-000000000001', 'aaaaaaaa-bbbb-cccc-dddd-000000000002'];
const snapshot = '11111111-2222-3333-4444-555555555555';
const server = '22222222-3333-4444-5555-666666666666';
const tick = () => new Promise(resolve => setImmediate(resolve));
const chunkBytes = 384 * 1024;
const byteSize = text => Buffer.byteLength(text, 'utf8');
const record = (type, payload) => JSON.stringify({timestamp:'2026-10-12T08:00:00Z', type, payload}) + '\n';
const raw = (thread = ids[0], message = 'hello') => record('session_meta', {id:thread, cwd:'/project'}) +
  record('turn_context', {turn_id:'t1'}) + record('response_item', {type:'message', id:'m1', role:'user',
    content:[{type:'input_text', text:message}], internal_chat_message_metadata_passthrough:{turn_id:'t1'}});

function page(viewId = ids[0], initialVisible = true) {
  const boxes = new Map(), listeners = new Map(), documentListeners = new Map(), timers = new Map(), polls = new Map(), calls = [], warnings = [];
  let serial = 0, timerSerial = 0, pollSerial = 0, responseSerial = 0, intersection;
  const box = selector => {
    if(!boxes.has(selector)) boxes.set(selector, {innerHTML:'', textContent:'', value:'', scrollTop:0, scrollLeft:0,
      style:{}, dataset:{}, classList:{add() {}, remove() {}, toggle() {}},
      querySelectorAll:() => [], querySelector:() => null});
    return boxes.get(selector);
  };
  box('#viewer-config').textContent = '{"thread_id":null}';
  const parent = {postMessage(message) { calls.push(JSON.parse(JSON.stringify(message))); }};
  const windowCallbacks = new Map(), documentCallbacks = new Map();
  const window = {parent, addEventListener:(name, fn) => {
      if(!windowCallbacks.has(name)) windowCallbacks.set(name,new Set());
      windowCallbacks.get(name).add(fn);
      listeners.set(name,(...args)=>{for(const callback of [...windowCallbacks.get(name)]) callback(...args);});
    }, removeEventListener:(name, fn) => {
      windowCallbacks.get(name)?.delete(fn);
      if(!windowCallbacks.get(name)?.size) listeners.delete(name);
    }};
  const context = vm.createContext({window, document:{hidden:false, querySelector:box, querySelectorAll:() => [],
      documentElement:{dataset:{}, style:{setProperty() {}}}, body:box('body'),
      addEventListener:(name, fn) => {
        if(!documentCallbacks.has(name)) documentCallbacks.set(name,new Set());
        documentCallbacks.get(name).add(fn);
        documentListeners.set(name,(...args)=>{for(const callback of [...documentCallbacks.get(name)]) callback(...args);});
      }, removeEventListener:(name, fn)=>documentCallbacks.get(name)?.delete(fn)},
    IntersectionObserver:class {
      constructor(callback, options={}) {
        const thresholds=[...(Array.isArray(options.threshold)?options.threshold:[options.threshold??0])].sort((a,b)=>a-b);
        let previousIndex=-1, previousIntersecting=false;
        intersection=visible=>{
          const entry=typeof visible==="object"?visible:{isIntersecting:visible,intersectionRect:{width:visible?100:0,height:visible?100:0}};
          const ratio=entry.intersectionRatio??entry.intersectionRect.width*entry.intersectionRect.height/10000;
          const next=thresholds.findIndex(threshold=>threshold>ratio),index=next<0?thresholds.length:next;
          // Native notification rule: thresholdIndex or isIntersecting must change.
          // https://www.w3.org/TR/intersection-observer/#update-intersection-observations-algo
          if(index!==previousIndex || entry.isIntersecting!==previousIntersecting) {
            previousIndex=index;previousIntersecting=entry.isIntersecting;callback([entry]);
          }
        };
      }
      observe() { if(initialVisible!==null) intersection(initialVisible); }
      disconnect() { intersection=()=>{}; }
    }, localStorage:{getItem:() => null}, console:{...console, warn:(...args)=>warnings.push(args.join(" "))}, URL, AbortController, TextDecoder, Uint8Array, atob, performance,
    crypto:{randomUUID:() => serial++ === 0 ? viewId : '99999999-8888-7777-6666-' + String(serial).padStart(12, '0')},
    setTimeout:(fn, ms) => { const id = ++timerSerial; if(ms===0) setImmediate(fn); else timers.set(id, {fn, ms}); return id; },
    clearTimeout:id => timers.delete(id), setInterval:(fn, ms) => {const id = ++pollSerial; polls.set(id, {fn, ms}); return id;},
    clearInterval:id => polls.delete(id), fetch() { throw new Error('HTTP/network access forbidden'); }});
  const run = code => vm.runInContext(code, context);
  run(main);
  run('const savedRenderInsp=renderInsp; for(const name of ["renderList","renderSessionBar","renderTimeline","renderFilterBar","renderStream","renderTabs","renderInsp"]) eval(name+"=()=>{}");');
  run(adapter);
  const emit = (message, source = parent) => listeners.get('message')?.({source, data:message});
  const toolCalls = name => calls.filter(c => c.method === 'tools/call' && (!name || c.params.name === name));
  const data = (call, extra = {}) => ({view_id:call.params.arguments.view_id, request_nonce:call.params.arguments.request_nonce,
    thread_id:ids[0], identity_status:'verified', identity_source:'_meta.threadId', snapshot_match:true,
    server_instance_id:server, request_id:++responseSerial, ...extra});
  const reply = (call, extra = {}) => emit({jsonrpc:'2.0', id:call.id, result:{structuredContent:data(call, extra)}});
  return {context, run, box, calls, listeners, documentListeners, timers, polls, warnings, emit, toolCalls, reply, data,
    visible:value=>intersection(value), hidden:value=>{context.document.hidden=value;documentListeners.get("visibilitychange")?.();},
    async start() {const boot = run('bind();boot()'); const init = calls[0];
      emit({jsonrpc:'2.0', id:init.id, result:{protocolVersion:'2026-01-26'}}); await tick(); return {boot, call:toolCalls('trace_snapshot').at(-1)};},
    ready(call, extra = {}) {reply(call, {status:'ready', session:{path:'/state/self.jsonl', name:'self.jsonl',
      thread_id:ids[0], revision:'revision1'}, snapshot_id:snapshot, total_bytes:byteSize(raw()), start_offset:0,
      chunk_bytes:chunkBytes, total_chunks:1, truncated:false, ...extra});},
    chunk(call, text = raw(), extra = {}) {const bytes = typeof text === 'string' ? Buffer.from(text, 'utf8') : text;
      reply(call, {snapshot_id:snapshot, revision:'revision1', chunk_index:call.params.arguments.chunk_index,
        encoding:'base64', data:bytes.toString('base64'), byte_offset:call.params.arguments.chunk_index*chunkBytes,
        byte_length:bytes.length, done:true, ...extra});},
    async loaded() {const {boot, call} = await this.start(); this.ready(call); await tick(); this.chunk(toolCalls('trace_chunk').at(-1)); await boot;},
    timeout() {const [id, item] = timers.entries().next().value; timers.delete(id); item.fn();}};
}

test('plugin boots through the host, hides global sources before binding and applies the original parser', async () => {
  const p = page(), {boot, call} = await p.start();
  assert.equal(p.calls[0].method, 'ui/initialize');
  assert.equal(p.box('#pick').style.display, 'none');
  assert.notEqual(p.run('S.scope'), null);
  assert.equal(p.run('S.parsed'), null);
  assert.equal('thread_id' in call.params.arguments, false);
  p.ready(call); await tick();
  p.chunk(p.toolCalls('trace_chunk').at(-1)); await boot;
  assert.equal(p.run('S.scope'), ids[0]);
  assert.equal(p.run('S.parsed.meta.id'), ids[0]);
  assert.equal(p.run('S.parsed.turns[0].steps[0].text'), 'hello');
  assert.equal(p.run('S.current.stats.turns'), 1);
  assert.equal(p.polls.size, 1);
  assert.match(p.box('#live-status').textContent, /插件|本会话/);
});

test('waiting retains pending scope and binds only a verified ready snapshot', async () => {
  const p = page(), {boot, call} = await p.start();
  p.reply(call, {identity_status:'candidate', snapshot_match:null, status:'waiting', session:null,
    snapshot_id:null, total_chunks:0, truncated:false}); await boot;
  assert.notEqual(p.run('S.scope'), ids[0]);
  assert.equal(p.run('S.parsed'), null);
  assert.match(p.box('#live-status').textContent, /等待/);
  const refresh = p.run('refreshScoped()'); await tick(); p.ready(p.toolCalls('trace_snapshot').at(-1)); await tick();
  p.chunk(p.toolCalls('trace_chunk').at(-1)); await refresh;
  assert.equal(p.run('S.scope'), ids[0]);
});

test('unchanged snapshot avoids raw reads, parsing, statistics writes and global sidebar work', async () => {
  const p = page(); await p.loaded(); const parsed = p.run('S.parsed');
  p.run('buildTree=()=>{throw new Error("tree")}; renderList=()=>{throw new Error("sidebar")}; const beforeParse=parseRollout;parseRollout=()=>{throw new Error("unnecessary parse")};');
  const refresh = p.run('refreshScoped()'); await tick(); const call = p.toolCalls('trace_snapshot').at(-1);
  assert.equal(call.params.arguments.known_revision, 'revision1');
  p.reply(call, {status:'unchanged', session:{path:'/state/self.jsonl', name:'self.jsonl', thread_id:ids[0], revision:'revision1'},
    snapshot_id:null, total_chunks:0, total_bytes:byteSize(raw()), start_offset:byteSize(raw()), truncated:false}); await refresh;
  assert.equal(p.run('S.parsed'), parsed);
  assert.equal(p.toolCalls('trace_chunk').length, 1);
  assert.match(p.box('#live-status').textContent, /完整记录/);
});

test('two page instances reject crossed replies and preserve their own verified identity', async () => {
  const a = page(ids[0]), b = page(ids[1]);
  const [{boot:ab, call:ac}, {boot:bb, call:bc}] = await Promise.all([a.start(), b.start()]);
  b.emit({jsonrpc:'2.0', id:ac.id, result:{structuredContent:a.data(ac)}});
  a.emit({jsonrpc:'2.0', id:bc.id, result:{structuredContent:b.data(bc)}});
  await tick(); assert.equal(a.run('S.parsed'), null); assert.equal(b.run('S.parsed'), null);
  a.ready(ac); b.ready(bc, {thread_id:ids[1], session:{path:'/state/other.jsonl', name:'other.jsonl', thread_id:ids[1], revision:'revision1'}});
  await tick(); a.chunk(a.toolCalls('trace_chunk').at(-1)); b.chunk(b.toolCalls('trace_chunk').at(-1), raw(ids[1]), {thread_id:ids[1]});
  await Promise.all([ab, bb]);
  assert.equal(a.run('S.parsed.meta.id'), ids[0]); assert.equal(b.run('S.parsed.meta.id'), ids[1]);
  const old = a.run('S.parsed'), refresh = a.run('refreshScoped()'); await tick();
  a.ready(a.toolCalls('trace_snapshot').at(-1), {thread_id:ids[1]}); await refresh;
  assert.equal(a.run('S.parsed'), old); assert.match(a.box('#live-status').textContent, /身份已改变/);
});

test('the parent bridge and independently owned nonce are required; unsolicited tool notifications do nothing', async () => {
  for(const change of [{view_id:ids[1]}, {request_nonce:snapshot}]) {
    const p = page(), {boot, call} = await p.start();
    p.emit({jsonrpc:'2.0', id:call.id, result:{structuredContent:p.data(call)}}, {});
    p.emit({jsonrpc:'2.0', method:'ui/notifications/tool-result', params:{structuredContent:p.data(call)}});
    assert.equal(p.run('S.parsed'), null);
    p.ready(call, change); await boot;
    assert.equal(p.run('S.parsed'), null); assert.match(p.box('#live-status').textContent, /标识不一致/);
    assert.equal(p.toolCalls('trace_chunk').length, 0);
  }
});

test('candidate, malformed pagination and mismatched session identity cannot commit a snapshot', async () => {
  for(const change of [{identity_status:'candidate', snapshot_match:null}, {total_chunks:1.5}, {snapshot_id:'bad'},
    {session:{path:'/other', name:'other', thread_id:ids[1], revision:'revision1'}}]) {
    const p = page(), {boot, call} = await p.start(); p.ready(call, change); await boot;
    assert.equal(p.run('S.parsed'), null); assert.equal(p.toolCalls('trace_chunk').length, 0);
    assert.match(p.box('#live-status').textContent, /失败/);
  }
});

test('chunk revision, identity, server, ordering, completion and content size all gate atomic display', async () => {
  for(const change of [{revision:'other'}, {thread_id:ids[1]}, {server_instance_id:snapshot}, {chunk_index:1},
    {done:false}, {data:'x'.repeat(524289)}, {data:Buffer.from(raw(ids[1])).toString('base64')},
    {byte_offset:1}, {byte_length:1}, {encoding:'utf8'}]) {
    const p = page(); await p.loaded(); const old = p.run('S.parsed');
    const refresh = p.run('refreshScoped()'); await tick();
    p.ready(p.toolCalls('trace_snapshot').at(-1)); await tick();
    p.chunk(p.toolCalls('trace_chunk').at(-1), raw(), change); await refresh;
    assert.equal(p.run('S.parsed'), old); assert.match(p.box('#live-status').textContent, /失败/);
  }
});

test('byte chunks crossing UTF-8 sequences preserve long records without slice warnings', async () => {
  const p = page(), {boot, call} = await p.start();
  const text = raw(ids[0], '🙂'.repeat(100000)), bytes = Buffer.from(text);
  p.ready(call, {total_bytes:bytes.length, total_chunks:Math.ceil(bytes.length/chunkBytes)}); await tick();
  p.chunk(p.toolCalls('trace_chunk').at(-1), bytes.subarray(0, chunkBytes), {done:false}); await tick();
  assert.equal(p.run('S.parsed'), null);
  p.chunk(p.toolCalls('trace_chunk').at(-1), bytes.subarray(chunkBytes)); await boot;
  assert.equal(p.run('S.parsed.turns[0].steps[0].text'), '🙂'.repeat(100000));
  assert.equal(p.run('S.crossed'), false); assert.doesNotMatch(p.box('#live-status').textContent, /切片/);
});

test('concurrent refreshes share a request, hidden pages do not poll and timeouts permit retries', async () => {
  const p = page(); await p.loaded(); const old = p.run('S.parsed');
  p.context.document.hidden = true; [...p.polls.values()][0].fn();
  assert.equal(p.toolCalls('trace_snapshot').length, 1);
  p.context.document.hidden = false;
  const a = p.run('refreshScoped()'), b = p.run('refreshScoped()'); await tick();
  assert.equal(p.toolCalls('trace_snapshot').length, 2);
  p.timeout(); await Promise.all([a,b]); assert.equal(p.run('S.parsed'), old);
  assert.match(p.box('#live-status').textContent, /10 秒/); assert.equal(p.timers.size, 0);
  const retry = p.run('refreshScoped()'); await tick(); p.ready(p.toolCalls('trace_snapshot').at(-1)); await tick();
  p.chunk(p.toolCalls('trace_chunk').at(-1)); await retry; assert.match(p.box('#live-status').textContent, /实时更新/);
});

test('resource teardown cancels host calls and cannot apply a late chunk', async () => {
  const p = page(), {boot, call} = await p.start(); p.ready(call); await tick();
  const chunk = p.toolCalls('trace_chunk').at(-1);
  p.emit({jsonrpc:'2.0', id:'close', method:'ui/resource-teardown', params:{}}); await boot;
  p.chunk(chunk); await tick();
  const release=p.toolCalls('trace_release').at(-1); p.reply(release,{released:true}); await tick();
  assert.equal(p.run('S.parsed'), null); assert.equal(p.timers.size, 0); assert.equal(p.polls.size, 0);
  assert.ok(p.calls.some(c => c.id === 'close' && c.result));
  assert.ok(p.calls.some(c => c.method === 'notifications/cancelled' && c.params.requestId === chunk.id));
});

test('initialization failures stay scoped and permit an explicit retry without HTTP fallback', async () => {
  const p = page(), boot = p.run('bind();boot()'); const init = p.calls[0];
  p.emit({jsonrpc:'2.0', id:init.id, error:{code:-32000, message:'bridge unavailable'}}); await boot;
  assert.equal(p.run('S.mode'), 'server'); assert.notEqual(p.run('S.scope'), null);
  assert.equal(p.box('#pick').style.display, 'none'); assert.equal(p.toolCalls().length, 0);
  assert.match(p.box('#live-status').textContent, /bridge unavailable/);
  const retry = p.box('#reload').onclick(), next = p.calls.filter(c => c.method === 'ui/initialize').at(-1);
  p.emit({jsonrpc:'2.0', id:next.id, result:{protocolVersion:'2026-01-26'}}); await tick();
  p.ready(p.toolCalls('trace_snapshot').at(-1)); await tick(); p.chunk(p.toolCalls('trace_chunk').at(-1)); await retry;
  assert.equal(p.run('S.parsed.meta.id'), ids[0]);
});

test('schema uses the host and explicitly rejects all unrecognized HTTP and external routes', async () => {
  const p = page(); await p.loaded();
  for(const query of ['/api/sessions?limit=0', '/api/raw?path=/other', 'https://example.com/',
    '/api/toolschema?name=run&path=/other', '/api/toolschema?name=', '/api/toolschema?name=' + 'x'.repeat(257)])
    await assert.rejects(p.run(`fetch(${JSON.stringify(query)})`));
  const read = p.run('fetch("/api/toolschema?name=run&ns=functions")'); await tick();
  const call = p.toolCalls('trace_schema').at(-1);
  assert.deepEqual(Object.keys(call.params.arguments).sort(), ['name','ns','request_nonce','view_id']);
  p.reply(call, {schema:{found:true, name:'run', description:'description', inputSchema:{type:'object'}}});
  const response = await read; assert.equal(response.ok, true); assert.equal((await response.json()).name, 'run');
  const mismatch = p.run('fetch("/api/toolschema?name=run")'); await tick();
  p.reply(p.toolCalls('trace_schema').at(-1), {thread_id:ids[1], schema:{found:false}});
  await assert.rejects(mismatch, /身份已改变/);
});

test('the existing tool Schema inspector renders the adapter result without a separate viewer', async () => {
  const p = page(), {boot, call} = await p.start();
  const text = record('session_meta', {id:ids[0]}) + record('turn_context', {turn_id:'t1'}) +
    record('response_item', {type:'function_call', id:'c1', name:'run', call_id:'call1', arguments:'{}'});
  p.ready(call, {total_bytes:byteSize(text)}); await tick();
  p.chunk(p.toolCalls('trace_chunk').at(-1), text); await boot;
  p.run('renderInsp=savedRenderInsp;S.sel={ti:0,idx:0};S.selTurn=0;S.tab="t_schema";renderInsp()'); await tick();
  p.reply(p.toolCalls('trace_schema').at(-1), {schema:{found:true, name:'run', description:'host cache schema', inputSchema:{type:'object'}}});
  await tick(); assert.match(p.box('#insp').innerHTML, /host cache schema/);
});

test('refreshed snapshots retain the original selected record, expanded output and scroll positions', async () => {
  const p = page(); await p.loaded();
  p.run('S.sel={ti:0,idx:0};S.selTurn=0;S.tab="m_raw";S.expanded=new Set([0]);S.truncMap.set("0:0",true);');
  p.box('#stream').scrollTop = 120; p.box('#insp').scrollTop = 80;
  const refresh = p.run('refreshScoped()'); await tick();
  const appended = raw() + record('response_item', {type:'message', id:'m2', role:'assistant',
    content:[{type:'output_text', text:'new reply'}], internal_chat_message_metadata_passthrough:{turn_id:'t1'}});
  p.ready(p.toolCalls('trace_snapshot').at(-1), {total_bytes:byteSize(appended), session:{path:'/state/self.jsonl', name:'self.jsonl', thread_id:ids[0], revision:'revision2'}});
  await tick(); p.chunk(p.toolCalls('trace_chunk').at(-1), appended, {revision:'revision2'}); await refresh;
  assert.equal(p.run('selectedStep().text'), 'hello'); assert.equal(p.run('S.parsed.turns[0].steps.length'), 2);
  assert.equal(p.run('S.tab'), 'm_raw');
  assert.equal(p.run('[...S.expanded].join(",")'), '0'); assert.equal(p.run('S.truncMap.get("0:0")'), true);
  assert.equal(p.box('#stream').scrollTop, 120); assert.equal(p.box('#insp').scrollTop, 80);
});

async function nextChunk(p, index) {
  for(let wait=0;wait<20;wait++) {
    const call=p.toolCalls('trace_chunk').at(-1);
    if(call?.params.arguments.chunk_index===index) return call;
    await tick();
  }
  assert.fail('missing chunk '+index+': '+p.box('#live-status').textContent);
}

async function deliver(p, bytes, {start=0, size=chunkBytes, revision='revision1', thread=ids[0]} = {}) {
  for(let index=0; index<Math.ceil(bytes.length/size); index++) {
    const chunk = bytes.subarray(index*size, (index+1)*size);
    p.chunk(await nextChunk(p,index), chunk, {chunk_index:index, byte_offset:start+index*size,
      done:(index+1)*size>=bytes.length, revision, thread_id:thread});
    await tick();
  }
}

const changedSession = revision => ({path:'/state/self.jsonl', name:'self.jsonl', thread_id:ids[0], revision});
const appendedMessage = (message='new reply', turn='t1') => record('response_item', {type:'message', id:'m-'+turn,
  role:'assistant', content:[{type:'output_text', text:message}], internal_chat_message_metadata_passthrough:{turn_id:turn}});

test('more than 256 negotiated byte pages load every round atomically and show byte and record progress', async () => {
  const p = page(), {boot, call} = await p.start();
  const text = raw() + Array.from({length:34}, (_,i) => record('turn_context', {turn_id:'t'+(i+2)}) +
    appendedMessage('reply '+i, 't'+(i+2))).join('');
  const bytes = Buffer.from(text), size=23, pages=Math.ceil(bytes.length/size);
  assert.ok(pages>256); p.ready(call, {total_bytes:bytes.length, chunk_bytes:size, total_chunks:pages}); await tick();
  for(let index=0; index<pages; index++) {
    assert.equal(p.run('S.parsed'), null);
    p.chunk(await nextChunk(p,index), bytes.subarray(index*size,(index+1)*size),
      {byte_offset:index*size, done:index===pages-1}); await tick();
    if(index===16) assert.match(p.box('#live-status').textContent, /已读取 \d+\/.+已解析/);
  }
  await boot; assert.equal(p.run('S.parsed.turns.length'), 35);
  assert.equal(p.run('JSON.stringify(S.parsed)'), p.run(`JSON.stringify(parseRollout(${JSON.stringify(text)}))`));
  assert.match(p.box('#live-status').textContent, /完整记录/);
});

test('byte progress is uncapped beyond the former 32 MiB decoded text limit', async () => {
  const p=page(), {boot,call}=await p.start(), first=Buffer.concat([Buffer.from(raw()),
    Buffer.from('#'.repeat(chunkBytes-byteSize(raw())-1)+'\n')]), middle=Buffer.from('#'.repeat(chunkBytes-1)+'\n');
  const pages=87, last=Buffer.from(record('turn_context', {turn_id:'last'})+appendedMessage('last round','last'));
  const total=(pages-1)*chunkBytes+last.length;
  assert.ok(total>32*1024*1024); p.ready(call,{total_bytes:total,total_chunks:pages}); await tick();
  for(let i=0;i<pages;i++) {
    p.chunk(await nextChunk(p,i),i===0?first:i===pages-1?last:middle,{done:i===pages-1}); await tick();
  }
  await boot; assert.equal(p.run('S.parsed.turns.length'),2); assert.equal(p.run('S.parsed.turns[1].steps[0].text'),'last round');
});

test('delta parses only new JSONL records and produces full-record semantics without replacing old raw records', async () => {
  const p=page(); await p.loaded(); const old=p.run('S.parsed'), oldRaw=p.run('S.parsed.turns[0].steps[0].raw');
  p.run('let jsonParses=0; const realJSONparse=JSON.parse;JSON.parse=(...args)=>{jsonParses++;return realJSONparse(...args)};');
  const addition=record('turn_context',{turn_id:'t2'})+appendedMessage('second round','t2'), bytes=Buffer.from(addition);
  const refresh=p.run('refreshScoped()'); await tick(); const call=p.toolCalls('trace_snapshot').at(-1);
  assert.equal(call.params.arguments.known_revision,'revision1');
  p.ready(call,{status:'delta',base_revision:'revision1',session:changedSession('revision2'),start_offset:byteSize(raw()),
    total_bytes:byteSize(raw())+bytes.length,total_chunks:1}); await tick();
  p.chunk(p.toolCalls('trace_chunk').at(-1),bytes,{revision:'revision2',byte_offset:byteSize(raw())}); await refresh;
  assert.equal(p.run('jsonParses'),2); assert.notEqual(p.run('S.parsed'),old);
  assert.equal(p.run('S.parsed.turns[0].steps[0].raw'),oldRaw); assert.equal(p.run('S.parsed.turns.length'),2);
  assert.equal(p.run('JSON.stringify(S.parsed)'),p.run(`JSON.stringify(parseRollout(${JSON.stringify(raw()+addition)}))`));
  const next=p.run('refreshScoped()'); await tick(); assert.equal(p.toolCalls('trace_snapshot').at(-1).params.arguments.known_revision,'revision2');
  p.reply(p.toolCalls('trace_snapshot').at(-1),{status:'unchanged',session:changedSession('revision2'),truncated:false,
    total_bytes:byteSize(raw()+addition),start_offset:byteSize(raw()+addition),total_chunks:0}); await next;
  assert.match(p.box('#live-status').textContent,/完整记录/);
});

test('invalid byte ranges, page counts and truncation never start a partial transfer', async () => {
  for(const change of [{total_bytes:-1},{total_bytes:Number.MAX_SAFE_INTEGER+1},{total_bytes:Infinity},
    {start_offset:-1},{start_offset:1},{chunk_bytes:0},{chunk_bytes:chunkBytes+1},{total_chunks:0},{truncated:true}]) {
    const p=page(), {boot,call}=await p.start(); p.ready(call,change); await boot;
    assert.equal(p.run('S.parsed'),null); assert.equal(p.toolCalls('trace_chunk').length,0);
    assert.match(p.box('#live-status').textContent,/失败/);
  }
});

test('a delta with a different base, byte start or path is rejected and the retry requests a full snapshot', async () => {
  for(const change of [{base_revision:'other'},{start_offset:byteSize(raw())-1},
    {session:{...changedSession('revision2'),path:'/other'}}]) {
    const p=page(); await p.loaded(); const old=p.run('S.parsed');
    const refresh=p.run('refreshScoped()'); await tick();
    p.ready(p.toolCalls('trace_snapshot').at(-1),{status:'delta',base_revision:'revision1',session:changedSession('revision2'),
      start_offset:byteSize(raw()),total_bytes:byteSize(raw())+byteSize(appendedMessage()),...change}); await refresh;
    assert.equal(p.run('S.parsed'),old); assert.equal(p.toolCalls('trace_chunk').length,1);
    const retry=p.run('refreshScoped()'); await tick(); const call=p.toolCalls('trace_snapshot').at(-1);
    assert.equal('known_revision' in call.params.arguments,false); p.ready(call); await tick();
    p.chunk(p.toolCalls('trace_chunk').at(-1)); await retry; assert.match(p.box('#live-status').textContent,/完整记录/);
  }
});

test('failed later delta pages preserve committed records and parsed state without accepting nonce or offset mismatch', async () => {
  for(const bad of [{request_nonce:snapshot},{byte_offset:0},{data:'!!!!'}]) {
    const p=page(); await p.loaded(); const old=p.run('S.parsed'), original=p.run('JSON.stringify(S.parsed)');
    const added=record('turn_context',{turn_id:'t2'})+appendedMessage('x'.repeat(chunkBytes),'t2'), bytes=Buffer.from(added);
    const refresh=p.run('refreshScoped()'); await tick();
    p.ready(p.toolCalls('trace_snapshot').at(-1),{status:'delta',base_revision:'revision1',session:changedSession('revision2'),
      start_offset:byteSize(raw()),total_bytes:byteSize(raw())+bytes.length,total_chunks:2}); await tick();
    p.chunk(p.toolCalls('trace_chunk').at(-1),bytes.subarray(0,chunkBytes),{revision:'revision2',byte_offset:byteSize(raw()),done:false}); await tick();
    assert.equal(p.run('S.parsed'),old); assert.match(p.box('#live-status').textContent,/保留上次完整快照/);
    p.chunk(p.toolCalls('trace_chunk').at(-1),bytes.subarray(chunkBytes),{revision:'revision2',byte_offset:byteSize(raw())+chunkBytes,...bad}); await refresh;
    assert.equal(p.run('S.parsed'),old); assert.equal(p.run('JSON.stringify(S.parsed)'),original);
    assert.equal(p.run('S.current.revision'),'revision1'); assert.match(p.box('#live-status').textContent,/失败.+保留/);
  }
});

test('a yield after 16 pages allows cancellation and cannot replace the last complete snapshot', async () => {
  const p=page(); await p.loaded(); const old=p.run('S.parsed');
  const text=raw(ids[0],'new '.repeat(200)), bytes=Buffer.from(text), size=23;
  const refresh=p.run('refreshScoped()'); await tick();
  p.ready(p.toolCalls('trace_snapshot').at(-1),{session:changedSession('revision2'),total_bytes:bytes.length,
    chunk_bytes:size,total_chunks:Math.ceil(bytes.length/size)}); await tick();
  for(let i=0;i<16;i++) p.chunk(await nextChunk(p,i),bytes.subarray(i*size,(i+1)*size),
    {revision:'revision2',byte_offset:i*size,done:false});
  p.run('stopScopedPolling()'); await refresh;
  assert.equal(p.run('S.parsed'),old); assert.equal(p.run('S.current.revision'),'revision1');
  assert.equal(p.timers.size,0); assert.equal(p.polls.size,0);
});

test('complete non-newline EOF rejects an append delta and retains the last full snapshot', async () => {
  const p=page(), {boot,call}=await p.start(), text=raw().trimEnd();
  p.ready(call,{total_bytes:byteSize(text)}); await tick(); p.chunk(p.toolCalls('trace_chunk').at(-1),text); await boot;
  const old=p.run('S.parsed'), refresh=p.run('refreshScoped()'); await tick();
  p.ready(p.toolCalls('trace_snapshot').at(-1),{status:'delta',base_revision:'revision1',session:changedSession('revision2'),
    start_offset:byteSize(text),total_bytes:byteSize(text)+1}); await refresh;
  assert.equal(p.run('S.parsed'),old); assert.equal(p.toolCalls('trace_chunk').length,1);
  assert.match(p.box('#live-status').textContent,/基线/);
});


test('closing a loaded page immediately drops parsed data and DOM while waiting for scoped release acknowledgement', async () => {
  const p=page(); await p.loaded();
  p.run('S.sel={ti:0,idx:0}; S.selTurn=0; S.schemaRestore={parsed:S.parsed,st:S.parsed.turns[0].steps[0]}; S.expanded.add(0); S.truncMap.set("0:0",true);');
  for(const selector of ['#stream','#insp','#sbar','#mbody','#mtitle','#tabs','#slist','#filter','#tl']) p.box(selector).innerHTML='large retained trace';
  p.emit({jsonrpc:'2.0',id:'close-loaded',method:'ui/resource-teardown',params:{}});
  assert.equal(p.run('S.parsed'),null); assert.equal(p.run('S.current'),null);
  assert.equal(p.run('S.schemaRestore'),null); assert.equal(p.run('S.sel'),null); assert.equal(p.run('S.selTurn'),null);
  assert.equal(p.run('S.sessions.length+S.filtered.length+S.rows.length+S.tree.length+S.expanded.size+S.truncMap.size'),0);
  for(const selector of ['#stream','#insp','#sbar','#mbody','#mtitle','#tabs','#slist','#filter','#tl']) assert.equal(p.box(selector).innerHTML,'');
  assert.equal(p.polls.size,0); assert.equal(p.calls.some(c=>c.id==='close-loaded'&&c.result),false);
  const release=p.toolCalls('trace_release').at(-1); assert.ok(release);
  assert.deepEqual(Object.keys(release.params.arguments).sort(),['request_nonce','view_id']);
  p.reply(release,{released:true}); await tick();
  assert.ok(p.calls.some(c=>c.id==='close-loaded'&&c.result)); assert.equal(p.timers.size,0); assert.equal(p.listeners.has('message'),false);
});


test('closing one of two loaded pages releases only its own view and the other keeps updating', async () => {
  const a=page(ids[0]), b=page(ids[1]);
  const [{boot:ab,call:ac},{boot:bb,call:bc}]=await Promise.all([a.start(),b.start()]);
  a.ready(ac); b.ready(bc,{thread_id:ids[1],session:{path:'/state/other.jsonl',name:'other.jsonl',thread_id:ids[1],revision:'revision1'}});
  await tick(); a.chunk(a.toolCalls('trace_chunk').at(-1)); b.chunk(b.toolCalls('trace_chunk').at(-1),raw(ids[1]),{thread_id:ids[1]});
  await Promise.all([ab,bb]); const parsed=b.run('S.parsed');
  a.emit({jsonrpc:'2.0',id:'close-a',method:'ui/resource-teardown',params:{}});
  const release=a.toolCalls('trace_release').at(-1); assert.equal(release.params.arguments.view_id,ids[0]);
  assert.equal(b.toolCalls('trace_release').length,0); assert.equal(b.run('S.parsed'),parsed); assert.equal(b.polls.size,1);
  a.reply(release,{released:true}); await tick(); assert.equal(a.timers.size,0);
  const refresh=b.run('refreshScoped()'); await tick(); b.reply(b.toolCalls('trace_snapshot').at(-1),{
    thread_id:ids[1],status:'unchanged',session:{path:'/state/other.jsonl',name:'other.jsonl',thread_id:ids[1],revision:'revision1'},
    truncated:false,total_bytes:byteSize(raw(ids[1])),start_offset:byteSize(raw(ids[1])),total_chunks:0}); await refresh;
  assert.equal(b.run('S.parsed'),parsed); assert.match(b.box('#live-status').textContent,/实时更新/);
});

test('teardown acknowledgement has a one-second deadline, logs failure and clears all request timers', async () => {
  const p=page(); await p.loaded(); p.emit({jsonrpc:'2.0',id:'close-timeout',method:'ui/resource-teardown'});
  assert.equal(p.timers.size,1); const timer=[...p.timers.values()][0]; assert.ok(timer.ms<=1000);
  assert.equal(p.calls.some(c=>c.id==='close-timeout'&&c.result),false);
  p.timeout(); await tick();
  assert.ok(p.calls.some(c=>c.id==='close-timeout'&&c.result)); assert.match(p.warnings.join(' '),/定时清理兜底.+1 秒/);
  assert.equal(p.timers.size,0); assert.equal(p.listeners.has('message'),false); assert.equal(p.run('S.parsed'),null);
});

test('pagehide clears data synchronously, deduplicates release and disposed pages cannot restart polling', async () => {
  const p=page(); await p.loaded(); const hide=p.listeners.get('pagehide'); hide(); hide();
  assert.equal(p.run('S.parsed'),null); assert.equal(p.toolCalls('trace_release').length,1);
  const calls=p.calls.length; p.listeners.get('pageshow')(); p.documentListeners.get('visibilitychange')();
  await p.box('#reload').onclick(); await tick(); assert.equal(p.calls.length,calls); assert.equal(p.polls.size,0);
  p.reply(p.toolCalls('trace_release').at(-1),{released:false}); await tick(); assert.equal(p.timers.size,0);
  assert.equal(p.warnings.length,0);
});

test('close during initialization cancels old init, initializes only to release and ignores stale identity replies', async () => {
  const p=page(), boot=p.run('bind();boot()'), oldInit=p.calls[0];
  p.emit({jsonrpc:'2.0',id:'close-init',method:'ui/resource-teardown'});
  assert.equal(p.run('S.parsed'),null); const closeInit=p.calls.filter(c=>c.method==='ui/initialize').at(-1);
  assert.notEqual(closeInit.id,oldInit.id); p.emit({jsonrpc:'2.0',id:oldInit.id,result:{protocolVersion:'2026-01-26'}});
  p.emit({jsonrpc:'2.0',id:closeInit.id,result:{protocolVersion:'2026-01-26'}}); await tick();
  assert.equal(p.toolCalls('trace_snapshot').length,0); const release=p.toolCalls('trace_release').at(-1); assert.ok(release);
  p.reply(release,{released:false}); await boot; await tick();
  assert.ok(p.calls.some(c=>c.id==='close-init'&&c.result)); assert.equal(p.timers.size,0); assert.equal(p.polls.size,0);
});

test('close while snapshot is pending releases even without a committed snapshot and refuses late ready data', async () => {
  const p=page(),{boot,call}=await p.start(); p.emit({jsonrpc:'2.0',id:'close-pending',method:'ui/resource-teardown'});
  const release=p.toolCalls('trace_release').at(-1); assert.ok(release); p.ready(call); await boot;
  assert.equal(p.toolCalls('trace_chunk').length,0); assert.equal(p.run('S.parsed'),null);
  p.reply(release,{released:false}); await tick(); assert.equal(p.timers.size,0);
});

test('duplicate teardown requests share one release and acknowledge each request after completion', async () => {
  const p=page(); await p.loaded();
  for(const id of ['close-one','close-two']) p.emit({jsonrpc:'2.0',id,method:'ui/resource-teardown'});
  assert.equal(p.toolCalls('trace_release').length,1);
  p.reply(p.toolCalls('trace_release').at(-1),{released:true}); await tick();
  for(const id of ['close-one','close-two']) assert.ok(p.calls.some(c=>c.id===id&&c.result));
  assert.equal(p.timers.size,0);
});

test('release replies still require page, nonce, thread identity and a boolean acknowledgement', async () => {
  for(const change of [{view_id:ids[1]},{request_nonce:snapshot},{thread_id:ids[1]},{released:'yes'}]) {
    const p=page(); await p.loaded(); p.emit({jsonrpc:'2.0',id:'close-bad',method:'ui/resource-teardown'});
    p.reply(p.toolCalls('trace_release').at(-1),{released:true,...change}); await tick();
    assert.equal(p.run('S.parsed'),null); assert.equal(p.timers.size,0); assert.equal(p.warnings.length,1);
    assert.ok(p.calls.some(c=>c.id==='close-bad'&&c.result));
  }
});


test('system clock rollback cannot extend the shared initialization and release deadline beyond one second', async () => {
  const p=page(), boot=p.run('bind();boot()');
  p.emit({jsonrpc:'2.0',id:'close-clock',method:'ui/resource-teardown'});
  const closeInit=p.calls.filter(c=>c.method==='ui/initialize').at(-1);
  p.run('Date.now=()=>0');
  p.emit({jsonrpc:'2.0',id:closeInit.id,result:{protocolVersion:'2026-01-26'}});await tick();
  const release=p.toolCalls('trace_release').at(-1);assert.ok(release);
  assert.equal(p.timers.size,1);assert.ok([...p.timers.values()][0].ms<=1000);
  p.reply(release,{released:false});await boot;await tick();assert.equal(p.timers.size,0);
});


test('restored hidden panels handshake but never read or poll before a visible intersection', async () => {
  const p=page(ids[0],null),boot=p.run('bind();boot()');
  assert.equal(p.calls[0].method,'ui/initialize');assert.equal(p.toolCalls().length,0);assert.equal(p.polls.size,0);
  p.visible(false);await tick();assert.equal(p.toolCalls().length,0);
  p.emit({jsonrpc:'2.0',id:p.calls[0].id,result:{protocolVersion:'2026-01-26'}});await boot;
  p.listeners.get('pageshow')({persisted:false});p.documentListeners.get('visibilitychange')();
  await p.box('#reload').onclick();await tick();assert.equal(p.toolCalls().length,0);assert.equal(p.polls.size,0);
  p.visible(true);await tick();
  const call=p.toolCalls('trace_snapshot').at(-1);p.ready(call);await tick();p.chunk(p.toolCalls('trace_chunk').at(-1));await tick();
  assert.equal(p.run('S.parsed.turns.length'),1);assert.equal(p.polls.size,1);
  assert.equal(p.calls.filter(c=>c.method==='ui/initialize').length,1);
});

test('CSS-hidden panel releases all data, waits without polling, and resumes with a fresh isolated view', async () => {
  const p=page();await p.loaded();const oldView=p.toolCalls('trace_snapshot')[0].params.arguments.view_id;
  p.visible(false); assert.equal(p.run('S.parsed'),null);assert.equal(p.polls.size,0);
  const release=p.toolCalls('trace_release').at(-1);assert.equal(release.params.arguments.view_id,oldView);
  const before=p.toolCalls('trace_snapshot').length;await p.run('refreshScoped()');assert.equal(p.toolCalls('trace_snapshot').length,before);
  p.visible(true);await tick();const fresh=p.toolCalls('trace_snapshot').at(-1);
  assert.notEqual(fresh.params.arguments.view_id,oldView);assert.equal(fresh.params.arguments.known_revision,undefined);
  p.reply(release,{released:true});await tick();assert.equal(p.polls.size,1);
  p.ready(fresh);await tick();p.chunk(p.toolCalls('trace_chunk').at(-1));await tick();
  assert.equal(p.run('S.parsed.turns.length'),1);assert.equal(p.warnings.length,0);
});

test('hidden document and persisted pagehide suspend, then bfcache pageshow can resume', async () => {
  const p=page();await p.loaded();p.hidden(true);const r1=p.toolCalls('trace_release').at(-1);
  assert.equal(p.run('S.parsed'),null);assert.equal(p.polls.size,0);p.reply(r1,{released:true});await tick();
  p.hidden(false);await tick();const call=p.toolCalls('trace_snapshot').at(-1);p.ready(call);await tick();p.chunk(p.toolCalls('trace_chunk').at(-1));await tick();
  p.listeners.get('pagehide')({persisted:true});assert.equal(p.run('S.parsed'),null);assert.equal(p.polls.size,0);
  const release=p.toolCalls('trace_release').at(-1);p.reply(release,{released:true});await tick();
  p.listeners.get('pageshow')({persisted:true});await tick();const next=p.toolCalls('trace_snapshot').at(-1);
  assert.notEqual(next.params.arguments.view_id,call.params.arguments.view_id);p.ready(next);await tick();p.chunk(p.toolCalls('trace_chunk').at(-1));await tick();
  assert.equal(p.run('S.parsed.turns.length'),1);assert.equal(p.polls.size,1);
});

test('late snapshot after hide cannot repopulate a resumed panel or release the replacement view', async () => {
  const p=page(),{boot,call}=await p.start();p.visible(false);const release=p.toolCalls('trace_release').at(-1);
  p.visible(true);await tick();const fresh=p.toolCalls('trace_snapshot').at(-1);p.ready(call);await boot;await tick();
  assert.equal(p.run('S.parsed'),null);assert.equal(p.toolCalls('trace_chunk').length,0);
  p.ready(fresh);await tick();p.chunk(p.toolCalls('trace_chunk').at(-1));await tick();
  p.reply(release,{released:true});await tick();assert.equal(p.run('S.parsed.turns.length'),1);assert.equal(p.polls.size,1);
});


test('intersection boundaries suspend only at zero visible area, without using window focus', async () => {
  const p=page();await p.loaded();p.visible({isIntersecting:true,intersectionRect:{width:100,height:0}});
  assert.equal(p.polls.size,0);assert.equal(p.run('S.parsed'),null);
  const release=p.toolCalls('trace_release').at(-1);p.reply(release,{released:true});await tick();
  p.visible({isIntersecting:true,intersectionRect:{width:1,height:1}});await tick();
  assert.equal(p.polls.size,1);assert.equal(p.listeners.has('blur'),false);
});

test('rapid hide-show cancels old initialization and keeps its release isolated from the new initialization', async () => {
  const p=page(),boot=p.run('bind();boot()'),init=p.calls[0];p.visible(false);
  const releaseInit=p.calls.filter(c=>c.method==='ui/initialize').at(-1);p.visible(true);
  const freshInit=p.calls.filter(c=>c.method==='ui/initialize').at(-1);assert.notEqual(freshInit.id,releaseInit.id);
  p.emit({jsonrpc:'2.0',id:init.id,result:{protocolVersion:'2026-01-26'}});
  p.emit({jsonrpc:'2.0',id:releaseInit.id,result:{protocolVersion:'2026-01-26'}});await tick();
  const release=p.toolCalls('trace_release').at(-1);assert.ok(release);
  p.emit({jsonrpc:'2.0',id:freshInit.id,result:{protocolVersion:'2026-01-26'}});await tick();
  const fresh=p.toolCalls('trace_snapshot').at(-1);assert.notEqual(fresh.params.arguments.view_id,release.params.arguments.view_id);
  p.reply(release,{released:false});p.ready(fresh);await tick();p.chunk(p.toolCalls('trace_chunk').at(-1));await boot;await tick();
  assert.equal(p.run('S.parsed.turns.length'),1);assert.equal(p.polls.size,1);assert.equal(p.warnings.length,0);
});

test('multiple alternating views remain isolated when an older release times out and old chunks arrive late', async () => {
  const p=page(),{boot,call}=await p.start();p.ready(call);await tick();const oldChunk=p.toolCalls('trace_chunk').at(-1);
  p.visible(false);const first=p.toolCalls('trace_release').at(-1);p.visible(true);await tick();
  const second=p.toolCalls('trace_snapshot').at(-1);p.visible(false);p.visible(false);
  const nextRelease=p.toolCalls('trace_release').at(-1);p.visible(true);await tick();const fresh=p.toolCalls('trace_snapshot').at(-1);
  assert.equal(p.toolCalls('trace_release').length,2);assert.notEqual(fresh.params.arguments.view_id,second.params.arguments.view_id);
  p.chunk(oldChunk);p.ready(second);await boot;await tick();assert.equal(p.run('S.parsed'),null);
  p.reply(nextRelease,{released:false});p.timeout();await tick();assert.equal(p.timers.size,1);
  assert.equal(p.warnings.length,1);assert.equal(p.calls.some(x=>x.method==='notifications/cancelled'&&x.params.requestId===first.id),true);
  p.ready(fresh);await tick();p.chunk(p.toolCalls('trace_chunk').at(-1));await tick();
  assert.equal(p.run('S.parsed.turns.length'),1);assert.equal(p.polls.size,1);assert.equal(p.timers.size,0);
});

test('hiding one visible window leaves the other window data and polling intact', async () => {
  const a=page(ids[0]),b=page(ids[1]);await Promise.all([a.loaded(),b.loaded()]);const keep=b.run('S.parsed');
  a.visible(false);const release=a.toolCalls('trace_release').at(-1);assert.equal(release.params.arguments.view_id,ids[0]);
  assert.equal(a.run('S.parsed'),null);assert.equal(b.run('S.parsed'),keep);assert.equal(b.polls.size,1);
  assert.equal(b.toolCalls('trace_release').length,0);a.reply(release,{released:true});await tick();
  assert.equal(b.run('S.current.stats.turns'),1);
});


test('teardown of a never-shown restored page cancels only its handshake and creates no backend release guard',async()=>{
  const p=page(ids[0],false),boot=p.run('bind();boot()'),init=p.calls[0];
  p.emit({jsonrpc:'2.0',id:'close-hidden',method:'ui/resource-teardown'});await boot;await tick();
  assert.equal(p.toolCalls().length,0);assert.equal(p.polls.size,0);assert.equal(p.timers.size,0);
  assert.ok(p.calls.some(c=>c.id==='close-hidden'&&c.result));assert.equal(p.listeners.has('message'),false);
  p.emit({jsonrpc:'2.0',id:init.id,result:{protocolVersion:'2026-01-26'}});p.visible(true);await tick();
  assert.equal(p.toolCalls().length,0);assert.equal(p.run('S.parsed'),null);
});


test('native threshold transitions notify zero-area adjacent to positive-area visibility in both directions',async()=>{
  const edge={isIntersecting:true,intersectionRatio:0,intersectionRect:{width:100,height:0}};
  const p=page(ids[0],edge),boot=p.run('bind();boot()');
  p.emit({jsonrpc:'2.0',id:p.calls[0].id,result:{protocolVersion:'2026-01-26'}});await boot;
  assert.equal(p.toolCalls('trace_snapshot').length,0);assert.equal(p.polls.size,0);
  p.visible({isIntersecting:true,intersectionRatio:0.0001,intersectionRect:{width:1,height:1}});await tick();
  const fresh=p.toolCalls('trace_snapshot').at(-1);assert.ok(fresh,'positive area must cross an observer threshold');
  p.ready(fresh);await tick();p.chunk(p.toolCalls('trace_chunk').at(-1));await tick();assert.equal(p.polls.size,1);
  p.visible(edge);assert.equal(p.polls.size,0);assert.equal(p.run('S.parsed'),null);
  p.reply(p.toolCalls('trace_release').at(-1),{released:true});await tick();assert.equal(p.timers.size,0);
});

test('manual pause clears this page immediately and visibility or refresh cannot resume it',async()=>{
  const p=page();await p.loaded();const button=p.box('#trace-pause');
  assert.equal(button.textContent,'暂停并释放');assert.equal(button.style.display,'');
  p.box('#stream').innerHTML='retained content';const paused=button.onclick();
  assert.equal(p.run('S.parsed'),null);assert.equal(p.box('#stream').innerHTML,'');assert.equal(p.polls.size,0);
  assert.equal(button.textContent,'恢复轨迹');
  const release=p.toolCalls('trace_release').at(-1);assert.ok(release);
  const before=p.toolCalls('trace_snapshot').length;
  p.visible(false);p.visible(true);p.listeners.get('pageshow')();p.hidden(true);p.hidden(false);
  await p.box('#reload').onclick();await tick();assert.equal(p.toolCalls('trace_snapshot').length,before);
  p.reply(release,{released:true});await paused;
  assert.match(p.box('#live-status').textContent,/已暂停并释放/);assert.equal(p.polls.size,0);
});

test('manual resume uses a fresh view and an old release cannot change the resumed page',async()=>{
  const p=page();await p.loaded();const old=p.toolCalls('trace_snapshot')[0].params.arguments.view_id;
  const paused=p.box('#trace-pause').onclick(),release=p.toolCalls('trace_release').at(-1);
  const resumed=p.box('#trace-pause').onclick();await tick();const fresh=p.toolCalls('trace_snapshot').at(-1);
  assert.notEqual(fresh.params.arguments.view_id,old);assert.equal(fresh.params.arguments.known_revision,undefined);
  p.ready(fresh);await tick();p.chunk(p.toolCalls('trace_chunk').at(-1));await resumed;
  p.reply(release,{released:true});await paused;
  assert.equal(p.run('S.parsed.turns.length'),1);assert.equal(p.polls.size,1);
  assert.equal(p.box('#trace-pause').textContent,'暂停并释放');assert.match(p.box('#live-status').textContent,/实时更新/);
});

test('manual pause release timeout remains paused and reports the unconfirmed release',async()=>{
  const p=page();await p.loaded();const paused=p.box('#trace-pause').onclick();p.timeout();await paused;
  assert.equal(p.run('S.parsed'),null);assert.equal(p.polls.size,0);assert.equal(p.timers.size,0);
  assert.match(p.box('#live-status').textContent,/释放未确认/);assert.match(p.box('#live-status').textContent,/30 秒/);
  assert.equal(p.warnings.length,1);assert.equal(p.box('#trace-pause').textContent,'恢复轨迹');
});

test('manual pause of one page leaves the other page data and polling untouched',async()=>{
  const a=page(ids[0]),b=page(ids[1]);await Promise.all([a.loaded(),b.loaded()]);const keep=b.run('S.parsed');
  const paused=a.box('#trace-pause').onclick();a.reply(a.toolCalls('trace_release').at(-1),{released:true});await paused;
  assert.equal(a.polls.size,0);assert.equal(b.polls.size,1);assert.equal(b.run('S.parsed'),keep);
  assert.equal(b.toolCalls('trace_release').length,0);assert.equal(b.box('#trace-pause').textContent,'暂停并释放');
});
