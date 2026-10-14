const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');
const assert = require('node:assert/strict');
const test = require('node:test');

const root = path.join(__dirname, '..');
const main = fs.readFileSync(path.join(root, 'index.html'), 'utf8').split('<script>')[1].split('</script>')[0]
  .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
const bridge = fs.readFileSync(path.join(root, 'plugins/codex-trace-probe/viewer-bridge.js'), 'utf8');
const thread = '11111111-2222-4333-8444-555555555555';
const record = (type, payload, ms = 0) => ({type, payload, timestamp:new Date(ms).toISOString()});
const context = id => record('turn_context', {turn_id:id, model:'recorded', model_context_window:2000,
  cwd:'/synthetic project', collaboration_mode:{mode:'default'}});
const message = (role, text, id = 'a', ms = 0) => record('response_item', {type:'message', role,
  content:[{type:role === 'user' ? 'input_text' : 'output_text', text}],
  internal_chat_message_metadata_passthrough:{turn_id:id}}, ms);
const usage = (id, response, input, output, ms) => record('token_usage_record', {turn_id:id, response_id:response,
  usage:{input_tokens:input, cached_input_tokens:input / 2, output_tokens:output, reasoning_output_tokens:output / 2,
    total_tokens:input + output}, thread_token_usage:{total_tokens:999999}}, ms);
const long = '中文🙂'.repeat(5000);
const records = [record('session_meta', {id:thread, cwd:'/synthetic project', dynamic_tools:[]}),
  record('world_state', {date:'1970-01-01'}), context('a'),
  record('event_msg', {type:'thread_settings_applied', thread_settings:{model:'recorded'}}),
  record('event_msg', {type:'task_started', turn_id:'a', started_at:0, collaboration_mode_kind:'default'}),
  message('developer', '<instructions>preserved</instructions>'),
  message('user', 'first input', 'a', 1),
  record('response_item', {type:'custom_tool_call', name:'exec', call_id:'late', input:'text("test")'}, 2),
  usage('a', 'r1', 100, 20, 3),
  record('event_msg', {type:'token_count', info:{last_token_usage:{input_tokens:100, output_tokens:20,
    total_tokens:120}, model_context_window:2000}}, 4),
  message('user', '<send_user_message_question_reply>[{"question":"q","answer":"a"}]</send_user_message_question_reply>', 'a', 10),
  usage('a', 'r1', 100, 20, 11), usage('a', 'r2', 200, 30, 12),
  record('response_item', {type:'function_call', name:'lookup', namespace:'mcp', call_id:'mcp', arguments:'{}'}, 13),
  record('compacted', {message:'retained summary', window_number:2,
    replacement_history:[{type:'message', content:long}], retained_context:{text:long}}, 14),
  record('response_item', {type:'reasoning', summary:[{text:'summary'}], encrypted_content:long}, 15),
  message('assistant', 'first answer', 'a', 16),
  context('b'), message('user', 'other native turn', 'b', 20),
  record('event_msg', {type:'item_completed', turn_id:'a', item:{type:'McpToolCall', id:'mcp', call_id:'mcp',
    server:'example', tool:'lookup', duration:{secs:0, nanos:6000000}, result:{isError:false, content:[{text:long}]}}}, 21),
  record('response_item', {type:'custom_tool_call_output', call_id:'late', output:'Script failed\nkept error',
    internal_chat_message_metadata_passthrough:{turn_id:'a'}}, 22),
  record('event_msg', {type:'item_completed', turn_id:'a', item:{type:'CommandExecution', id:'late',
    command:['sh','-c','echo test'], duration:{secs:1, nanos:234000000}, exit_code:1,
    aggregated_output:long, stdout:long, stderr:long, formatted_output:long}}, 23),
  record('event_msg', {type:'item_completed', turn_id:'b', item:{type:'CommandExecution', id:'separate',
    duration:{secs:0, nanos:500000}, command:['sh','-c','standalone'], exit_code:0, aggregated_output:long}}, 24),
  record('event_msg', {type:'item_completed', turn_id:'b', item:{type:'FileChange', id:'file',
    status:'completed', changes:{'demo.txt':{type:'add', content:long}}}}, 25),
  record('response_item', {type:'function_call_output', call_id:'unmatched', output:'unmatched result'}, 26),
  usage('b', 'r3', 300, 40, 27),
  record('event_msg', {type:'task_complete', turn_id:'a', duration_ms:100, time_to_first_token_ms:6,
    completed_at:28, last_agent_message:'first answer'}, 28),
  message('assistant', 'latest answer', 'b', 29)];
// Physical-line gaps must not be mistaken for the compact valid-record positions.
const source = records.map((r, i) => JSON.stringify(r) + (i === 8 ? '\n\n{malformed}' : '')).join('\n') + '\n';

const python = String.raw`
import base64, importlib.util, json, os, pathlib, sys, tempfile
request = json.load(sys.stdin)
spec = importlib.util.spec_from_file_location('audit_plugin', pathlib.Path.cwd() / 'plugins/codex-trace-probe/server.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
with tempfile.TemporaryDirectory(prefix='trace-lazy-equivalence-') as directory:
    os.environ['CODEX_HOME'] = directory
    root = pathlib.Path(directory) / 'sessions'
    root.mkdir()
    thread = request['thread']
    (root / ('rollout-' + thread + '.jsonl')).write_text(request['source'], encoding='utf-8')
    store = plugin.TraceStore()
    view = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    try:
        manifest = store.snapshot(thread, view, None, 'outline')
        parts = [base64.b64decode(store.chunk(thread, view, manifest['snapshot_id'], i)['data'])
                 for i in range(manifest['total_chunks'])]
        result = {'outline': b''.join(parts).decode(), 'manifest': manifest}
        if 'indices' in request:
            detail = store.detail(thread, view, manifest['snapshot_id'], request['indices'])
            parts = [base64.b64decode(store.chunk(thread, view, manifest['snapshot_id'], i, detail['detail_id'])['data'])
                     for i in range(detail['total_chunks'])]
            result['detail'] = b''.join(parts).decode()
        print(json.dumps(result))
    finally:
        store.close()
`;
function backend(indices) {
  const reply = spawnSync(process.env.PYTHON || 'python3', ['-B', '-c', python], {cwd:root,
    input:JSON.stringify({thread, source, ...(indices ? {indices} : {})}), encoding:'utf8', maxBuffer:16 * 1024 * 1024});
  assert.equal(reply.status, 0, reply.stderr);
  return JSON.parse(reply.stdout);
}
function viewer(outline) {
  const box = {innerHTML:'', querySelectorAll:() => [], querySelector:() => null};
  const state = vm.createContext({console, source, outline, localStorage:{getItem:() => null},
    document:{querySelector:() => box, querySelectorAll:() => []}});
  const run = text => vm.runInContext(text, state);
  run(main);
  run(bridge.split('/* Injected')[0]);
  run('var full=parseRollout(source);var model=createTraceOutline(outline.trim().split("\\n").map(JSON.parse));');
  return {state, run};
}
const summary = `function summary(P){return {meta:P.meta,world:P.world,settings:P.settings,rateLimits:P.rateLimits,
  compacts:P.compacts,totals:P.totals,turns:P.turns.map(t=>({id:t.id,part:t.part,parts:t.parts,ctx:t.ctx,
  spanMs:t.spanMs,toolMs:t.toolMs,firstTs:t.firstTs,lastTs:t.lastTs,durationMs:t.durationMs,ttftMs:t.ttftMs,
  usage:usageOfTurn(t),ctxWindow:t.ctxWindow,tokenSteps:t.tokenSteps.map(s=>({total:s.total,last:s.last,cw:s.cw})),
  kinds:t.steps.map(kindOfStep),tools:t.steps.filter(s=>s.kind==='tool'||s.kind==='mcp').map(s=>({id:s.id,
    callId:s.callId,view:toolView(s),outputs:(s.outputRecords||[]).map(r=>r.timestamp),result:s.result?.raw.timestamp}))}))};}`;

test('the actual backend outline preserves every display round and its independent statistics', () => {
  const reply = backend(), v = viewer(reply.outline); v.run(summary);
  assert.equal(v.run('JSON.stringify(summary(model.parsed))'), v.run('JSON.stringify(summary(full))'));
  assert.equal(v.run('model.parsed.turns.length'), 3);
  assert.equal(v.run('model.parsed.totals.durMs'), v.run('model.parsed.turns.reduce((sum,t)=>sum+t.spanMs,0)'));
  assert.ok(Buffer.byteLength(reply.outline) < Buffer.byteLength(source) / 2);
});

test('each lazily hydrated display round matches its full source including late outputs and raw records', () => {
  const reply = backend(), v = viewer(reply.outline); v.run(summary);
  for(let ti = 0; ti < v.run('full.turns.length'); ti++) {
    const indices = JSON.parse(v.run(`JSON.stringify(traceRoundRecordIndices(model,${ti}))`));
    assert.ok(indices.length > 0);
    const detail = backend(indices).detail;
    v.state.details = detail.trim().split('\n').map(JSON.parse);
    v.run(`var hydrated=hydrateTraceRound(model,${ti},details);`);
    assert.equal(v.run('JSON.stringify(summary(hydrated))'), v.run('JSON.stringify(summary(full))'));
    v.run('function ownTurn(t){const copy={...t};delete copy.sourceTurn;return copy;}');
    assert.equal(v.run(`JSON.stringify(ownTurn(hydrated.turns[${ti}]))`), v.run(`JSON.stringify(ownTurn(full.turns[${ti}]))`));
    assert.ok(indices.some((index, n) => index > n));
  }
});
