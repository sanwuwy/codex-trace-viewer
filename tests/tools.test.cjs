const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function viewer() {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const script = html.split('<script>')[1].split('</script>')[0]
    .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
  const box = {innerHTML: ''};
  const context = vm.createContext({console, localStorage: {getItem: () => null},
    document: {querySelector: () => box}, fetch: () => Promise.reject(new Error('offline'))});
  vm.runInContext(script, context);
  return {context, box, run: (code) => vm.runInContext(code, context)};
}
const record = (type, payload, timestamp = '2026-10-09T12:00:00Z') => ({type, payload, timestamp});
const call = (id, name, input, custom = false) => record('response_item', custom
  ? {type: 'custom_tool_call', call_id: id, name, input, status: 'completed'}
  : {type: 'function_call', call_id: id, name, namespace: 'collaboration', arguments: input});
const done = (id, type, extra = {}) => record('event_msg', {type: 'item_completed', turn_id: 't1',
  started_at_ms: 10, completed_at_ms: 11, item: {type, id, status: 'completed', ...extra}});
function parse(v, records) {
  v.context.fixture = [record('turn_context', {turn_id: 't1'}), ...records].map(JSON.stringify).join('\n');
  return v.run('parseRollout(fixture)');
}

test('parallel command events never attach to unrelated calls or overwrite each other', () => {
  const v = viewer();
  const p = parse(v, [call('message', 'followup_task', '{"target":"worker"}'),
    call('exec', 'exec', 'await Promise.all([tools.exec_command({cmd:"first"}), tools.exec_command({cmd:"second"})]);', true),
    done('exec-1', 'CommandExecution', {command: ['/bin/zsh', '-lc', 'first']}),
    done('exec-2', 'CommandExecution', {command: ['/bin/zsh', '-lc', 'second']})]);
  const steps = p.turns[0].steps;
  assert.equal(steps.find(s => s.tool === 'followup_task').result, undefined);
  assert.equal(steps.filter(s => s.result?.item?.type === 'CommandExecution').length, 2);
  assert.equal(steps.find(s => s.callId === 'exec').result, undefined);
});

test('MCP event and output use call ID and retain structured content without duplicate rows', () => {
  const v = viewer();
  const p = parse(v, [call('call-mcp', 'js', '{"code":"inspect"}'),
    done('call-mcp', 'McpToolCall', {server: 'cua_repl', tool: 'js', arguments: {code: 'inspect'},
      duration: {secs: 0, nanos: 0}, result: {isError: true, content: [{type: 'text', text: 'failed'}]}}),
    record('response_item', {type: 'function_call_output', call_id: 'call-mcp', output: [{type: 'input_image', image_url: 'data:image/png;base64,AA=='}]})]);
  const steps = p.turns[0].steps;
  assert.equal(steps.length, 1);
  v.context.step = steps[0];
  assert.equal(v.run('toolView(step).status'), 'failed');
  assert.equal(v.run('toolView(step).secs'), 0);
  assert.ok(v.run('resultSection(step)').includes('input_image'));
});

test('freeform code remains original input instead of guessing the first object as executed parameters', () => {
  const v = viewer();
  v.context.step = {args: 'const options = {cmd:"unused"}; await tools.exec_command({cmd:"actual"});'};
  assert.equal(v.run('parseToolArgs(step).params'), null);
  assert.equal(v.run('toolView(step).cmd'), '');
  v.context.step = {args: '{"cmd":"echo hi","workdir":"/tmp"}'};
  assert.equal(v.run('parseToolArgs(step).params.cmd'), 'echo hi');
});

test('completed model call without a result does not claim successful execution or invented duration', () => {
  const v = viewer();
  v.context.step = parse(v, [call('call-pending', 'exec', 'text("pending");', true)]).turns[0].steps[0];
  assert.equal(v.run('toolView(step).status'), '未记录结果');
  assert.equal(v.run('toolView(step).secs'), null);
  assert.ok(!v.run('resultSection(step)').includes('可能仍在运行'));
});

test('empty output is preserved and record interval is distinct from measured execution duration', () => {
  const v = viewer();
  const p = parse(v, [call('empty', 'empty', '{}'), record('response_item', {
    type: 'function_call_output', call_id: 'empty', output: ''}, '2026-10-09T12:00:03Z')]);
  v.context.step = p.turns[0].steps[0];
  assert.equal(v.run('toolView(step).secs'), null);
  assert.equal(v.run('toolView(step).status'), '已返回');
  assert.ok(v.run('resultSection(step)').includes('空输出'));
});

test('session schema lookup respects namespaces and flat dynamic tool definitions', () => {
  const v = viewer();
  v.context.catalog = [{name: 'alpha', tools: [{name: 'run', inputSchema: {title: 'alpha'}}]},
    {name: 'beta', tools: [{name: 'run', inputSchema: {title: 'beta'}}]},
    {name: 'flat', input_schema: {title: 'flat'}}];
  v.run('S.parsed = {dynTools:catalog}');
  v.context.step = {tool: 'run', ns: 'beta', args: '{}'};
  assert.equal(v.run('sessionToolSchema(step, parseToolArgs(step)).tool.inputSchema.title'), 'beta');
  v.context.step = {tool: 'run', ns: '', args: '{}'};
  assert.equal(v.run('sessionToolSchema(step, parseToolArgs(step))'), null);
  v.context.step = {tool: 'flat', args: '{}'};
  assert.equal(v.run('sessionToolSchema(step, parseToolArgs(step)).tool.input_schema.title'), 'flat');
});

test('late schema responses never replace a newly selected arguments tab', async () => {
  const v = viewer();
  let resolve;
  v.context.fetch = () => new Promise(r => {resolve = r;});
  v.context.step = {kind: 'tool', tool: 'exec', args: 'original code', ts: '2026-10-09T12:00:00Z'};
  v.context.turn = {ctx: {}, tokenSteps: []};
  v.run('S.mode="server"; S.parsed={dynTools:[],turns:[{...turn,steps:[step]}]}; S.sel={ti:0,idx:0}; S.tab="t_schema"; renderInsp()');
  v.run('S.tab="t_args"; renderInsp()');
  const before = v.box.innerHTML;
  resolve({ok: true, json: async () => ({found: true, name: 'WRONG TAB'})});
  await new Promise(r => setImmediate(r));
  assert.equal(v.box.innerHTML, before);
});

test('IDs are scoped to the owning turn and orphan outputs remain visible', () => {
  const v = viewer();
  const p = parse(v, [call('same', 'first', '{}'), record('turn_context', {turn_id: 't2'}),
    done('same', 'CommandExecution', {command: ['/bin/zsh', '-lc', 'echo first']}),
    record('response_item', {type: 'function_call_output', call_id: 'missing', output: {error: 'orphan'}})]);
  assert.equal(p.turns[0].steps[0].result.item.command[2], 'echo first');
  assert.ok(p.turns[1].steps.some(s => s.kind === 'toolout' && s.text.includes('orphan')));
});

test('execution row exposes its own result and expanded output is not permanently truncated', () => {
  const v = viewer();
  const output = 'a'.repeat(900) + 'END_OF_OUTPUT';
  const p = parse(v, [done('execution', 'CommandExecution', {command: ['/bin/zsh', '-lc', 'echo data'],
    aggregated_output: output, duration: {secs: 0, nanos: 2000}})]);
  v.context.step = p.turns[0].steps[0];
  assert.ok(v.run('stepHtml(step,0,0)').includes('END_OF_OUTPUT'));
  assert.equal(p.totals.tools, 0);
  assert.equal(p.totals.commands, 1);
});

test('standalone execution timestamps are not labeled as model invocation records', () => {
  const v = viewer();
  v.context.step = {kind: 'mcp', origin: 'event', tool: 'run', args: {},
    ts: '2026-10-09T12:00:00Z', outputRecords: [{timestamp: '2026-10-09T12:00:03Z'}]};
  v.run('S.tab="t_time"; renderToolInsp(document.querySelector("#insp"), step, {ctx:{}})');
  assert.ok(v.box.innerHTML.includes('>执行事件记录<'));
  assert.ok(!v.box.innerHTML.includes('>调用记录<'));
  assert.ok(v.box.innerHTML.includes('未记录有效间隔'));
});

test('tool and MCP rows retain full arguments and results when expanded', () => {
  const v = viewer();
  v.context.step = {kind: 'mcp', server: 'test', tool: 'run',
    args: {text: 'a'.repeat(700) + 'END_OF_ARGS'},
    result: {item: {type: 'McpToolCall', result: {text: 'b'.repeat(700) + 'END_OF_RESULT'}}}};
  const html = v.run('stepHtml(step, 0, 0)');
  assert.ok(html.includes('END_OF_ARGS'));
  assert.ok(html.includes('END_OF_RESULT'));
  v.context.step = {kind: 'tool', tool: 'exec', args: 'a'.repeat(700) + 'END_OF_INPUT'};
  assert.ok(v.run('stepHtml(step, 0, 0)').includes('END_OF_INPUT'));
});

test('image event labels accept Windows and POSIX paths while retaining the original record', () => {
  for(const imagePath of ['C:\\Users\\example\\images\\sample.png', '/home/example/images/sample.png']){
    const v = viewer();
    const p = parse(v, [done('image', 'ImageView', {path: imagePath})]);
    const step = p.turns[0].steps[0];
    assert.equal(step.label, '查看图片 sample.png');
    assert.equal(step.raw.payload.item.path, imagePath);
  }
});

test('file change summaries abbreviate Windows and POSIX paths without changing recorded paths', () => {
  for(const filePath of ['C:\\Users\\example\\demo\\src\\main.js', '/home/example/demo/src/main.js']){
    const v = viewer();
    const p = parse(v, [done('file', 'FileChange', {changes: {[filePath]: {type: 'update', content: 'text'}}})]);
    v.context.step = p.turns[0].steps[0];
    assert.ok(v.run('stepHtml(step, 0, 0)').includes('demo/src/main.js'));
    assert.equal(v.context.step.files[0].path, filePath);
    assert.ok(Object.hasOwn(v.context.step.raw.payload.item.changes, filePath));
  }
});
