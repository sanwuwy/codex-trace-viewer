const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

const thread = 'self';
const entry = (revision = '1') => ({path: '/state/self.jsonl', name: 'self.jsonl', thread_id: thread, revision});
const record = (type, payload, timestamp = '2026-10-10T02:00:00Z') => ({type, payload, timestamp});
const context = (id = 't1') => record('turn_context', {turn_id: id});
const message = (id, text, turn = 't1') => record('response_item', {type: 'message', id, role: 'user',
  content: [{type: 'input_text', text}], internal_chat_message_metadata_passthrough: {turn_id: turn}});
const transcript = (records = [context(), message('m1', 'hello')], id = thread) =>
  [record('session_meta', {id}), ...records].map(JSON.stringify).join('\n') + '\n';

function viewer(bound = thread) {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const script = html.split('<script>')[1].split('</script>')[0]
    .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
  const boxes = new Map(), callbacks = new Map(), calls = [], timers = [], timeouts = new Map();
  let elapsed = 0, timeoutSerial = 0;
  const box = selector => {
    if(!boxes.has(selector)) boxes.set(selector, {innerHTML: '', textContent: '', value: '', scrollTop: 0, scrollLeft: 0,
      style: {}, className: '', classList: {add() {}, remove() {}, toggle() {}},
      querySelectorAll: () => [], querySelector: () => null});
    return boxes.get(selector);
  };
  box('#viewer-config').textContent = JSON.stringify({thread_id: bound});
  const state = {items: [entry()], raw: transcript(), failure: null};
  const environment = vm.createContext({console, location: {search: '?thread_id=other'},
    localStorage: {getItem: () => null}, document: {hidden: false, querySelector: box, querySelectorAll: () => [],
      body: box('body'), addEventListener: (event, cb) => callbacks.set(event, cb)},
    window: {addEventListener: (event, cb) => callbacks.set(event, cb)}, AbortController, TextDecoder,
    setInterval: (cb, ms) => {timers.push({cb, ms}); return timers.length;}, clearInterval: () => {},
    setTimeout: (cb, ms) => {const id = ++timeoutSerial; timeouts.set(id, {cb, ms, due:elapsed+ms}); return id;},
    clearTimeout: id => timeouts.delete(id),
    fetch: async (url, options) => {
      calls.push({url, options});
      if(state.hangFetch || url === state.hangURL) await new Promise((resolve, reject) => options.signal.addEventListener('abort', () =>
        reject(Object.assign(new Error('aborted'), {name: 'AbortError'})), {once: true}));
      if(state.failure) throw new Error(state.failure);
      if(url === '/api/health') return {ok: true, json: async () => ({app: 'codex-trace-viewer', protocol: 1,
        thread_id: state.healthThread ?? bound, instance_id: 'instance'})};
      if(url.startsWith('/api/sessions')) return {ok: true, json: async () => ({root: '/state', count: state.items.length,
        bound_thread: bound, items: state.items})};
      if(url.startsWith('/api/raw')) {
        if(state.rawWait) await state.rawWait;
        return {ok: true, headers: {get: () => null}, body:state.rawBody, text: async () => state.raw};
      }
      if(url === '/api/stats') return {ok: true};
      if(url.startsWith('/api/toolschema')) return new Promise(resolve => state.schemaRequests.push(resolve));
      throw new Error('Unexpected fetch ' + url);
    }});
  const run = code => vm.runInContext(code, environment);
  run(script);
  run('const actualRenderInsp=renderInsp,actualRenderTabs=renderTabs,actualRenderStream=renderStream,actualRenderList=renderList;');
  run('for(const name of ["renderList","renderSessionBar","renderTimeline","renderFilterBar","renderStream","renderTabs","renderInsp"]) eval(name+"=()=>{}");');
  const advance = ms => {
    const target = elapsed+ms;
    for(;;){
      const next = [...timeouts.entries()].filter(([,t])=>t.due<=target).sort((a,b)=>a[1].due-b[1].due)[0];
      if(!next) break;
      elapsed = next[1].due; timeouts.delete(next[0]); next[1].cb();
    }
    elapsed = target;
  };
  return {run, environment, boxes, box, state, calls, timers, timeouts, callbacks, html, advance};
}

test('scoped boot ignores query scope, hides global selectors and opens the bound transcript', async () => {
  const v = viewer();
  await v.run('boot()');
  assert.equal(v.run('S.scope'), thread);
  assert.equal(v.run('S.current.thread_id'), thread);
  assert.equal(v.run('S.parsed.meta.id'), thread);
  assert.equal(v.box('#pick').style.display, 'none');
  assert.equal(v.box('#mode').textContent, '会话轨迹');
  assert.equal(v.timers.length, 1);
  assert.equal(v.timers[0].ms, 2000);
});

test('scoped session assignment skips sorting, search indexing and conversation tree building', () => {
  const v = viewer();
  v.environment.input = [entry()];
  Object.defineProperty(v.environment.input[0], '_searchText', {set: () => {throw new Error('global search index');}});
  v.run('let sortCalls=0,treeCalls=0; const originalSort=Array.prototype.sort; Array.prototype.sort=function(...args){sortCalls++;return originalSort.apply(this,args)}; buildTree=()=>{treeCalls++;throw new Error("global conversation tree")};');
  v.run('setSessions(input)');
  assert.equal(v.run('sortCalls'), 0);
  assert.equal(v.run('treeCalls'), 0);
  assert.equal(v.run('S.sessions[0].path'), v.environment.input[0].path);
  assert.equal(v.run('S.tree.length'), 0);
  assert.equal(v.run('S.rows.length'), 0);
});

test('scoped waiting, startup, update, refresh and display controls perform no global sidebar work', async () => {
  const v = viewer();
  v.run('const sidebarCalls={sort:0,tree:0,rows:0,list:0}; const originalSort=Array.prototype.sort; Array.prototype.sort=function(...args){sidebarCalls.sort++;return originalSort.apply(this,args)}; buildTree=()=>{sidebarCalls.tree++;throw new Error("tree")}; buildRows=()=>{sidebarCalls.rows++;throw new Error("rows")}; renderList=()=>{sidebarCalls.list++;throw new Error("hidden sidebar")};');
  v.state.items = [];
  await v.run('boot()');
  v.timers[0].cb(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(v.run('S.parsed'), null);
  assert.equal(v.calls.filter(c => c.url.startsWith('/api/raw')).length, 0);
  v.state.items = [entry()]; await v.run('refreshScoped()');
  assert.equal(v.run('S.parsed?.meta.id'), thread);
  v.state.items = [entry('2')]; v.state.raw += JSON.stringify(message('m2', 'next')) + '\n';
  await v.run('refreshScoped()');
  v.environment.document.documentElement = {dataset: {theme: 'light'}};
  v.run('bind()');
  await v.box('#reload').onclick();
  v.box('#theme').onclick();
  v.box('#detail').onchange({target: {checked: false}});
  v.box('#q').oninput();
  v.box('#slist').onclick({target: {closest: () => {throw new Error('hidden sidebar input');}}});
  v.box('#dirpick').onchange({target: {files: [{name: 'other.jsonl'}]}});
  v.callbacks.get('drop')({preventDefault() {}, dataTransfer: {files: [{name: 'other.jsonl'}]}});
  const model = {dataset: {tab: 'model'}};
  v.box('#tabs').querySelectorAll = selector => selector === 'button'? [model] : [];
  v.run('renderTabs=actualRenderTabs;renderInsp=actualRenderInsp;renderStream=actualRenderStream;setSelTurn(0);renderTabs()');
  model.onclick();
  assert.equal(v.run('S.tab'), 'model');
  assert.equal(v.run('JSON.stringify(sidebarCalls)'), '{"sort":0,"tree":0,"rows":0,"list":0}');
  assert.equal(v.run('"_searchText" in S.current'), false);
  assert.equal(v.run('"preview" in S.current'), false);
});

test('generic startup still sorts conversations and builds its visible sidebar', async () => {
  const v = viewer(null);
  v.run('let sortCalls=0,treeCalls=0,rowsCalls=0; const originalSort=Array.prototype.sort,originalTree=buildTree,originalRows=buildRows; Array.prototype.sort=function(...args){sortCalls++;return originalSort.apply(this,args)}; buildTree=list=>{treeCalls++;return originalTree(list)}; buildRows=()=>{rowsCalls++;return originalRows()}; renderList=actualRenderList;');
  await v.run('boot()');
  assert.ok(v.run('sortCalls') > 0);
  assert.equal(v.run('treeCalls'), 1);
  assert.equal(v.run('rowsCalls'), 1);
  assert.ok(v.run('S.sessions[0]._searchText').includes('self'));
  assert.match(v.box('#slist').innerHTML, /最近|会话/);
});

test('initial scoped service failure never falls back to local files or global sessions', async () => {
  const v = viewer(); v.state.failure = 'service stopped';
  await v.run('boot()');
  assert.equal(v.run('S.mode'), 'server');
  assert.equal(v.run('S.scope'), thread);
  assert.equal(v.run('S.parsed'), null);
  assert.equal(v.box('#pick').style.display, 'none');
  assert.match(v.box('#live-status').textContent, /服务|更新失败/);
  assert.match(v.box('#stream').innerHTML, /启动|重试/);
});

test('unchanged revisions avoid raw reads, parsing and statistics writes', async () => {
  const v = viewer(); await v.run('boot()');
  const snapshot = v.run('S.parsed');
  await v.run('refreshScoped()');
  assert.equal(v.run('S.parsed'), snapshot);
  assert.equal(v.calls.filter(c => c.url.startsWith('/api/raw')).length, 1);
  assert.equal(v.calls.filter(c => c.url === '/api/stats').length, 1);
  v.state.items = [entry('2')]; v.state.raw += JSON.stringify(message('m2', 'next')) + '\n';
  await v.run('refreshScoped()');
  assert.equal(v.calls.filter(c => c.url.startsWith('/api/raw')).length, 2);
  assert.equal(v.calls.filter(c => c.url === '/api/stats').length, 2);
});

test('concurrent refresh triggers share one request and hidden tabs do not poll', async () => {
  const v = viewer(); await v.run('boot()'); v.state.items = [entry('2')];
  let release; v.state.rawWait = new Promise(resolve => { release = resolve; });
  const first = v.run('refreshScoped()'), second = v.run('refreshScoped()');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(v.calls.filter(c => c.url.startsWith('/api/raw')).length, 2);
  release(); await Promise.all([first, second]);
  const before = v.calls.length;
  v.environment.document.hidden = true;
  v.timers[0].cb();
  assert.equal(v.calls.length, before);
});

test('a stopped service retains the last snapshot and retries without changing scope', async () => {
  const v = viewer(); await v.run('boot()'); const snapshot = v.run('S.parsed');
  v.state.failure = 'connection refused'; await v.run('refreshScoped()');
  assert.equal(v.run('S.parsed'), snapshot);
  assert.equal(v.run('S.current.thread_id'), thread);
  assert.match(v.box('#live-status').textContent, /更新失败/);
  v.state.failure = null; await v.run('refreshScoped()');
  assert.match(v.box('#live-status').textContent, /实时更新/);
});

test('a hung response is aborted after ten seconds and cannot block later retries', async () => {
  const v = viewer(); await v.run('boot()'); const snapshot = v.run('S.parsed');
  v.state.hangFetch = true;
  const pending = v.run('refreshScoped()');
  assert.equal(v.timeouts.size, 1);
  const timer = [...v.timeouts.values()][0]; assert.equal(timer.ms, 10000); timer.cb();
  await pending;
  assert.equal(v.run('S.parsed'), snapshot);
  assert.match(v.box('#live-status').textContent, /超时/);
  assert.equal(v.timeouts.size, 0);
  v.state.hangFetch = false; await v.run('refreshScoped()');
  assert.match(v.box('#live-status').textContent, /实时更新/);
});

test('health and listing requests each remain bounded when no response arrives', async () => {
  for(const url of ['/api/health', '/api/sessions?limit=0']) {
    const v = viewer(); await v.run('boot()'); const snapshot = v.run('S.parsed');
    v.state.hangURL = url;
    const pending = v.run('refreshScoped()'); await new Promise(resolve=>setImmediate(resolve));
    assert.equal(v.calls.at(-1).url, url);
    v.advance(10000); await pending;
    assert.equal(v.run('S.parsed'), snapshot); assert.match(v.box('#live-status').textContent, /超时/);
    assert.equal(v.timeouts.size, 0); v.state.hangURL = null;
    await v.run('refreshScoped()'); assert.match(v.box('#live-status').textContent, /实时更新/);
  }
});

function controlledBody() {
  let release;
  const state = {cancelled:0, released:0};
  return {state, body:{getReader:() => ({read:() => new Promise(resolve => {release=resolve;}),
    async cancel() {state.cancelled++;release?.({done:true});}, releaseLock() {state.released++;}})},
    deliver(value) {release(value == null ? {done:true} : {done:false, value:Buffer.from(value)});} };
}

test('a full stream can run longer than ten seconds while nonempty chunks keep making progress', async () => {
  const v = viewer(); await v.run('boot()'); const input = controlledBody();
  v.state.items = [entry('2')]; v.state.rawBody = input.body;
  const reading = v.run('refreshScoped()'); await new Promise(resolve=>setImmediate(resolve));
  const rows = v.state.raw.split('\n');
  v.advance(6000); input.deliver(rows.slice(0,2).join('\n')+'\n'); await new Promise(resolve=>setImmediate(resolve));
  v.advance(6000); input.deliver(rows.slice(2).join('\n')); await new Promise(resolve=>setImmediate(resolve));
  v.advance(6000); input.deliver(null); await reading;
  assert.equal(v.run('S.current.revision'), '2'); assert.equal(v.run('S.parsed.turns.length'), 1);
  assert.match(v.box('#live-status').textContent, /实时更新/);
  assert.equal(input.state.cancelled, 0); assert.equal(input.state.released, 1); assert.equal(v.timeouts.size, 0);
});

test('ten seconds without stream progress aborts the reader and retains the previous complete snapshot', async () => {
  const v = viewer(); await v.run('boot()'); const input = controlledBody(), snapshot = v.run('S.parsed');
  v.state.items = [entry('2')]; v.state.rawBody = input.body;
  const reading = v.run('refreshScoped()'); await new Promise(resolve=>setImmediate(resolve));
  v.advance(6000); input.deliver(v.state.raw.slice(0,40)); await new Promise(resolve=>setImmediate(resolve));
  v.advance(10000); await reading;
  assert.equal(v.run('S.parsed'), snapshot); assert.equal(v.run('S.current.revision'), '1');
  assert.match(v.box('#live-status').textContent, /超时/);
  assert.equal(input.state.cancelled, 1); assert.equal(input.state.released, 1); assert.equal(v.timeouts.size, 0);
});

test('empty stream chunks do not reset the ten-second inactivity limit', async () => {
  const v = viewer(); await v.run('boot()'); const input = controlledBody(), snapshot = v.run('S.parsed');
  v.state.items = [entry('2')]; v.state.rawBody = input.body;
  const reading = v.run('refreshScoped()'); await new Promise(resolve=>setImmediate(resolve));
  v.advance(9000); input.deliver(''); await new Promise(resolve=>setImmediate(resolve));
  v.advance(1000); await reading;
  assert.equal(v.run('S.parsed'), snapshot); assert.match(v.box('#live-status').textContent, /超时/);
  assert.equal(input.state.cancelled, 1); assert.equal(v.timeouts.size, 0);
});

test('a transcript not yet saved waits and is automatically opened when it becomes available', async () => {
  const v = viewer(); v.state.items = []; await v.run('boot()');
  assert.equal(v.run('S.parsed'), null);
  assert.match(v.box('#stream').innerHTML, /等待.*记录/);
  assert.equal(v.calls.filter(c => c.url.startsWith('/api/raw')).length, 0);
  v.state.items = [entry()]; await v.run('refreshScoped()');
  assert.equal(v.run('S.parsed.meta.id'), thread);
});

test('health, listing and transcript identities must all match the immutable thread', async () => {
  for(const mismatch of ['health', 'listing', 'raw']) {
    const v = viewer(); await v.run('boot()'); const snapshot = v.run('S.parsed');
    v.state.items = [entry('2')];
    if(mismatch === 'health') v.state.healthThread = 'other';
    if(mismatch === 'listing') v.state.items[0].thread_id = 'other';
    if(mismatch === 'raw') v.state.raw = transcript([], 'other');
    await v.run('refreshScoped()');
    assert.equal(v.run('S.parsed'), snapshot);
    assert.match(v.box('#live-status').textContent, /更新失败/);
  }
});

test('refresh remaps selection, expanded rounds and output after preceding records or message IDs change', async () => {
  const v = viewer(); v.state.raw = transcript([context(), message('m1', 'x'.repeat(1000)), context('t2'), message('m2', 'next', 't2')]);
  await v.run('boot()');
  v.run('S.sel={ti:0,idx:0}; S.selTurn=0; S.tab="m_raw"; S.expanded=new Set([0]); S.truncMap.set("0:0",true)');
  v.box('#stream').scrollTop = 128; v.box('#insp').scrollTop = 64;
  v.state.items = [entry('2')];
  v.state.raw = transcript([context('earlier'), message('earlier', 'old', 'earlier'), context(),
    message('new-m1-id', 'x'.repeat(1000)), context('t2'), message('m2', 'next', 't2')]);
  await v.run('refreshScoped()');
  assert.equal(v.run('S.sel.ti'), 1);
  assert.equal(v.run('selectedStep().id'), 'new-m1-id');
  assert.equal(v.run('S.tab'), 'm_raw');
  assert.equal(v.run('[...S.expanded].join(",")'), '1');
  assert.equal(v.run('S.truncMap.get("1:0")'), true);
  assert.ok(!v.run('stepHtml(selectedStep(),0,1).includes("tx clip")'));
  assert.equal(v.box('#stream').scrollTop, 128);
  assert.equal(v.box('#insp').scrollTop, 64);
});

test('expanded output styling never removes the literal word clip from recorded content', async () => {
  const v = viewer(); v.state.raw = transcript([context(), message('m1', 'clip ' + 'x'.repeat(1000))]);
  await v.run('boot()');
  v.run('S.truncMap.set("0:0",true)');
  assert.match(v.run('stepHtml(S.parsed.turns[0].steps[0],0,0)'), />clip x/);
});

test('an incomplete final JSONL append is ignored until the next revision completes it', async () => {
  const v = viewer(); await v.run('boot()');
  const append = JSON.stringify(message('m2', 'next'));
  v.state.items = [entry('2')]; v.state.raw += append.slice(0, 40);
  await v.run('refreshScoped()');
  assert.equal(v.run('S.parsed.turns.length'), 1);
  v.state.items = [entry('3')]; v.state.raw += append.slice(40) + '\n';
  await v.run('refreshScoped()');
  assert.equal(v.run('S.parsed.turns.length'), 2);
});

test('late requests invalidated by page shutdown cannot replace the snapshot', async () => {
  const v = viewer(); await v.run('boot()'); const snapshot = v.run('S.parsed');
  v.state.items = [entry('2')];
  let release; v.state.rawWait = new Promise(resolve => { release = resolve; });
  const pending = v.run('refreshScoped()');
  await new Promise(resolve => setImmediate(resolve));
  v.run('stopScopedPolling()'); release(); await pending;
  assert.equal(v.run('S.parsed'), snapshot);
});

async function schemaViewer() {
  const v = viewer();
  v.state.schemaRequests = [];
  v.state.raw = transcript([context(), record('response_item', {type: 'function_call', id: 'tool1',
    name: 'run', call_id: 'call1', arguments: '{}'})]);
  await v.run('boot()');
  const box = v.box('#insp');
  let html = '', top = 0, left = 0, maximum = 500;
  Object.defineProperties(box, {
    innerHTML: {get: () => html, set: value => {html = value; maximum = value.includes('正在查询工具 Schema')? 0 : 500;
      top = Math.min(top, maximum); left = Math.min(left, maximum);}},
    scrollTop: {get: () => top, set: value => {top = Math.min(value, maximum);}},
    scrollLeft: {get: () => left, set: value => {left = Math.min(value, maximum);}}
  });
  v.run('renderInsp=actualRenderInsp; S.sel={ti:0,idx:0}; S.selTurn=0; S.tab="t_schema"; renderInsp()');
  v.state.schemaRequests.shift()({ok: true, json: async () => ({found: true, name: 'run', description: 'x'.repeat(2000)})});
  await new Promise(resolve => setImmediate(resolve));
  box.scrollTop = 220; box.scrollLeft = 3;
  return v;
}

test('asynchronous schema loading restores inspector scroll after a refreshed snapshot', async () => {
  const v = await schemaViewer(); v.state.items = [entry('2')];
  await v.run('refreshScoped()');
  v.state.schemaRequests.shift()({ok: true, json: async () => ({found: true, name: 'updated', description: 'x'.repeat(2000)})});
  await new Promise(resolve => setImmediate(resolve));
  assert.match(v.box('#insp').innerHTML, /updated/);
  assert.equal(v.box('#insp').scrollTop, 220);
  assert.equal(v.box('#insp').scrollLeft, 3);
});

test('schema scroll restoration follows only the latest snapshot and selected tab', async () => {
  const v = await schemaViewer(); v.state.items = [entry('2')];
  await v.run('refreshScoped()'); const old = v.state.schemaRequests.shift();
  v.state.items = [entry('3')]; await v.run('refreshScoped()');
  old({ok: true, json: async () => ({found: true, name: 'stale', description: 'x'.repeat(2000)})});
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!v.box('#insp').innerHTML.includes('stale'));
  v.state.schemaRequests.shift()({ok: true, json: async () => ({found: true, name: 'latest', description: 'x'.repeat(2000)})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(v.box('#insp').scrollTop, 220);
  v.state.items = [entry('4')]; await v.run('refreshScoped()');
  v.run('S.tab="t_args"; renderInsp()');
  v.box('#insp').scrollTop = 80;
  const content = v.box('#insp').innerHTML;
  v.state.schemaRequests.shift()({ok: true, json: async () => ({found: true, name: 'wrong tab'})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(v.box('#insp').innerHTML, content);
  assert.equal(v.box('#insp').scrollTop, 80);
});

test('generic service keeps the existing picker and creates no live polling timer', async () => {
  const v = viewer(null); await v.run('boot()');
  assert.equal(v.run('S.scope'), null);
  assert.equal(v.run('S.mode'), 'server');
  assert.equal(v.run('S.current'), null);
  assert.equal(v.timers.length, 0);
  assert.equal(v.calls.length, 1);
  assert.ok(v.calls[0].url.startsWith('/api/sessions'));
});
