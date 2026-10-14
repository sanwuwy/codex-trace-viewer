const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const root = require('node:path').join(__dirname,'..');
const main=fs.readFileSync(root+'/index.html','utf8').split('<script>')[1].split('</script>')[0].replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/,'');
const adapter=fs.readFileSync(root+'/plugins/codex-trace-probe/viewer-bridge.js','utf8');
const thread='aaaaaaaa-bbbb-cccc-dddd-000000000001', snapshot='11111111-2222-3333-4444-555555555555';
const server='22222222-3333-4444-5555-666666666666', detail='33333333-4444-5555-6666-777777777777';
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const rec=(type,payload)=>({type,payload,timestamp:'2026-10-13T01:00:00Z'});
const context=id=>rec('turn_context',{turn_id:id,model:'test'});
const message=(id,text)=>rec('response_item',{type:'message',role:'user',content:[{type:'input_text',text}],internal_chat_message_metadata_passthrough:{turn_id:id}});
const source=[rec('session_meta',{id:thread}),context('t1'),message('t1','first'),
  rec('response_item',{type:'custom_tool_call',name:'exec',call_id:'late',input:'text("ok")'}),
  context('t2'),message('t2','second'),rec('event_msg',{type:'item_completed',turn_id:'t2',item:{type:'McpToolCall',id:'mcp',tool:'example',result:{isError:false,content:[{text:'PRIVATE SECOND DETAIL'}]}}}),
  context('t3'),message('t3','third'),rec('compacted',{message:'summary',replacement_history:[{text:'PRIVATE THIRD DETAIL'}]}),
  rec('event_msg',{type:'item_completed',turn_id:'t1',item:{type:'CommandExecution',id:'late',duration:{secs:1},exit_code:0,aggregated_output:'PRIVATE FIRST DETAIL'}})];
const projection=source.map(record=>{const r=structuredClone(record); if(r.type==='compacted') delete r.payload.replacement_history;
  const item=r.payload.item;if(item?.type==='CommandExecution') delete item.aggregated_output;
  if(item?.type==='McpToolCall') item.result={isError:false};return r;});
const rows=records=>records.map((record,index)=>({index:index*2,record}));
const encode=records=>Buffer.from(records.map(JSON.stringify).join('\n')+'\n');
const nextSnapshot='44444444-5555-6666-7777-888888888888';
async function loadedPage(){const p=page(),boot=p.start();await tick();await tick();await tick();await p.finishDetail();await boot;return p;}
async function beginDelta(p,added,extra={},body){
  const refresh=p.run('refreshScoped()');await tick();const previous=encode(rows(projection));
  const envelopes=added.map((record,i)=>({index:(projection.length+i)*2,record}));
  const bytes=body??(added.length?encode(envelopes):Buffer.alloc(0));
  p.manifest(p.tools('trace_snapshot').at(-1),bytes,{status:'delta',base_revision:'r1',session:p.session('r2'),
    snapshot_id:nextSnapshot,start_offset:previous.length,total_bytes:previous.length+bytes.length,
    source_bytes:10000+bytes.length+1,record_count:projection.length+added.length,total_chunks:bytes.length?1:0,...extra});
  await tick();return {refresh,bytes,offset:previous.length};
}
async function finishNextDetail(p,records){
  const call=p.tools('trace_detail').at(-1),indices=call.params.arguments.record_indices;
  const bytes=encode(indices.map(index=>({index,record:records[index/2]})));
  p.reply(call,{snapshot_id:nextSnapshot,revision:'r2',detail_id:detail,record_indices:indices,
    record_count:indices.length,total_bytes:bytes.length,total_chunks:1,chunk_bytes:384*1024});await tick();
  p.chunk(p.tools('trace_chunk').at(-1),bytes,{snapshot_id:nextSnapshot,revision:'r2'});await tick();
}
function page(){
  const boxes=new Map(),callbacks=new Map(),calls=[],timers=new Map(),warnings=[];let serial=0,intersection;
  const box=key=>{if(!boxes.has(key)) boxes.set(key,{innerHTML:'',textContent:'',scrollTop:0,scrollLeft:0,style:{},dataset:{},classList:{add(){},remove(){},toggle(){}},querySelectorAll:()=>[],querySelector:()=>null});return boxes.get(key);};
  box('#viewer-config').textContent='{"thread_id":null}';
  const parent={postMessage:message=>calls.push(JSON.parse(JSON.stringify(message)))};
  const window={parent,addEventListener:(name,fn)=>{if(!callbacks.has(name))callbacks.set(name,new Set());callbacks.get(name).add(fn);},removeEventListener:(name,fn)=>callbacks.get(name)?.delete(fn)};
  const c=vm.createContext({window,document:{hidden:false,querySelector:box,querySelectorAll:()=>[],body:box('body'),documentElement:{dataset:{},style:{setProperty(){}}},addEventListener(){},removeEventListener(){}},localStorage:{getItem:()=>null},console:{...console,warn:text=>warnings.push(text)},URL,AbortController,TextDecoder,Uint8Array,atob,performance,
    IntersectionObserver:class { constructor(callback){intersection=visible=>callback([{isIntersecting:visible,intersectionRect:{width:visible?100:0,height:visible?100:0}}]);} observe(){intersection(true);} disconnect(){intersection=()=>{};} }, crypto:{randomUUID:()=>`99999999-8888-7777-6666-${String(++serial).padStart(12,'0')}`},setTimeout:(fn,ms)=>{if(ms===0)return setImmediate(fn);const id=++serial;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),setInterval:()=>++serial,clearInterval(){}});
  const metrics={parses:0,renders:0};c.metrics=metrics;
  const run=text=>vm.runInContext(text,c);run(main);
  run('var originalParse=parseRecords,originalRender=renderStream;parseRecords=(...args)=>{metrics.parses++;return originalParse(...args)};renderStream=(...args)=>{metrics.renders++;return originalRender(...args)}');
  run(adapter);
  const emit=data=>{for(const callback of callbacks.get('message')||[])callback({source:parent,data});};
  const tools=name=>calls.filter(x=>x.method==='tools/call'&&x.params.name===name);
  const reply=(call,extra)=>emit({jsonrpc:'2.0',id:call.id,result:{structuredContent:{view_id:call.params.arguments.view_id,request_nonce:call.params.arguments.request_nonce,thread_id:thread,identity_status:'verified',snapshot_match:true,server_instance_id:server,request_id:++serial,...extra}}});
  const session=revision=>({path:'/self.jsonl',name:'self.jsonl',thread_id:thread,revision});
  const manifest=(call,bytes,extra={})=>reply(call,{status:'ready',format:'outline',session:session('r1'),snapshot_id:snapshot,total_bytes:bytes.length,source_bytes:10000,record_count:projection.length,skipped_records:0,start_offset:0,total_chunks:1,chunk_bytes:384*1024,truncated:false,...extra});
  const chunk=(call,bytes,extra={})=>reply(call,{snapshot_id:snapshot,revision:'r1',chunk_index:0,encoding:'base64',data:bytes.toString('base64'),byte_offset:0,byte_length:bytes.length,done:true,...(call.params.arguments.detail_id?{detail_id:call.params.arguments.detail_id}:{}),...extra});
  async function start(){const boot=run('bind();boot()');emit({jsonrpc:'2.0',id:calls[0].id,result:{protocolVersion:'2026-01-26'}});await tick();const bytes=encode(rows(projection));manifest(tools('trace_snapshot').at(-1),bytes);await tick();chunk(tools('trace_chunk').at(-1),bytes);await tick();return boot;}
  async function finishDetail(extra={}){const call=tools('trace_detail').at(-1),indices=call.params.arguments.record_indices,bytes=encode(indices.map(index=>({index,record:source[index/2]})));
    reply(call,{snapshot_id:snapshot,revision:'r1',detail_id:detail,record_indices:indices,record_count:indices.length,total_bytes:bytes.length,total_chunks:1,chunk_bytes:384*1024,...extra});await tick();
    if(!Object.keys(extra).length)chunk(tools('trace_chunk').at(-1),bytes);await tick();}
  return {run,box,c,calls,tools,reply,manifest,chunk,session,emit,start,finishDetail,timers,warnings,metrics,visible:value=>intersection(value)};
}

test('manual pause releases outline and details; resume restores every round and identical statistics',async()=>{
  const p=await loadedPage(),signature=p.run('traceSummarySignature(S.parsed)');
  const old=p.tools('trace_snapshot')[0].params.arguments.view_id;
  const paused=p.box('#trace-pause').onclick();
  assert.equal(p.run('S.parsed'),null);assert.equal(p.run('S.lazyDetails'),null);
  p.reply(p.tools('trace_release').at(-1),{released:true});await paused;
  const resumed=p.box('#trace-pause').onclick();await tick();const call=p.tools('trace_snapshot').at(-1);
  assert.notEqual(call.params.arguments.view_id,old);assert.equal(call.params.arguments.known_revision,undefined);
  const bytes=encode(rows(projection));p.manifest(call,bytes);await tick();p.chunk(p.tools('trace_chunk').at(-1),bytes);
  await tick();await p.finishDetail();await resumed;
  assert.equal(p.run('S.parsed.turns.length'),3);assert.equal(p.run('S.lazyDetails.loadedIndex'),2);
  assert.equal(p.run('traceSummarySignature(S.parsed)'),signature);assert.equal(p.warnings.length,0);
});

test('outline renders every header before downloading only the last round details',async()=>{
  const p=page();let boot;const started=p.start().then(value=>{boot=value;});await tick();await tick();await tick();
  assert.equal(p.tools('trace_snapshot')[0].params.arguments.format,'outline');
  assert.equal(p.run('S.parsed.turns.length'),3);assert.equal(p.run('S.lazyDetails.loadedIndex'),null);
  assert.equal((p.box('#stream').innerHTML.match(/class="turn-h"/g)||[]).length,3);
  assert.doesNotMatch(p.box('#stream').innerHTML,/PRIVATE/);assert.match(p.box('#stream').innerHTML,/正在载入本轮详情/);
  assert.doesNotMatch(p.box('#filter').innerHTML,/全部展开/);assert.deepEqual(p.tools('trace_detail')[0].params.arguments.record_indices,[14,16,18]);
  await p.finishDetail();await started;await boot;assert.equal(p.run('S.lazyDetails.loadedIndex'),2);
  assert.equal(p.run('S.parsed.turns[2].steps[1].raw.payload.replacement_history[0].text'),'PRIVATE THIRD DETAIL');
  assert.equal(p.tools('trace_detail').length,1);
});

test('history selection fetches its late tool result and drops the formerly hydrated round',async()=>{
  const p=page(),boot=p.start();await tick();await tick();await tick();await p.finishDetail();await boot;
  const totals=p.run('JSON.stringify(S.parsed.totals)'), old=p.run('S.parsed');
  const load=p.run('openTurn(0)');await tick();assert.deepEqual(p.tools('trace_detail').at(-1).params.arguments.record_indices,[2,4,6,20]);
  assert.doesNotMatch(p.run('JSON.stringify(S.parsed)'),/PRIVATE/);assert.notEqual(p.run('S.parsed'),old);
  await p.finishDetail();assert.equal(await load,true);assert.equal(p.run('S.lazyDetails.loadedIndex'),0);
  assert.equal(p.run('S.parsed.turns[0].steps[1].result.item.aggregated_output'),'PRIVATE FIRST DETAIL');
  assert.equal(p.run('S.parsed.turns[2].steps[1].raw.payload.replacement_history'),undefined);
  assert.equal(p.run('JSON.stringify(S.parsed.totals)'),totals);assert.equal(p.run('[...S.expanded].join()'),'0');
});

test('polling cannot replace a snapshot while selected detail pages are being read',async()=>{
  const p=page(),boot=p.start();await tick();await tick();await tick();await p.finishDetail();await boot;
  const load=p.run('openTurn(0)');await tick();const poll=p.run('refreshScoped()');await tick();
  assert.equal(p.tools('trace_snapshot').length,1);await p.finishDetail();await load;await poll;
  const refresh=p.run('refreshScoped()');await tick();assert.equal(p.tools('trace_snapshot').length,2);
  const bytes=encode(rows(projection));p.manifest(p.tools('trace_snapshot').at(-1),bytes,{status:'unchanged',start_offset:bytes.length,total_chunks:0});await refresh;
  assert.equal(p.tools('trace_detail').length,2);assert.equal(p.run('S.lazyDetails.loadedIndex'),0);
});

test('rapid history selection cancels the first request and commits only the latest selection',async()=>{
  const p=page(),boot=p.start();await tick();await tick();await tick();await p.finishDetail();await boot;
  const first=p.run('openTurn(0)');await tick();const stale=p.tools('trace_detail').at(-1);
  const second=p.run('openTurn(1)');await tick();assert.equal(await first,false);assert.notEqual(p.tools('trace_detail').at(-1).id,stale.id);
  p.reply(stale,{snapshot_id:snapshot,revision:'r1',detail_id:detail,record_indices:[2,4,6,20]});
  await p.finishDetail();assert.equal(await second,true);assert.equal(p.run('S.lazyDetails.loadedIndex'),1);
  assert.doesNotMatch(p.run('JSON.stringify(S.parsed)'),/PRIVATE FIRST DETAIL/);
});

test('mismatched detail indices, revision or nonce never expose projected raw records as complete',async()=>{
  for(const change of [{record_indices:[2]},{revision:'wrong'},{request_nonce:'wrong'},{record_count:1}]){
    const p=page(),boot=p.start();await tick();await tick();await tick();await p.finishDetail();await boot;
    const load=p.run('openTurn(0)');await tick();await p.finishDetail(change);
    if(change.record_count){const call=p.tools('trace_chunk').at(-1);p.chunk(call,encode([2,4,6,20].map(index=>({index,record:source[index/2]}))));}
    assert.equal(await load,false);assert.equal(p.run('S.lazyDetails.loadedIndex'),null);
    p.run('S.tab="raw";renderInsp()');assert.match(p.box('#insp').innerHTML,/尚未载入/);assert.doesNotMatch(p.box('#stream').innerHTML,/PRIVATE/);
  }
});

test('malformed or incomplete outline wire retains the last valid snapshot and never drops records silently',async()=>{
  for(const bytes of [Buffer.from('{broken}\n'),encode(rows(projection).slice(0,-1))]){
    const p=page(),boot=p.start();await tick();await tick();await tick();await p.finishDetail();await boot;
    const previous=p.run('S.parsed'),refresh=p.run('refreshScoped()');await tick();
    p.manifest(p.tools('trace_snapshot').at(-1),bytes,{session:p.session('r2')});await tick();
    p.chunk(p.tools('trace_chunk').at(-1),bytes,{revision:'r2'});await refresh;
    assert.equal(p.run('S.parsed'),previous);assert.match(p.box('#live-status').textContent,/更新失败/);
  }
});

test('refresh invalidates old detail and reloads the selected historical step against the new revision',async()=>{
  const p=page(),boot=p.start();await tick();await tick();await tick();await p.finishDetail();await boot;
  const load=p.run('openTurn(0)');await tick();await p.finishDetail();await load;p.run('selectStep(0,1);S.tab="t_result"');
  const refresh=p.run('refreshScoped()');await tick();const bytes=encode(rows(projection));
  p.manifest(p.tools('trace_snapshot').at(-1),bytes,{session:p.session('r2')});await tick();
  p.chunk(p.tools('trace_chunk').at(-1),bytes,{revision:'r2'});await tick();assert.equal(p.run('S.lazyDetails.loadedIndex'),null);
  const call=p.tools('trace_detail').at(-1),indices=call.params.arguments.record_indices,full=encode(indices.map(index=>({index,record:source[index/2]})));
  p.reply(call,{snapshot_id:snapshot,revision:'r2',detail_id:detail,record_indices:indices,record_count:indices.length,total_bytes:full.length,total_chunks:1,chunk_bytes:384*1024});await tick();
  p.chunk(p.tools('trace_chunk').at(-1),full,{revision:'r2'});await refresh;
  assert.equal(p.run('S.lazyDetails.loadedIndex'),0);assert.equal(p.run('JSON.stringify(S.sel)'),'{"ti":0,"idx":1}');assert.equal(p.run('S.tab'),'t_result');
});

test('closing during detail loading clears outline and hydrated state and rejects late replies',async()=>{
  const p=page(),boot=p.start();await tick();await tick();await tick();await p.finishDetail();await boot;
  const load=p.run('openTurn(0)');await tick();const stale=p.tools('trace_detail').at(-1);
  p.emit({jsonrpc:'2.0',method:'ui/resource-teardown',id:'close'});assert.equal(p.run('S.parsed'),null);assert.equal(p.run('S.lazyDetails'),null);
  p.reply(stale,{snapshot_id:snapshot,revision:'r1',detail_id:detail,record_indices:[2,4,6,20]});
  const release=p.tools('trace_release').at(-1);p.reply(release,{released:true});await load;await tick();
  assert.equal(p.run('S.parsed'),null);assert.equal(p.timers.size,0);assert.equal(p.box('#stream').innerHTML,'');
});

test('one round with more than 8192 source records is hydrated in serial bounded batches',async()=>{
  const p=page(),items=[source[0],context('large'),message('large','many events'),
    ...Array.from({length:8191},()=>rec('inter_agent_communication_metadata',{}))], envelopes=rows(items), bytes=encode(envelopes), size=384*1024;
  async function pages(body){for(let i=0;i<Math.ceil(body.length/size);i++){
    await tick();p.chunk(p.tools('trace_chunk').at(-1),body.subarray(i*size,(i+1)*size),{chunk_index:i,byte_offset:i*size,done:(i+1)*size>=body.length});
  }await tick();}
  const boot=p.run('bind();boot()');p.emit({jsonrpc:'2.0',id:p.calls[0].id,result:{protocolVersion:'2026-01-26'}});await tick();
  p.manifest(p.tools('trace_snapshot').at(-1),bytes,{record_count:items.length,total_chunks:Math.ceil(bytes.length/size)});await pages(bytes);
  for(const expectedLength of [8192,1]){
    const call=p.tools('trace_detail').at(-1),indices=call.params.arguments.record_indices;
    assert.equal(indices.length,expectedLength);const full=encode(indices.map(index=>envelopes[index/2]));
    p.reply(call,{snapshot_id:snapshot,revision:'r1',detail_id:detail,record_indices:indices,record_count:indices.length,total_bytes:full.length,total_chunks:Math.ceil(full.length/size),chunk_bytes:size});
    await pages(full);
  }
  await boot;assert.equal(p.run('S.lazyDetails.loadedIndex'),0);assert.equal(p.run('S.parsed.turns[0].steps.length'),8192);
  assert.equal(p.tools('trace_detail').length,2);
});

test('collapse-all during detail loading stays collapsed after that request completes',async()=>{
  const p=page(),boot=p.start();await tick();await tick();await tick();p.run('S.expanded.clear();renderStream()');
  await p.finishDetail();await boot;assert.equal(p.run('S.expanded.size'),0);
});

test('retry refresh retries missing detail even when its complete outline has not changed',async()=>{
  const p=page(),boot=p.start();await tick();await tick();await tick();await p.finishDetail({revision:'bad'});await boot;
  assert.equal(p.run('S.lazyDetails.loadedIndex'),null);
  const retry=p.run('refreshScoped()');await tick();const bytes=encode(rows(projection));
  p.manifest(p.tools('trace_snapshot').at(-1),bytes,{status:'unchanged',start_offset:bytes.length,total_chunks:0});await tick();
  assert.equal(p.tools('trace_detail').length,2);await p.finishDetail();await retry;assert.equal(p.run('S.lazyDetails.loadedIndex'),2);
});

test('appended outlines reuse immutable old records and recompute late results, timing and usage exactly',()=>{
  const p=page();p.c.original=rows(projection);p.c.extra=rows([
    rec('token_usage_record',{turn_id:'t1',response_id:'r1',usage:{input_tokens:100,cached_input_tokens:50,output_tokens:20,total_tokens:120}}),
    rec('token_usage_record',{turn_id:'t1',response_id:'r1',usage:{input_tokens:100,cached_input_tokens:50,output_tokens:20,total_tokens:120}}),
    {...rec('event_msg',{type:'item_completed',turn_id:'t1',item:{type:'CommandExecution',id:'late',duration:{secs:3},exit_code:1}}),timestamp:'2026-10-13T01:00:09Z'},
    context('t4'),message('t4','fourth')]).map((row,i)=>({...row,index:100+i}));
  p.run('var first=createTraceOutline(original);var before=traceSummarySignature(first.parsed);var next=appendTraceOutline(first,extra);');
  assert.equal(p.run('next.records[0]===first.records[0]'),true);assert.equal(p.run('first.records.length'),projection.length);
  assert.equal(p.run('first.sources.has(extra[0].record)'),false);assert.equal(p.run('traceSummarySignature(first.parsed)'),p.run('before'));
  assert.equal(p.run('traceSummarySignature(next.parsed)'),p.run('traceSummarySignature(parseRecords(original.concat(extra).map(r=>r.record)))'));
  assert.equal(p.run('next.parsed.turns.length'),4);assert.equal(p.run('next.parsed.totals.tokens'),120);
  assert.equal(p.run('next.parsed.turns[0].toolMs'),3000);assert.equal(p.run('appendTraceOutline(next,[])===next'),true);
});

test('same-round delta transfers only appended summary and missing selected detail records',async()=>{
  const p=await loadedPage(),added=[rec('compacted',{message:'new summary'}),
    rec('token_usage_record',{turn_id:'t3',response_id:'latest',usage:{input_tokens:100,output_tokens:20,total_tokens:120}})];
  const before=p.tools('trace_chunk').length,delta=await beginDelta(p,added);
  assert.equal(p.tools('trace_chunk').length,before+1);
  p.chunk(p.tools('trace_chunk').at(-1),delta.bytes,{snapshot_id:nextSnapshot,revision:'r2',byte_offset:delta.offset});await tick();
  assert.deepEqual(p.tools('trace_detail').at(-1).params.arguments.record_indices,[22,24]);
  await finishNextDetail(p,source.concat(added));await delta.refresh;
  assert.equal(p.run('S.current.revision'),'r2');assert.equal(p.run('S.lazyDetails.loadedIndex'),2);
  assert.equal(p.run('S.parsed.totals.tokens'),120);assert.equal(p.run('S.parsed.turns[2].steps[1].raw.payload.replacement_history[0].text'),'PRIVATE THIRD DETAIL');
});

test('a new-round delta retains historical selection and reuses its complete detail without another RPC',async()=>{
  const p=await loadedPage(),load=p.run('openTurn(0)');await tick();await p.finishDetail();await load;
  p.run('selectStep(0,1);S.tab="t_result"');const count=p.tools('trace_detail').length;
  const delta=await beginDelta(p,[context('t4'),message('t4','fourth')]);
  p.chunk(p.tools('trace_chunk').at(-1),delta.bytes,{snapshot_id:nextSnapshot,revision:'r2',byte_offset:delta.offset});await delta.refresh;
  assert.equal(p.run('S.parsed.turns.length'),4);assert.equal(p.tools('trace_detail').length,count);
  assert.equal(p.run('S.lazyDetails.loadedIndex'),0);assert.equal(p.run('JSON.stringify(S.sel)'),'{"ti":0,"idx":1}');
  assert.equal(p.run('S.tab'),'t_result');assert.equal(p.run('[...S.expanded].join()'),'0');
});

test('unchanged historical detail reuses parsed steps with one outline parse and one list paint',async()=>{
  const p=await loadedPage(),load=p.run('openTurn(0)');await tick();await p.finishDetail();await load;
  const steps=p.run('S.parsed.turns[0].steps');p.run('selectStep(0,1);S.tab="t_result";S.truncMap.set("0:1",true)');
  p.metrics.parses=0;p.metrics.renders=0;
  const added=[context('t4'),message('t4','fourth'),
    rec('token_usage_record',{turn_id:'t4',response_id:'r4',usage:{input_tokens:50,output_tokens:10,total_tokens:60}}),
    rec('event_msg',{type:'thread_settings_applied',thread_settings:{model:'new-global-model'}}),
    rec('world_state',{state:{model:'new-world'}})];
  const delta=await beginDelta(p,added);
  p.chunk(p.tools('trace_chunk').at(-1),delta.bytes,{snapshot_id:nextSnapshot,revision:'r2',byte_offset:delta.offset});await delta.refresh;
  assert.equal(p.metrics.parses,1);assert.equal(p.metrics.renders,1);
  assert.equal(p.run('S.parsed.turns[0].steps'),steps);
  assert.equal(p.run('S.parsed.totals.tokens'),60);assert.equal(p.run('S.parsed.turns.length'),4);
  assert.equal(p.run('S.parsed.settings.model'),'new-global-model');assert.equal(p.run('S.parsed.world.state.model'),'new-world');
  assert.equal(p.run('S.parsed.turns[0].steps[1].result.item.aggregated_output'),'PRIVATE FIRST DETAIL');
  assert.equal(p.run('S.tab'),'t_result');assert.equal(p.run('S.truncMap.get("0:1")'),true);
});

test('initial and changed selected details paint only the summary loading state and completed detail',async()=>{
  const p=await loadedPage();assert.equal(p.metrics.parses,2);assert.equal(p.metrics.renders,2);
  p.metrics.parses=0;p.metrics.renders=0;
  const added=[rec('compacted',{message:'updated summary'})],delta=await beginDelta(p,added);
  p.chunk(p.tools('trace_chunk').at(-1),delta.bytes,{snapshot_id:nextSnapshot,revision:'r2',byte_offset:delta.offset});await tick();
  assert.equal(p.metrics.renders,1);assert.match(p.box('#stream').innerHTML,/正在载入本轮详情/);
  await finishNextDetail(p,source.concat(added));await delta.refresh;
  assert.equal(p.metrics.parses,2);assert.equal(p.metrics.renders,2);
  assert.equal(p.run('S.parsed.turns[2].steps.at(-1).text'),'updated summary');
});

test('empty outline delta advances its snapshot without parsing, detail transfer or losing a collapsed selection',async()=>{
  const p=await loadedPage(),previous=p.run('S.parsed'),chunks=p.tools('trace_chunk').length,details=p.tools('trace_detail').length;
  p.run('S.expanded.clear();renderStream()');const delta=await beginDelta(p,[],{skipped_records:1});await delta.refresh;
  assert.equal(p.run('S.current.revision'),'r2');assert.equal(p.run('S.parsed'),previous);
  assert.equal(p.tools('trace_chunk').length,chunks);assert.equal(p.tools('trace_detail').length,details);
  assert.equal(p.run('S.expanded.size'),0);assert.equal(p.run('S.lazyDetails.loadedIndex'),2);
});

test('a late result delta updates its original selected round and transfers only that new source record',async()=>{
  const p=await loadedPage(),load=p.run('openTurn(0)');await tick();await p.finishDetail();await load;
  const raw={...rec('event_msg',{type:'item_completed',turn_id:'t1',item:{type:'CommandExecution',id:'late',
    duration:{secs:4},exit_code:1,aggregated_output:'NEW PRIVATE RESULT'}}),timestamp:'2026-10-13T01:00:12Z'};
  const light=structuredClone(raw);delete light.payload.item.aggregated_output;
  const delta=await beginDelta(p,[light]);p.chunk(p.tools('trace_chunk').at(-1),delta.bytes,
    {snapshot_id:nextSnapshot,revision:'r2',byte_offset:delta.offset});await tick();
  assert.deepEqual(p.tools('trace_detail').at(-1).params.arguments.record_indices,[22]);
  await finishNextDetail(p,source.concat([raw]));await delta.refresh;
  assert.equal(p.run('S.parsed.turns[0].toolMs'),4000);assert.equal(p.run('S.parsed.turns[0].spanMs'),12000);
  assert.equal(p.run('S.parsed.turns[0].steps[1].result.item.aggregated_output'),'NEW PRIVATE RESULT');
  assert.equal(p.run('S.parsed.turns[1].toolMs'),0);assert.equal(p.run('S.parsed.turns.length'),3);
});

test('invalid delta baselines retain the committed snapshot and make the next request a full reset',async()=>{
  for(const extra of [{base_revision:'wrong'},{start_offset:1},{source_bytes:10000},{record_count:0},
    {server_instance_id:nextSnapshot},{snapshot_id:snapshot},{session:{path:'/other.jsonl',name:'other',thread_id:thread,revision:'r2'}},
    {session:{path:'/self.jsonl',name:'self',thread_id:thread,revision:'r1'}},{skipped_records:-1},{total_bytes:1}]){
    const p=await loadedPage(),previous=p.run('S.parsed'),count=p.tools('trace_chunk').length;
    const delta=await beginDelta(p,[message('t3','added')],extra);await delta.refresh;
    assert.equal(p.run('S.parsed'),previous);assert.equal(p.run('S.current.revision'),'r1');assert.equal(p.tools('trace_chunk').length,count);
    const retry=p.run('refreshScoped()');await tick();const call=p.tools('trace_snapshot').at(-1);
    assert.equal(call.params.arguments.known_revision,undefined);p.reply(call,{status:'waiting',truncated:false});await retry;
  }
});

test('corrupt delta count, physical index, UTF-8 and chunk offsets are atomic and never reuse a poisoned baseline',async()=>{
  const item=message('t3','added 中文🙂'),good=encode([{index:22,record:item}]);
  for(const issue of [{bytes:encode([{index:20,record:item}])},{bytes:encode([{index:22,record:item},{index:22,record:item}]),record_count:projection.length+2},
    {bytes:Buffer.from([0xff,0x0a])},{bytes:good.subarray(0,-1)},{bytes:good,record_count:projection.length+2},
    {bytes:good,chunk:{byte_offset:0}},{bytes:good,chunk:{request_nonce:'wrong'}},{bytes:good,chunk:{revision:'wrong'}}]){
    const p=await loadedPage(),previous=p.run('S.parsed'),count=p.tools('trace_detail').length;
    const delta=await beginDelta(p,[item],issue.record_count?{record_count:issue.record_count}:{},issue.bytes);
    p.chunk(p.tools('trace_chunk').at(-1),delta.bytes,{snapshot_id:nextSnapshot,revision:'r2',byte_offset:delta.offset,...issue.chunk});await delta.refresh;
    assert.equal(p.run('S.parsed'),previous);assert.equal(p.run('S.current.revision'),'r1');assert.equal(p.tools('trace_detail').length,count);
    const retry=p.run('refreshScoped()');await tick();const call=p.tools('trace_snapshot').at(-1);
    assert.equal(call.params.arguments.known_revision,undefined);p.reply(call,{status:'waiting',truncated:false});await retry;
  }
});

test('a delta whose last chunk is corrupt preserves all old record identities and usage totals',async()=>{
  const p=await loadedPage(),previous=p.run('S.parsed'),added=[message('t3','valid 中文🙂'),rec('compacted',{message:'second'})];
  const body=encode(added.map((record,i)=>({index:22+i*2,record}))),size=body.length-2;
  const delta=await beginDelta(p,added,{chunk_bytes:size,total_chunks:2},body);
  p.chunk(p.tools('trace_chunk').at(-1),body.subarray(0,size),{snapshot_id:nextSnapshot,revision:'r2',byte_offset:delta.offset,done:false});await tick();
  assert.equal(p.run('S.parsed'),previous);
  p.chunk(p.tools('trace_chunk').at(-1),body.subarray(size),{snapshot_id:nextSnapshot,revision:'r2',byte_offset:delta.offset+size,chunk_index:1,request_nonce:'bad'});
  await delta.refresh;assert.equal(p.run('S.parsed'),previous);assert.equal(p.run('S.current.revision'),'r1');
});

test('replayed delta cannot double-count usage or request its cached detail a second time',async()=>{
  const p=await loadedPage(),added=[rec('token_usage_record',{turn_id:'t3',response_id:'r',usage:{input_tokens:100,output_tokens:20,total_tokens:120}})];
  const delta=await beginDelta(p,added);p.chunk(p.tools('trace_chunk').at(-1),delta.bytes,{snapshot_id:nextSnapshot,revision:'r2',byte_offset:delta.offset});await tick();
  await finishNextDetail(p,source.concat(added));await delta.refresh;const previous=p.run('S.parsed'),count=p.tools('trace_detail').length;
  const replay=await beginDelta(p,added);await replay.refresh;
  assert.equal(p.run('S.parsed'),previous);assert.equal(p.run('S.parsed.totals.tokens'),120);assert.equal(p.tools('trace_detail').length,count);
});

test('a user record before turn_context requests only actual source indices and remains hydratable',()=>{
  const p=page();p.c.rows=rows([source[0],message('pending','context arrives later')]);
  p.run('var outline=createTraceOutline(rows)');
  assert.equal(p.run('JSON.stringify(traceRoundRecordIndices(outline,0))'),'[2]');
  assert.equal(p.run('hydrateTraceRound(outline,0,[rows[1]]).turns[0].steps[0].text'),'context arrives later');
  for(const missing of [null,undefined,false,0,'']){
    p.c.missing=missing;p.run('outline.parsed.turns[0].ctx=missing;outline.parsed.turns[0].steps[0].context=missing;');
    assert.equal(p.run('JSON.stringify(traceRoundRecordIndices(outline,0))'),'[2]');
  }
});


test('hidden outline page cancels a pending round detail and fully restores identical statistics on a new view',async()=>{
  const p=page(),boot=p.start();await tick();await tick();await tick();
  const oldDetail=p.tools('trace_detail').at(-1),before=p.run('traceSummarySignature(S.parsed)');
  const oldView=oldDetail.params.arguments.view_id,oldChunks=p.tools('trace_chunk').length;
  p.visible(false);assert.equal(p.run('S.parsed'),null);assert.equal(p.run('S.lazyDetails'),null);assert.equal(p.box('#stream').innerHTML,'');
  const release=p.tools('trace_release').at(-1);p.visible(true);await tick();
  const fresh=p.tools('trace_snapshot').at(-1);assert.notEqual(fresh.params.arguments.view_id,oldView);
  p.reply(oldDetail,{snapshot_id:snapshot,revision:'r1',detail_id:detail,record_indices:oldDetail.params.arguments.record_indices,
    record_count:oldDetail.params.arguments.record_indices.length,total_bytes:1,total_chunks:1,chunk_bytes:384*1024});
  await boot;await tick();assert.equal(p.tools('trace_chunk').length,oldChunks);assert.equal(p.run('S.parsed'),null);
  const bytes=encode(rows(projection));p.manifest(fresh,bytes);await tick();p.chunk(p.tools('trace_chunk').at(-1),bytes);await tick();await p.finishDetail();
  p.reply(release,{released:true});await tick();assert.equal(p.run('traceSummarySignature(S.parsed)'),before);
  assert.equal(p.run('S.parsed.turns.length'),3);assert.equal(p.run('S.lazyDetails.loadedIndex'),2);assert.equal(p.warnings.length,0);
});
