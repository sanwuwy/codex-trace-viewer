const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function viewer(records) {
  const script = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8')
    .split('<script>')[1].split('</script>')[0].replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
  const box = {innerHTML: '', querySelectorAll: () => [], querySelector: () => null};
  const context = vm.createContext({console, fixture: records.map(JSON.stringify).join('\n'),
    localStorage: {getItem: () => null}, document: {querySelector: () => box, querySelectorAll: () => []}});
  const run = code => vm.runInContext(code, context);
  run(script); run('S.parsed=parseRollout(fixture); S.current={name:"usage"}; S.selTurn=0; S.tab="request"');
  return {run, box};
}
const rec = (type, payload) => ({type, payload, timestamp: '2026-10-10T02:00:00Z'});
const ctx = id => rec('turn_context', {turn_id: id, model: 'recorded'});
const msg = (text, id = 'a') => rec('response_item', {type: 'message', role: 'user',
  content: [{type: 'input_text', text}], internal_chat_message_metadata_passthrough: {turn_id: id}});
const usage = (input, cached, output, reasoning = 0) => ({input_tokens: input, cached_input_tokens: cached,
  cache_write_input_tokens: 0, output_tokens: output, reasoning_output_tokens: reasoning, total_tokens: input + output});
const request = (response_id, u, cumulative, id = 'a') => rec('token_usage_record', {
  turn_id: id, response_id, usage: u, turn_token_usage: cumulative, thread_token_usage: usage(999999, 0, 0)});
const token = (u, id = 'a', cw = 10000) => rec('event_msg', {type: 'token_count', turn_id: id,
  info: {last_token_usage: u, total_token_usage: usage(999999, 0, 0), model_context_window: cw}});

test('split rounds sum only their own request records and all views use those independent values', () => {
  const first = usage(1000, 800, 20, 10), second = usage(1000, 900, 30, 15), cumulative = usage(2000, 1700, 50, 25);
  const v = viewer([ctx('a'), msg('first'), request('r1', first, first), token(first),
    msg('second'), request('r2', second, cumulative), token(second)]);
  assert.equal(v.run('S.parsed.turns[0].usage.total_tokens'), 1020);
  assert.equal(v.run('S.parsed.turns[1].usage.total_tokens'), 1030);
  assert.equal(v.run('S.parsed.totals.tokens'), 2050);
  v.run('renderInsp()');
  assert.ok(v.box.innerHTML.includes('本轮用量 · 第 1 轮'));
  assert.ok(v.box.innerHTML.includes('<dt>合计 token</dt><dd>1,020</dd>'));
  assert.ok(!v.box.innerHTML.includes('999,999'));
  v.run('S.selTurn=1; renderInsp()');
  assert.ok(v.box.innerHTML.includes('本轮用量 · 第 2 轮'));
  assert.ok(v.box.innerHTML.includes('<dt>合计 token</dt><dd>1,030</dd>'));
  v.run('renderStream()');
  assert.ok(v.box.innerHTML.includes('20 out'));
  assert.ok(v.box.innerHTML.includes('30 out'));
});

test('cache breakdown is weighted across requests and cached input is one read counter', () => {
  const a = usage(100, 0, 10), b = usage(900, 900, 20);
  const v = viewer([ctx('a'), msg('one'), request('r1', a, a), request('r2', b, usage(1000, 900, 30))]);
  v.run('renderInsp()');
  assert.ok(v.box.innerHTML.includes('90.0%'));
  assert.ok(v.box.innerHTML.includes('<dt>未缓存输入</dt><dd>100</dd>'));
  assert.ok(v.box.innerHTML.includes('<dt>缓存输入（读取）</dt><dd>900</dd>'));
  assert.ok(!v.box.innerHTML.includes('45.0%'));
});

test('repeated response IDs and repeated token snapshots do not duplicate billed request usage', () => {
  const u = usage(100, 80, 10), record = request('r1', u, u);
  const v = viewer([ctx('a'), msg('one'), record, token(u), record, token(u), token(u)]);
  assert.equal(v.run('S.parsed.turns[0].usage.total_tokens'), 110);
  assert.equal(v.run('S.parsed.totals.tokens'), 110);
});

test('compaction request is counted once while its retained context snapshot stays nonzero', () => {
  const u = usage(100, 80, 10), record = request('compaction', u, u);
  const reset = {...usage(0, 0, 0), total_tokens: 40};
  const v = viewer([ctx('a'), msg('one'), record,
    rec('compacted', {latest_token_usage_record: record.payload}), token(reset, 'a', 200)]);
  assert.equal(v.run('S.parsed.turns[0].usage.total_tokens'), 110);
  v.run('renderInsp()');
  assert.ok(v.box.innerHTML.includes('40 / 200 tokens（20.0%）'));
  assert.ok(!v.box.innerHTML.includes('系统提示词 token'));
});

test('late usage records with explicit turn IDs do not enter the current different turn', () => {
  const a = usage(100, 80, 10), b = usage(200, 150, 20);
  const v = viewer([ctx('a'), msg('one'), ctx('b'), msg('two', 'b'),
    request('rb', b, b, 'b'), request('ra', a, a, 'a')]);
  assert.equal(v.run('S.parsed.turns[0].usage.total_tokens'), 110);
  assert.equal(v.run('S.parsed.turns[1].usage.total_tokens'), 220);
});

test('a compaction context reset without request usage is not reported as model consumption', () => {
  const reset = {...usage(0, 0, 0), total_tokens: 40};
  const v = viewer([ctx('a'), msg('one'), token(reset, 'a', 200)]);
  assert.equal(v.run('S.parsed.totals.tokens'), 0);
  v.run('renderInsp()');
  assert.ok(!v.box.innerHTML.includes('<dt>合计 token</dt>'));
  assert.ok(v.box.innerHTML.includes('40 / 200 tokens（20.0%）'));
});

test('duplicates after a new human input remain counted only in their original segment', () => {
  const a = usage(100, 80, 10), b = usage(200, 150, 20), first = request('r1', a, a);
  const v = viewer([ctx('a'), msg('one'), first, msg('two'), first, request('r2', b, usage(300, 230, 30))]);
  assert.equal(v.run('S.parsed.turns[0].usage.total_tokens'), 110);
  assert.equal(v.run('S.parsed.turns[1].usage.total_tokens'), 220);
});

test('zero cache hits stay zero while zero input has no computable hit rate', () => {
  const v = viewer([ctx('a'), msg('one'), request('r', usage(100, 0, 10), usage(100, 0, 10))]);
  v.run('renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>缓存命中率</dt><dd>0.0%</dd>'));
  assert.ok(v.box.innerHTML.includes('<dt>缓存输入（读取）</dt><dd>0</dd>'));
  const zero = viewer([ctx('a'), msg('one'), request('r', usage(0, 0, 0), usage(0, 0, 0))]);
  zero.run('renderInsp()');
  assert.ok(zero.box.innerHTML.includes('<dt>合计 token</dt><dd>0</dd>'));
  assert.ok(!zero.box.innerHTML.includes('<dt>缓存命中率</dt>'));
  assert.ok(!zero.box.innerHTML.includes('NaN'));
});

test('missing and inconsistent cache counters do not invent uncached input or a valid hit rate', () => {
  const u = {...usage(100, 80, 10), cached_input_tokens: 150};
  const v = viewer([ctx('a'), msg('one'), request('r', u, u)]);
  v.run('renderInsp()');
  assert.ok(!v.box.innerHTML.includes('<dt>未缓存输入</dt>'));
  assert.ok(!v.box.innerHTML.includes('<dt>缓存命中率</dt>'));
  assert.ok(v.box.innerHTML.includes('缓存输入超过总输入'));
  const missing = usage(100, 80, 10); delete missing.cached_input_tokens;
  const old = viewer([ctx('a'), msg('one'), request('r', missing, missing)]);
  old.run('renderInsp()');
  assert.ok(!old.box.innerHTML.includes('<dt>未缓存输入</dt>'));
  assert.ok(!old.box.innerHTML.includes('<dt>缓存输入（读取）</dt>'));
});

test('a round without its own snapshots does not borrow a later context or cumulative usage', () => {
  const v = viewer([ctx('a'), msg('one'), msg('two'), token(usage(500, 400, 10)),
    rec('token_usage_record', {turn_id: 'a', turn_token_usage: usage(999, 800, 20)})]);
  v.run('renderInsp()');
  assert.ok(v.box.innerHTML.includes('本轮未记录独立用量'));
  assert.ok(!v.box.innerHTML.includes('<dt>合计 token</dt>'));
  assert.ok(!v.box.innerHTML.includes('最近记录的上下文占用'));
});

test('a missing earlier context window cannot be filled by a later display round', () => {
  const v = viewer([ctx('a'), msg('one'), token(usage(100, 80, 0), 'a', null),
    msg('two'), token(usage(200, 150, 0), 'a', 1000)]);
  assert.equal(v.run('turnMetrics(S.parsed.turns[0]).cw'), null);
  assert.equal(v.run('S.parsed.turns[0].ctxWindow'), null);
  v.run('renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>上下文 token</dt><dd>100</dd>'));
  assert.ok(!v.box.innerHTML.includes('100 / 1,000'));
});

test('a missing context window cannot be carried forward from the previous display round', () => {
  const v = viewer([ctx('a'), msg('one'), token(usage(100, 80, 0), 'a', 1000),
    msg('two'), token(usage(200, 150, 0), 'a', null)]);
  assert.equal(v.run('turnMetrics(S.parsed.turns[1]).cw'), null);
  v.run('S.selTurn=1; renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>上下文 token</dt><dd>200</dd>'));
  assert.ok(!v.box.innerHTML.includes('200 / 1,000'));
});

test('invalid snapshot and native cumulative counters cannot contaminate totals or round headers', () => {
  const bad = {input_tokens: '200', output_tokens: -10, total_tokens: '190'};
  for(const record of [token(bad), rec('token_usage_record', {turn_id: 'a', turn_token_usage: bad})]){
    const v = viewer([ctx('a'), msg('one'), record]);
    assert.equal(v.run('S.parsed.totals.tokens'), 0);
    assert.equal(v.run('S.parsed.totals.inTok'), 0);
    assert.equal(v.run('S.parsed.totals.outTok'), 0);
    v.run('renderStream()');
    assert.ok(!v.box.innerHTML.includes('-10 out'));
  }
});

test('an inconsistent request cache count cannot be hidden by other requests in the aggregate', () => {
  const a = usage(100, 200, 10), b = usage(1000, 0, 20);
  const v = viewer([ctx('a'), msg('one'), request('r1', a, a), request('r2', b, usage(1100, 200, 30))]);
  v.run('renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>缓存输入（读取）</dt><dd>200</dd>'));
  assert.ok(!v.box.innerHTML.includes('<dt>缓存命中率</dt>'));
  assert.ok(!v.box.innerHTML.includes('<dt>未缓存输入</dt>'));
  assert.ok(v.box.innerHTML.includes('缓存输入超过总输入'));
});
