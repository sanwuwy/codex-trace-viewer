const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function viewer(sessions) {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const script = html.split('<script>')[1].split('</script>')[0]
    .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
  const boxes = {'#q': {value: ''}, '#slist': {innerHTML: ''}, '#count': {textContent: ''}, '#stream': {innerHTML: ''}};
  const context = vm.createContext({console, fixture: sessions, AbortController, localStorage: {getItem: () => null},
    document: {querySelector: s => boxes[s]}});
  const run = code => vm.runInContext(code, context);
  run(script);
  run('S.mode="server"; setSessions(fixture)');
  return {run, context, boxes};
}
const session = (id, extra = {}) => ({path: id, thread_id: id, name: id + '.jsonl', title: id, ...extra});

test('projects follow creation time while conversations and recents follow human input time', () => {
  const v = viewer([session('older', {project: 'new-project', project_created_at: 200, last_user_ts: 10}),
    session('newer', {project: 'old-project', project_created_at: 100, last_user_ts: 20}),
    session('child', {parent_thread_id: 'older', last_user_ts: 999})]);
  v.run('buildRows()');
  assert.equal(v.run('S.rows.filter(r=>r.type==="proj").map(r=>r.label).join(",")'), 'new-project,old-project');
  assert.equal(v.run('S.rows.slice(S.rows.findIndex(r=>r.key==="__recent")+1).map(r=>r.s.title).join(",")'), 'newer,older');
  assert.ok(!v.run('S.rows.some(r=>r.s?.title==="child")'));
  assert.equal(v.run('countKids(S.tree.find(s=>s.title==="older"))'), 1);
});

test('recents use index time then file time as fallbacks and exclude unindexed roots', () => {
  const v = viewer([session('human', {last_user_ts: 200, updated_at: '2026-01-01T00:00:00Z'}),
    session('index', {updated_at: '1970-01-01T00:05:00Z', mtime: 999}),
    session('file', {updated_at: 'invalid', mtime: 250}), session('system', {title: '', last_user_ts: 999})]);
  v.run('buildRows()');
  assert.equal(v.run('S.rows.slice(S.rows.findIndex(r=>r.key==="__recent")+1).map(r=>r.s.path).join(",")'), 'index,file,human');
});

test('search includes child sessions and refreshes indexed text when new data arrives', () => {
  const v = viewer([session('root'), session('child', {parent_thread_id: 'root', preview: 'Unique Preview', agent: 'Agent One'})]);
  v.boxes['#q'].value = 'UNIQUE';
  v.run('buildRows()');
  assert.equal(v.run('S.rows.map(r=>r.s.path).join(",")'), 'child');
  assert.ok(v.run('S.rows[0].kid'));
  v.context.next = [session('root', {title: 'renamed'})];
  v.run('setSessions(next); buildRows()');
  assert.equal(v.run('S.rows.length'), 0);
  v.boxes['#q'].value = 'RENAMED';
  v.run('buildRows()');
  assert.equal(v.run('S.rows[0].s.path'), 'root');
});

test('a user-origin root is not labeled as a child in search results', () => {
  const v = viewer([session('root', {context_kind: 'user', thread_source: 'user'})]);
  v.boxes['#q'].value = 'root';
  v.run('buildRows()');
  assert.equal(v.run('S.rows[0].kid'), false);
});

test('opening a local file refreshes both tree membership and searchable metadata', async () => {
  const v = viewer([session('local', {thread_id: '', title: '', preview: ''}), session('parent')]);
  v.context.raw = JSON.stringify({type: 'session_meta', payload: {id: 'local', parent_thread_id: 'parent', cwd: '/loaded-directory'}});
  v.run('S.mode="local"; loadParsed=async()=>parseRollout(raw); for(const name of ["renderSessionBar","renderTimeline","renderFilterBar","renderStream","renderTabs","renderInsp"]) eval(name+"=()=>{}"); buildRows()');
  const i = v.run('S.rows.findIndex(r=>r.s?.path==="local")');
  await v.run(`openSession(${i})`);
  assert.equal(v.run('S.tree.length'), 1);
  v.boxes['#q'].value = 'loaded-directory';
  v.run('buildRows()');
  assert.equal(v.run('S.rows[0].s.path'), 'local');
  assert.ok(v.run('S.rows[0].kid'));
});

test('local project grouping accepts Windows and POSIX paths without changing the recorded cwd', () => {
  for(const cwd of ['C:\\Users\\example\\code\\demo', 'C:\\Users\\example\\code\\demo\\',
    '/home/example/code/demo', '/home/example/code/demo/']){
    const v = viewer([session('local', {cwd})]);
    v.run('S.mode="local"; buildRows()');
    assert.equal(v.run('S.rows.find(r=>r.type==="proj").label'), 'demo');
    assert.equal(v.run('S.sessions[0].cwd'), cwd);
  }
});
