const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function viewer(records) {
  const script = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8').split('<script>')[1].split('</script>')[0]
    .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
  const box = {innerHTML: '', querySelectorAll: () => [], querySelector: () => null};
  const context = vm.createContext({console, fixture: records.map(JSON.stringify).join('\n'),
    localStorage: {getItem: () => null}, document: {querySelector: () => box}});
  const run = code => vm.runInContext(code, context);
  run(script);
  run('S.parsed=parseRollout(fixture); S.current={name:"sample"}');
  return {run, box};
}
const record = (type, payload, ms) => ({type, payload, timestamp: new Date(ms).toISOString()});
const ctx = (id, ms) => record('turn_context', {turn_id: id}, ms);
const msg = (role, ms, id = 't1') => record('response_item', {type: 'message', role,
  content: [{type: 'input_text', text: 'text'}], internal_chat_message_metadata_passthrough: {turn_id: id}}, ms);
const call = (id, ms) => record('response_item', {type: 'function_call', name: 'command', call_id: id, arguments: '{}'}, ms);
const done = (id, ms, secs, nanos) => record('event_msg', {type: 'item_completed',
  item: {id, type: 'CommandExecution', duration: {secs, nanos}}}, ms);
const output = (id, ms) => record('response_item', {type: 'function_call_output', call_id: id, output: 'returned'}, ms);

test('completed and unfinished segments use record spans and their totals equal the sum', () => {
  const v = viewer([ctx('t1', 0), msg('user', 0), call('a', 100), done('a', 500, 1, 234567890),
    msg('assistant', 1500), record('event_msg', {type: 'task_complete', turn_id: 't1', duration_ms: 999999}, 1600),
    ctx('t2', 2000), msg('user', 2000, 't2'), call('b', 2010), done('b', 2600, 0, 1600000), output('b', 4500)]);
  assert.equal(v.run('S.parsed.turns.map(t=>t.spanMs).join(",")'), '1500,2500');
  assert.equal(v.run('S.parsed.turns.map(t=>t.toolMs).join(",")'), '1235,2');
  assert.equal(v.run('S.parsed.totals.durMs'), 4000);
  assert.equal(v.run('S.parsed.totals.toolMs'), 1237);
  v.run('renderSessionBar()');
  assert.ok(v.box.innerHTML.includes('执行耗时 1.237 秒'));
  assert.ok(v.box.innerHTML.includes('对话耗时 4 秒'));
  v.run('renderStream()');
  assert.equal((v.box.innerHTML.match(/执行耗时/g) || []).length, 2);
  assert.ok(v.box.innerHTML.includes('对话耗时 1.5 秒'));
  assert.ok(v.box.innerHTML.includes('对话耗时 2.5 秒'));
  assert.ok(!v.box.innerHTML.includes('跨度'));
});

test('tool return records and completion records extend the owning segment bounds', () => {
  const v = viewer([ctx('t1', 0), msg('user', 100), call('a', 200), output('a', 600), done('a', 800, 0, 0)]);
  assert.equal(v.run('S.parsed.turns[0].spanMs'), 700);
  assert.equal(v.run('S.parsed.turns[0].toolMs'), 0);
});

test('split segments keep individual spans and execution sums without reusing native turn duration', () => {
  const v = viewer([ctx('t1', 0), msg('user', 0), call('a', 10), done('a', 30, 0, 100000000), msg('assistant', 50),
    msg('user', 2000), call('b', 2500), done('b', 3000, 0, 200000000), msg('assistant', 4000),
    record('event_msg', {type: 'task_complete', turn_id: 't1', duration_ms: 9000}, 5000)]);
  assert.equal(v.run('S.parsed.turns.map(t=>t.spanMs).join(",")'), '50,2000');
  assert.equal(v.run('S.parsed.turns.map(t=>t.toolMs).join(",")'), '100,200');
  assert.equal(v.run('S.parsed.totals.durMs'), 2050);
  assert.equal(v.run('S.parsed.totals.toolMs'), 300);
  assert.equal(v.run('S.parsed.turns[1].sourceTurn.durationMs'), 9000);
});

test('single-record zero spans remain known while missing timestamps make the total unknown', () => {
  const v = viewer([ctx('t1', 0), msg('user', 0)]);
  assert.equal(v.run('S.parsed.turns[0].spanMs'), 0);
  assert.equal(v.run('S.parsed.totals.durMs'), 0);
  const bad = msg('user', 0); bad.timestamp = 'invalid';
  const unknown = viewer([ctx('t1', 0), bad]);
  assert.equal(unknown.run('S.parsed.turns[0].spanMs'), null);
  assert.equal(unknown.run('S.parsed.totals.durMs'), null);
});

test('millisecond display preserves fractional seconds and hours without rounding differences', () => {
  const v = viewer([ctx('t1', 0), msg('user', 0), call('a', 100), done('a', 200, 0, 490000),
    call('b', 300), done('b', 400, 0, 490000), msg('assistant', 500), ctx('t2', 1000), msg('user', 1000, 't2'), msg('assistant', 1500, 't2')]);
  assert.equal(v.run('S.parsed.turns[0].toolMs'), 1);
  assert.equal(v.run('S.parsed.totals.toolMs'), 1);
  assert.equal(v.run('elapsed(S.parsed.turns[0].spanMs)'), '0.5 秒');
  assert.equal(v.run('elapsed(S.parsed.totals.durMs)'), '1 秒');
  assert.equal(v.run('elapsed(3600001)'), '1 时 0 分 0.001 秒');
  assert.equal(v.run('elapsed(null)'), '—');
});

test('late returns remain in their original segment even when segment spans overlap', () => {
  const late = output('a', 3000);
  late.payload.internal_chat_message_metadata_passthrough = {turn_id: 't1'};
  const bad = msg('assistant', 2500, 't2'); bad.timestamp = 'invalid';
  const v = viewer([ctx('t1', 0), msg('user', 0), call('a', 100),
    ctx('t2', 1000), msg('user', 1000, 't2'), bad, msg('assistant', 2000, 't2'), late]);
  assert.equal(v.run('S.parsed.turns.map(t=>t.spanMs).join(",")'), '3000,1000');
  assert.equal(v.run('S.parsed.totals.durMs'), 4000);
});

test('timeline and performance details use segment metrics and keep native task timing separate', () => {
  const v = viewer([ctx('t1', 0), msg('user', 0), call('a', 100), done('a', 500, 0, 123000000),
    msg('assistant', 1500), record('event_msg', {type: 'task_complete', turn_id: 't1', duration_ms: 9000}, 1600)]);
  v.run('renderTimeline()');
  assert.ok(v.box.innerHTML.includes('执行耗时 0.123 秒 · 对话耗时 1.5 秒'));
  v.run('S.selTurn=0; S.tab="perf"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>执行耗时</dt><dd>0.123 秒</dd>'));
  assert.ok(v.box.innerHTML.includes('<dt>对话耗时</dt><dd>1.5 秒</dd>'));
  assert.ok(v.box.innerHTML.includes('<dt>任务记录耗时（turn 级）</dt><dd>9 s</dd>'));
  v.run('S.sel={ti:0,idx:1}; S.tab="t_time"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>执行耗时</dt><dd>0.123 秒</dd>'));
  assert.ok(v.box.innerHTML.includes('<dt>对话耗时</dt><dd>1.5 秒</dd>'));
});
