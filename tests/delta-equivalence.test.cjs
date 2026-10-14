const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');
const assert = require('node:assert/strict');
const test = require('node:test');
const root = path.join(__dirname, '..');
const main = fs.readFileSync(path.join(root, 'index.html'), 'utf8').split('<script>')[1].split('</script>')[0]
  .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
const helpers = fs.readFileSync(path.join(root, 'plugins/codex-trace-probe/viewer-bridge.js'), 'utf8').split('/* Injected')[0];
const thread = '11111111-2222-4333-8444-555555555555';
const rec = (type, payload, ms=0) => ({type,payload,timestamp:new Date(ms).toISOString()});
const ctx = id => rec('turn_context',{turn_id:id,model:'recorded',model_context_window:2000});
const msg = (role,text,id,ms) => rec('response_item',{type:'message',role,
  content:[{type:role==='user'?'input_text':'output_text',text}],internal_chat_message_metadata_passthrough:{turn_id:id}},ms);
const usage = (id,response,input,output,ms) => rec('token_usage_record',{turn_id:id,response_id:response,
  usage:{input_tokens:input,cached_input_tokens:input/2,output_tokens:output,reasoning_output_tokens:output/2,
    total_tokens:input+output},thread_token_usage:{total_tokens:999999}},ms);
const jsonl = records => Buffer.from(records.map(r=>JSON.stringify(r)).join('\n')+'\n');
const text = '中文🙂'.repeat(1000);
const initial = jsonl([rec('session_meta',{id:thread,cwd:'/synthetic'}),ctx('a'),
  rec('event_msg',{type:'task_started',turn_id:'a'}),msg('user','first','a',1),
  rec('response_item',{type:'custom_tool_call',name:'exec',call_id:'late',input:'text(1)'},2),usage('a','r1',100,20,3)]);
const changes = [
  jsonl([msg('user','steering','a',4),usage('a','r1',100,20,5),usage('a','r2',200,30,6),ctx('b'),msg('user','second','b',10),
    rec('response_item',{type:'custom_tool_call_output',call_id:'late',output:'Script completed',
      internal_chat_message_metadata_passthrough:{turn_id:'a'}},11),
    rec('event_msg',{type:'item_completed',turn_id:'a',item:{type:'CommandExecution',id:'late',duration:{secs:1,nanos:234000000},
      command:['sh','-c','true'],exit_code:0,aggregated_output:text}},12),usage('b','r3',300,40,13)]),
  Buffer.concat([Buffer.from('\n{malformed}\n'),jsonl([
    rec('event_msg',{type:'token_count',info:{last_token_usage:{input_tokens:90,output_tokens:10,total_tokens:100},
      total_token_usage:{input_tokens:900,output_tokens:100,total_tokens:1000},model_context_window:1000}},14),
    rec('compacted',{message:'kept',replacement_history:[{type:'message',content:text}],retained_context:{text}},15),
    rec('event_msg',{type:'token_count',info:{last_token_usage:{input_tokens:80,output_tokens:10,total_tokens:90},
      total_token_usage:{input_tokens:80,output_tokens:10,total_tokens:90},model_context_window:1000}},16),
    msg('assistant','second answer','b',17),rec('event_msg',{type:'task_complete',turn_id:'b',duration_ms:7000,time_to_first_token_ms:5},18)])])
];
const pending = jsonl([msg('user','incomplete 中文🙂 line','c',20)]);
const split = pending.indexOf(Buffer.from('🙂'))+2;
changes.push(pending.subarray(0,split),pending.subarray(split));
const operations = [{type:'replace',bytes:initial},...changes.map(bytes=>({type:'append',bytes}))];
let rewritten = Buffer.concat([initial,...changes]).toString().replace('"text":"first"','"text":"FIRST"');
operations.push({type:'replace',bytes:Buffer.from(rewritten)});

const python = String.raw`
import base64, importlib.util, json, os, pathlib, sys, tempfile
q=json.load(sys.stdin)
spec=importlib.util.spec_from_file_location('delta_audit',pathlib.Path.cwd()/'plugins/codex-trace-probe/server.py')
plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
with tempfile.TemporaryDirectory(prefix='trace-delta-equivalence-') as directory:
    os.environ['CODEX_HOME']=directory
    directory=pathlib.Path(directory);(directory/'sessions').mkdir()
    source=directory/'sessions'/('rollout-'+q['thread']+'.jsonl')
    store=plugin.TraceStore();view='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';revision=None;states=[]
    try:
        for op in q['operations']:
            with source.open('ab' if op['type']=='append' else 'wb') as f:f.write(base64.b64decode(op['bytes']))
            manifest=store.snapshot(q['thread'],view,revision,'outline')
            chunks=[store.chunk(q['thread'],view,manifest['snapshot_id'],i) for i in range(manifest['total_chunks'])]
            wire=b''.join(base64.b64decode(c['data']) for c in chunks).decode()
            indices=sorted(store.views[view]['record_offsets'])
            detail=store.detail(q['thread'],view,manifest['snapshot_id'],indices)
            raw=b''.join(base64.b64decode(store.chunk(q['thread'],view,manifest['snapshot_id'],i,detail['detail_id'])['data'])
                for i in range(detail['total_chunks'])).decode()
            states.append({'manifest':manifest,'wire':wire,'detail':raw,'chunks':[{k:v for k,v in c.items() if k!='data'} for c in chunks]})
            revision=manifest['session']['revision']
        print(json.dumps(states))
    finally:store.close()
`;
function backend(ops=operations) {
  const result=spawnSync(process.env.PYTHON||'python3',['-B','-c',python],{cwd:root,encoding:'utf8',maxBuffer:16*1024*1024,
    input:JSON.stringify({thread,operations:ops.map(op=>({type:op.type,bytes:op.bytes.toString('base64')}))})});
  assert.equal(result.status,0,result.stderr);return JSON.parse(result.stdout);
}
function viewer() {
  const box={innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null};
  const state=vm.createContext({console,localStorage:{getItem:()=>null},document:{querySelector:()=>box,querySelectorAll:()=>[]}});
  const run=code=>vm.runInContext(code,state);run(main);run(helpers);
  run('var outline=null;function ownTurn(t){const own={...t};delete own.sourceTurn;return own;}');
  return {state,run};
}
test('actual outline appends preserve all round statistics and exact raw history across late results, resets and rewrites',()=>{
  const states=backend(),v=viewer();let source=Buffer.alloc(0),previous=null;
  assert.equal(v.run('typeof appendTraceOutline'),'function');
  for(let i=0;i<states.length;i++) {
    const op=operations[i],s=states[i],m=s.manifest;
    source=op.type==='append'?Buffer.concat([source,op.bytes]):op.bytes;
    v.state.source=source.toString();v.state.rows=s.wire.trim().split('\n').filter(Boolean).map(JSON.parse);
    v.state.raw=s.detail.trim().split('\n').filter(Boolean).map(JSON.parse);
    v.run('var prior=outline;outline=prior&&'+JSON.stringify(m.status==='delta')+'?appendTraceOutline(prior,rows):createTraceOutline(rows);var full=parseRollout(source);');
    assert.equal(v.run('traceSummarySignature(outline.parsed)'),v.run('traceSummarySignature(full)'),`statistics state ${i}`);
    for(const field of ['meta','world','settings','rateLimits','compacts'])
      assert.equal(v.run(`JSON.stringify(outline.parsed.${field})`),v.run(`JSON.stringify(full.${field})`),`global ${field} state ${i}`);
    assert.equal(v.run('outline.parsed.totals.durMs'),v.run('outline.parsed.turns.reduce((n,t)=>n+t.spanMs,0)'));
    assert.equal(v.run('outline.records.length'),m.record_count);
    if(i>0&&i<3) {
      assert.equal(m.status,'delta',`append state ${i}`);assert.equal(m.base_revision,previous.session.revision);
      assert.equal(m.start_offset,previous.total_bytes);
      assert.equal(v.run('prior.records.every((r,i)=>r===outline.records[i])'),true,'prefix record objects must be reused');
      if(s.chunks.length)assert.equal(s.chunks[0].byte_offset,m.start_offset);
      assert.equal(Buffer.byteLength(s.wire),m.total_bytes-m.start_offset);
    } else assert.equal(m.status,'ready','incomplete UTF8 completion and rewrite require full replacement');
    for(let ti=0;ti<v.run('full.turns.length');ti++) {
      v.state.ti=ti;
      v.run('var wanted=new Set(traceRoundRecordIndices(outline,ti));var detail=raw.filter(r=>wanted.has(r.index));');
      assert.equal(v.run('detail.length'),v.run('wanted.size'),`state ${i} round ${ti}: wanted ${v.run('JSON.stringify([...wanted])')} raw ${v.run('JSON.stringify(raw.map(r=>r.index))')}`);
      v.run('var hydrated=hydrateTraceRound(outline,ti,detail);');
      assert.equal(v.run('traceSummarySignature(hydrated)'),v.run('traceSummarySignature(full)'));
      assert.equal(v.run('JSON.stringify(ownTurn(hydrated.turns[ti]))'),v.run('JSON.stringify(ownTurn(full.turns[ti]))'),`raw state ${i} round ${ti}`);
    }
    previous=m;
  }
});

test('reused round details match the full parser through source-turn timing, contexts, usage and steering changes',()=>{
  const input=[rec('session_meta',{id:thread,cwd:'/synthetic',dynamic_tools:[{name:'recorded',inputSchema:{type:'object'}}]}),
    rec('world_state',{cwd:'/synthetic',marker:'initial'}),
    rec('event_msg',{type:'thread_settings_applied',thread_settings:{model:'original'}}),ctx('a'),
    msg('user','first','a',1),msg('assistant','first reply','a',2),usage('a','r1',100,20,3),
    msg('user','steering','a',4),msg('assistant','second reply','a',5),usage('a','r2',200,30,6),
    msg('user','third input','a',7),
    rec('response_item',{type:'custom_tool_call',name:'exec',call_id:'late',input:'text(1)'},8),
    ctx('b'),ctx('b'),msg('user','other turn','b',10),usage('b','b1',300,40,11)];
  const appends=[
    [msg('assistant','another turn changed','b',12),usage('b','b2',400,50,13)],
    [rec('event_msg',{type:'task_complete',turn_id:'a',duration_ms:9000,time_to_first_token_ms:37,
      completed_at:new Date(14).toISOString(),last_agent_message:'completed'},14)],
    [rec('event_msg',{type:'task_complete',turn_id:'a',duration_ms:10000,time_to_first_token_ms:41,
      completed_at:new Date(15).toISOString(),last_agent_message:'updated'},15)],
    [rec('turn_context',{turn_id:'a',model:'replacement',cwd:'/new-context',effort:'high'},16)],
    [rec('response_item',{type:'custom_tool_call_output',call_id:'late',output:'late result',
      internal_chat_message_metadata_passthrough:{turn_id:'a'}},17),
      rec('event_msg',{type:'item_completed',turn_id:'a',item:{type:'CommandExecution',id:'late',
        duration:{secs:2,nanos:345000000},command:['sh','-c','true'],exit_code:0,aggregated_output:text}},18)],
    [usage('a','r2',999,99,19),usage('a','r3',500,60,20),
      rec('event_msg',{type:'task_started',turn_id:'a',collaboration_mode_kind:'plan',model_context_window:4000},21)],
    [rec('world_state',{cwd:'/updated',marker:'latest'}),
      rec('event_msg',{type:'thread_settings_applied',thread_settings:{model:'global-latest'}}),
      msg('user','fourth input','a',22),usage('a','r4',600,70,23)],
    [rec('event_msg',{type:'token_count',turn_id:'b',rate_limits:{primary:{used_percent:20}},info:{
      last_token_usage:{input_tokens:90,output_tokens:10,total_tokens:100},
      total_token_usage:{input_tokens:900,output_tokens:100,total_tokens:1000},model_context_window:2000}},24),
      rec('compacted',{message:'summary',replacement_history:[{type:'message',content:text}],retained_context:{text}},25),
      rec('event_msg',{type:'token_count',turn_id:'b',info:{
        last_token_usage:{input_tokens:10,output_tokens:1,total_tokens:11},
        total_token_usage:{input_tokens:10,output_tokens:1,total_tokens:11},model_context_window:2000}},26)],
    [ctx('b')]
  ];
  const ops=[{type:'replace',bytes:jsonl(input)},...appends.map(records=>({type:'append',bytes:jsonl(records)}))];
  const states=backend(ops),v=viewer();let source=Buffer.alloc(0),reuseHits=0,reuseMisses=0;
  assert.equal(v.run('typeof createTraceDetailCache'),'function');
  assert.equal(v.run('typeof reuseTraceRound'),'function');
  v.run('var caches=new Map();');
  for(let i=0;i<states.length;i++) {
    const s=states[i],m=s.manifest;
    source=Buffer.concat([source,ops[i].bytes]);
    assert.equal(m.status,i?'delta':'ready');
    v.state.source=source.toString();
    v.state.rows=s.wire.trim().split('\n').filter(Boolean).map(JSON.parse);
    v.state.raw=s.detail.trim().split('\n').filter(Boolean).map(JSON.parse);
    v.run('var prior=outline;outline=prior?appendTraceOutline(prior,rows):createTraceOutline(rows);var full=parseRollout(source);');
    for(let ti=0;ti<v.run('full.turns.length');ti++) {
      v.state.ti=ti;
      v.run('var key=turnAnchor(outline.parsed.turns[ti]);var old=caches.get(key);var reused=reuseTraceRound(outline,ti,old);');
      const reused=v.run('!!reused');
      if(i===1&&ti<2) assert.equal(reused,true,'unchanged historical segment must reuse parsed details');
      if(i===3&&ti===0) assert.equal(reused,false,'task-complete updates first segment without adding detail indices');
      if(i===3&&ti===1) {
        assert.equal(reused,true,'middle segment reuses details while shared task timing changes');
        assert.equal(v.run('JSON.stringify(traceRoundRecordIndices(prior,ti))'),
          v.run('JSON.stringify(traceRoundRecordIndices(outline,ti))'));
      }
      if(i===9&&v.run('outline.parsed.turns[ti].id')==='b') {
        assert.equal(v.run('traceRoundSignature(prior.parsed.turns[ti])'),v.run('traceRoundSignature(outline.parsed.turns[ti])'));
        assert.equal(reused,false,'identical context values from a new source record still require detail hydration');
      }
      if(reused) reuseHits++;else if(v.run('!!old')) reuseMisses++;
      v.run('var wanted=new Set(traceRoundRecordIndices(outline,ti));var detail=raw.filter(r=>wanted.has(r.index));'+
        'var parsed=reused||hydrateTraceRound(outline,ti,detail);'+
        'var next=createTraceDetailCache(outline,ti,detail,parsed);var hydrated=reuseTraceRound(outline,ti,next);');
      assert.equal(v.run('traceSummarySignature(hydrated)'),v.run('traceSummarySignature(full)'),`stats state ${i} round ${ti}`);
      assert.equal(v.run('JSON.stringify(ownTurn(hydrated.turns[ti]))'),v.run('JSON.stringify(ownTurn(full.turns[ti]))'),
        `selected raw state ${i} round ${ti}`);
      for(const field of ['meta','world','settings','rateLimits','compacts','dynTools','recs'])
        assert.equal(v.run(`JSON.stringify(hydrated.${field})`),v.run(`JSON.stringify(full.${field})`),`global ${field} state ${i}`);
      for(const field of ['durationMs','ttftMs','startedAt','completedAt','lastMsg','collab','usage','threadUsage'])
        assert.equal(v.run(`JSON.stringify(hydrated.turns[ti].sourceTurn?.${field})`),
          v.run(`JSON.stringify(full.turns[ti].sourceTurn?.${field})`),`shared ${field} state ${i} round ${ti}`);
      assert.equal(v.run('hydrated.totals.durMs'),v.run('hydrated.turns.reduce((n,t)=>n+t.spanMs,0)'));
      assert.equal(v.run('hydrated.totals.toolMs'),v.run('hydrated.turns.reduce((n,t)=>n+t.toolMs,0)'));
      v.run('caches.set(key,next);');
      assert.equal(v.run('Object.hasOwn(next.turn,"sourceTurn")'),false,'cache must not retain the old native-turn graph');
    }
  }
  assert.ok(reuseHits>=4,'fixture must exercise successful reuse across several append revisions');
  assert.ok(reuseMisses>=4,'changed history must invalidate reuse across several append revisions');
});
