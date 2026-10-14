const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function viewer(records) {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const script = html.split('<script>')[1].split('</script>')[0]
    .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
  const box = {innerHTML: '', querySelectorAll: () => [], querySelector: () => null};
  const context = vm.createContext({console, fixture: records.map(JSON.stringify).join('\n'),
    localStorage: {getItem: () => null}, document: {querySelector: () => box, querySelectorAll: () => []}});
  const run = code => vm.runInContext(code, context);
  run(script); run('S.parsed=parseRollout(fixture); S.current={name:"sample"}; S.selTurn=0');
  return {run, box, html};
}
const rec = (type, payload) => ({type, payload, timestamp: '2026-10-10T02:00:00Z'});
const ctx = (extra = {}) => rec('turn_context', {turn_id: 't1', model: 'historical', effort: 'high', ...extra});
const msg = (role, text, id = 't1') => rec('response_item', {type: 'message', role,
  content: [{type: 'input_text', text}], internal_chat_message_metadata_passthrough: {turn_id: id}});

test('round configuration never fills missing historical fields from the latest thread settings', () => {
  const settings = rec('event_msg', {type: 'thread_settings_applied', thread_settings: {model: 'future-model', cwd: '/future'}});
  const v = viewer([ctx(), msg('user', 'hello'), settings]);
  v.run('S.tab="model"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('historical'));
  assert.ok(!v.box.innerHTML.includes('/future'));
  assert.ok(!v.box.innerHTML.includes('future-model'));
  const missing = viewer([msg('user', 'hello'), settings]);
  missing.run('S.tab="model"; renderInsp()');
  assert.ok(missing.box.innerHTML.includes('未记录'));
  assert.ok(!missing.box.innerHTML.includes('future-model'));
});

test('default collaboration mode is omitted from round and message summaries but retained in raw context', () => {
  const v = viewer([ctx({collaboration_mode: {mode: 'default'}, approval_policy: 'default'}),
    rec('event_msg', {type: 'task_started', collaboration_mode_kind: 'default'}), msg('user', 'hello')]);
  v.run('renderStream()');
  assert.equal((v.box.innerHTML.match(/<span class="chip">default<\/span>/g) || []).length, 1);
  v.run('S.tab="model"; renderInsp()');
  assert.ok(!v.box.innerHTML.includes('<dt>协作模式</dt>'));
  assert.ok(v.box.innerHTML.includes('<dt>审批策略</dt><dd>default</dd>'));
  v.run('S.sel={ti:0,idx:0}; S.tab="m_context"; renderInsp()');
  assert.ok(!v.box.innerHTML.includes('<dt>协作模式</dt>'));
  v.run('S.sel=null; S.tab="raw"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('&quot;mode&quot;: &quot;default&quot;'));
});

test('nondefault collaboration mode remains visible for its round and message context', () => {
  const v = viewer([ctx({collaboration_mode: {mode: 'plan'}}),
    rec('event_msg', {type: 'task_started', collaboration_mode_kind: 'plan'}), msg('user', 'hello')]);
  v.run('renderStream()');
  assert.ok(v.box.innerHTML.includes('<span class="chip">plan</span>'));
  v.run('S.tab="model"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>协作模式</dt><dd>plan</dd>'));
  v.run('S.sel={ti:0,idx:0}; S.tab="m_context"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>协作模式</dt><dd>plan</dd>'));
});

test('unknown network access stays unknown and configuration objects are complete collapsed records', () => {
  const long = '/workspace/' + 'segment/'.repeat(40) + 'END_ROOT';
  const v = viewer([ctx({cwd: '/actual', sandbox_policy: {type: 'workspace-write'},
    runtime_workspace_roots: [long], summary: 'detailed', personality: '<script>data</script>'}), msg('user', 'hello')]);
  v.run('S.tab="model"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>网络访问</dt><dd>未记录</dd>'));
  assert.ok(v.box.innerHTML.includes('<details class="sec"><summary>更多配置</summary>'));
  assert.ok(v.box.innerHTML.includes('<details class="sec"><summary>本轮上下文原始记录</summary>'));
  assert.ok(v.box.innerHTML.includes('END_ROOT'));
  assert.ok(!v.box.innerHTML.includes('<script>data</script>'));
  assert.ok(!v.box.innerHTML.includes('<dt>runtime_workspace_roots</dt>'));
});

test('selecting a round shows only that round overview and clears a selection from the same round', () => {
  const v = viewer([ctx(), msg('user', 'one'), msg('assistant', 'reply'),
    rec('turn_context', {turn_id: 't2', model: 'other'}), msg('user', 'two', 't2')]);
  v.run('S.sel={ti:0,idx:1}; setSelTurn(0)');
  assert.equal(v.run('S.sel'), null);
  v.run('S.tab="overview"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('第 1 轮'));
  assert.ok(v.box.innerHTML.includes('轮次概览'));
  assert.ok(!v.box.innerHTML.includes('会话总览'));
  assert.ok(v.box.innerHTML.includes('执行耗时'));
  assert.ok(v.box.innerHTML.includes('对话耗时'));
});

test('usage distinguishes native turn totals from recent request usage without account quotas', () => {
  const v = viewer([ctx(), msg('user', 'hello'), rec('event_msg', {type: 'token_count',
    info: {last_token_usage: {input_tokens: 0, output_tokens: 25, total_tokens: 25}, model_context_window: 100},
    rate_limits: {plan_type: 'account-plan'}})]);
  v.run('S.tab="request"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('最近模型请求'));
  assert.ok(v.box.innerHTML.includes('<dt>输入 token</dt><dd>0</dd>'));
  assert.ok(!v.box.innerHTML.includes('本轮输入'));
  assert.ok(!v.box.innerHTML.includes('account-plan'));
  assert.equal(v.run('TABS.find(x=>x[0]==="request")[1]'), '用量');
});

test('timing keeps recorded metrics and does not infer model generation speed from task duration', () => {
  const v = viewer([ctx(), msg('assistant', 'reply'), rec('event_msg', {
    type: 'task_complete', turn_id: 't1', duration_ms: 9000, time_to_first_token_ms: 0}),
    rec('token_usage_record', {turn_id: 't1', turn_token_usage: {output_tokens: 900}})]);
  v.run('S.tab="perf"; renderInsp()');
  assert.ok(!v.box.innerHTML.includes('生成阶段'));
  assert.ok(!v.box.innerHTML.includes('输出速率'));
  assert.ok(v.box.innerHTML.includes('任务记录耗时（turn 级）'));
  assert.ok(v.box.innerHTML.includes('0 ms'));
});

test('split round configuration and raw context stay within their own display segment', () => {
  const v = viewer([ctx(), msg('user', 'first'), msg('assistant', 'one'),
    ctx({model: 'later-snapshot'}), msg('user', 'second'), msg('assistant', 'two')]);
  v.run('S.tab="model"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('historical'));
  assert.ok(!v.box.innerHTML.includes('later-snapshot'));
  v.run('S.tab="raw"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('historical'));
  assert.ok(!v.box.innerHTML.includes('later-snapshot'));
  v.run('S.selTurn=1; S.tab="model"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('later-snapshot'));
});

test('split rounds without per-request records use only their own snapshots and retain zero context occupancy', () => {
  const v = viewer([ctx(), msg('user', 'first'), msg('assistant', 'one'),
    rec('event_msg', {type: 'token_count', info: {last_token_usage: {input_tokens: 40, output_tokens: 10, total_tokens: 50}}}),
    msg('user', 'second'), msg('assistant', 'two'), rec('event_msg', {type: 'token_count',
      info: {last_token_usage: {total_tokens: 0}, model_context_window: 100}}),
    rec('token_usage_record', {turn_id: 't1', turn_token_usage: {input_tokens: 250, total_tokens: 250},
      thread_token_usage: {total_tokens: 500}})]);
  v.run('S.tab="request"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('最近模型请求'));
  assert.ok(v.box.innerHTML.includes('<dt>合计 token</dt><dd>50</dd>'));
  assert.ok(!v.box.innerHTML.includes('<dd>250</dd>'));
  assert.ok(!v.box.innerHTML.includes('线程累计'));
  v.run('S.selTurn=1; renderInsp()');
  assert.ok(v.box.innerHTML.includes('<dt>合计 token</dt><dd>0</dd>'));
  assert.ok(!v.box.innerHTML.includes('<dd>250</dd>'));
  assert.ok(v.box.innerHTML.includes('0 / 100 tokens（0.0%）'));
});

test('selecting the same or another round clears the old step highlight with the selected state', () => {
  const v = viewer([ctx(), msg('user', 'one'), msg('assistant', 'reply'),
    rec('turn_context', {turn_id: 't2'}), msg('user', 'two', 't2')]);
  v.run(`const stepNodes=[0,1].map(idx=>({dataset:{turn:"0",step:String(idx)},classList:{
    selected:false, toggle(name,on){if(name==="sel") this.selected=on;}, remove(name){if(name==="sel") this.selected=false;}
  }})); document.querySelectorAll=selector=>selector==="#stream .step"? stepNodes
    :selector==="#stream .step.sel"? stepNodes.filter(n=>n.classList.selected):[];`);
  v.run('selectStep(0,1)');
  assert.equal(v.run('stepNodes[1].classList.selected'), true);
  v.run('setSelTurn(0)');
  assert.equal(v.run('stepNodes.some(n=>n.classList.selected)'), false);
  v.run('selectStep(0,1); setSelTurn(1)');
  assert.equal(v.run('stepNodes.some(n=>n.classList.selected)'), false);
  assert.equal(v.run('S.sel'), null);
});
