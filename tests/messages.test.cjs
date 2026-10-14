const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function viewer(records) {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const script = html.split('<script>')[1].split('</script>')[0]
    .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
  const box = {innerHTML: ''};
  const context = vm.createContext({console, localStorage: {getItem: () => null},
    document: {querySelector: () => box}, fixture: records.map(JSON.stringify).join('\n')});
  const run = code => vm.runInContext(code, context);
  run(script);
  run('S.parsed=parseRollout(fixture); S.sel={ti:0,idx:0}');
  return {run, context, box};
}
const record = (type, payload) => ({type, timestamp: '2026-10-10T02:00:00Z', payload});
const ctx = (turn_id = 't1', model = 'original') => record('turn_context', {turn_id, model, effort: 'high'});
const msg = (role, text, extra = {}) => record('response_item', {type: 'message', role,
  content: [{type: role === 'user' ? 'input_text' : 'output_text', text}],
  internal_chat_message_metadata_passthrough: {turn_id: 't1'}, ...extra});

test('user and assistant messages receive role-specific detail tabs', () => {
  const v = viewer([ctx(), msg('user', 'hello'), msg('assistant', 'reply', {phase: 'final_answer'})]);
  assert.equal(v.run('tabsFor().map(x=>x[0]).join(",")'), 'm_message,m_context,m_raw');
  v.run('S.sel.idx=1');
  assert.equal(v.run('tabsFor().map(x=>x[0]).join(",")'), 'm_message,m_generation,m_context,m_raw');
});

test('explicit user content remains visible beside attached environment blocks', () => {
  const v = viewer([ctx(), msg('user', '', {
    content: [{type: 'input_text', text: '# Files mentioned by the user: attachment'},
      {type: 'input_text', text: 'Please inspect this'}, {type: 'input_image', image_url: 'data:image/png;base64,AA=='}],
    internal_chat_message_metadata_passthrough: {turn_id: 't1',
      content_item_kinds: ['environments.environment_context', 'user.text', 'user.image']}})]);
  const st = v.run('selectedStep()');
  assert.equal(st.kind, 'msg');
  assert.equal(st.role, 'user');
  assert.ok(st.text.includes('Please inspect this'));
  assert.ok(!st.text.includes('# Files'));
  v.run('S.tab="m_message"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('图片 1'));
  assert.ok(!v.box.innerHTML.includes('base64'));
});

test('message context remains historical when the same turn context or thread settings change', () => {
  const v = viewer([ctx(), msg('assistant', 'first'), ctx('t1', 'later'),
    record('event_msg', {type: 'thread_settings_applied', thread_settings: {model: 'newest'}}),
    msg('assistant', 'second')]);
  assert.equal(v.run('selectedStep().context.model'), 'original');
  v.run('S.tab="m_context"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('original'));
  assert.ok(!v.box.innerHTML.includes('newest'));
  v.run('S.sel.idx=1');
  assert.equal(v.run('selectedStep().context.model'), 'later');
});

test('message that precedes its matching context is resolved by turn ID without future settings fallback', () => {
  const v = viewer([msg('assistant', 'early'), ctx(),
    record('event_msg', {type: 'thread_settings_applied', thread_settings: {model: 'newest'}})]);
  assert.equal(v.run('selectedStep().context.model'), 'original');
  const missing = viewer([msg('assistant', 'unknown'),
    record('event_msg', {type: 'thread_settings_applied', thread_settings: {model: 'newest'}})]);
  missing.run('S.tab="m_context"; renderInsp()');
  assert.ok(missing.box.innerHTML.includes('未记录关联的 turn_context'));
  assert.ok(!missing.box.innerHTML.includes('newest'));
});

test('message body is complete and escaped while creation and record times have distinct labels', () => {
  const text = '<script>unsafe</script>\n' + 'x'.repeat(42000) + 'END_OF_MESSAGE';
  const v = viewer([ctx(), msg('assistant', text, {phase: 'commentary',
    internal_chat_message_metadata_passthrough: {turn_id: 't1', create_time: 1791597599.5}})]);
  v.run('S.tab="m_message"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('END_OF_MESSAGE'));
  assert.ok(v.box.innerHTML.includes('&lt;script&gt;'));
  assert.ok(!v.box.innerHTML.includes('<script>unsafe'));
  assert.ok(v.box.innerHTML.includes('进度说明'));
  assert.ok(v.box.innerHTML.includes('创建时间'));
  assert.ok(v.box.innerHTML.includes('记录时间'));
  assert.ok(!v.box.innerHTML.includes('线程设置'));
  assert.ok(!v.box.innerHTML.includes('审批策略'));
});

test('generation details label whole-turn statistics and keep zero values without assigning them to a message', () => {
  const v = viewer([ctx(), msg('assistant', 'reply'), record('event_msg', {
    type: 'task_complete', turn_id: 't1', duration_ms: 0, time_to_first_token_ms: 0}),
    record('token_usage_record', {turn_id: 't1', turn_token_usage: {input_tokens: 100, output_tokens: 0, total_tokens: 100}})]);
  v.run('S.tab="m_generation"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('本轮用量'));
  assert.ok(v.box.innerHTML.includes('未记录单条消息的 token 或生成耗时'));
  assert.ok(v.box.innerHTML.includes('0 ms'));
  assert.ok(!v.box.innerHTML.includes('输出速率'));
  assert.ok(!v.box.innerHTML.includes('套餐'));
});

test('split visual turns use independent request usage while preserving explicit native task timing', () => {
  const v = viewer([ctx(), msg('user', 'first'), msg('assistant', 'one'),
    record('token_usage_record', {turn_id: 't1', response_id: 'first', usage: {output_tokens: 100}, turn_token_usage: {output_tokens: 100}}),
    msg('user', 'second'), msg('assistant', 'two'), record('event_msg', {
      type: 'task_complete', turn_id: 't1', duration_ms: 900, time_to_first_token_ms: 50}),
    record('token_usage_record', {turn_id: 't1', response_id: 'second', usage: {output_tokens: 200}, turn_token_usage: {output_tokens: 300}})]);
  v.run('S.sel={ti:1,idx:1}; S.tab="m_generation"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('900 ms'));
  assert.ok(v.box.innerHTML.includes('<dt>输出 token</dt><dd>200</dd>'));
  assert.ok(!v.box.innerHTML.includes('<dd>300</dd>'));
  assert.ok(v.box.innerHTML.includes('共用同一个 turn'));
});

test('file attachment envelopes put the explicit request first and retain the bundled text in a collapsed section', () => {
  const bundled = '# Files mentioned by the user:\nfile info\n\n## My request:\nActual request\n<image name=[Image #1] path="/tmp/example.png">\n</image>';
  const v = viewer([ctx(), msg('user', bundled, {
    internal_chat_message_metadata_passthrough: {turn_id: 't1', content_item_kinds: ['user.text']}})]);
  v.run('S.tab="m_message"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('<h4>用户请求</h4>'));
  assert.ok(v.box.innerHTML.includes('<div class="message-body">Actual request</div>'));
  assert.ok(v.box.innerHTML.includes('<summary>附件清单及附加说明</summary>'));
  assert.ok(v.box.innerHTML.includes('file info'));
});

test('last_token_usage is labeled as recent model request usage rather than a whole-turn total', () => {
  const v = viewer([ctx(), msg('assistant', 'reply'), record('event_msg', {type: 'token_count',
    info: {last_token_usage: {input_tokens: 123, output_tokens: 45}}})]);
  v.run('S.tab="m_generation"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('最近模型请求'));
  assert.ok(v.box.innerHTML.includes('无法还原本轮完整累计'));
  assert.ok(v.box.innerHTML.includes('123'));
});

test('unknown phase and content type strings cannot resolve object prototype properties', () => {
  const v = viewer([ctx(), msg('assistant', '', {phase: 'constructor', content: [{type: '__proto__'}]})]);
  v.run('S.tab="m_message"; renderInsp()');
  assert.ok(v.box.innerHTML.includes('<h4>助手消息</h4>'));
  assert.ok(v.box.innerHTML.includes('__proto__ 1'));
  assert.ok(!v.box.innerHTML.includes('function Object'));
});
