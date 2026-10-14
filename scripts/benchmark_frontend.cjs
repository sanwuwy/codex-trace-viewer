/* Local numeric audit: node --expose-gc scripts/benchmark_frontend.cjs /rollout.jsonl /unpacked-0.7.0-plugin
 * Runs the actual main script and complete bridge in isolated VMs. The DOM records HTML writes;
 * no browser layout, GPU, or native desktop process memory is included. Raw records stay local. */
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),vm=require('node:vm');
const readline=require('node:readline'),crypto=require('node:crypto');
const {spawn,spawnSync}=require('node:child_process'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),tick=()=>new Promise(resolve=>setImmediate(resolve));
const round=x=>Math.round(x*100)/100,mib=x=>round(x/1048576);
function digest(value){return crypto.createHash('sha256').update(JSON.stringify(value,(_,v)=>
  v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v)).digest('hex');}
const python=String.raw`
import importlib.util,json,os,pathlib,sys
spec=importlib.util.spec_from_file_location('frontend_benchmark',os.environ['BENCHMARK_PLUGIN'])
plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
source=next((pathlib.Path(os.environ['CODEX_HOME'])/'sessions').glob('*.jsonl'))
with source.open('rb') as f:thread=json.loads(f.readline())['payload']['id']
store=plugin.TraceStore()
try:
    for line in sys.stdin:
        q=json.loads(line);a=q.get('arguments',{});name=q['method'];view=a.get('view_id')
        if name=='trace_snapshot':value=store.snapshot(thread,view,a.get('known_revision'),a['format'])
        elif name=='trace_chunk':value=store.chunk(thread,view,a['snapshot_id'],a['chunk_index'],a.get('detail_id'))
        elif name=='trace_detail':value=store.detail(thread,view,a['snapshot_id'],a['record_indices'])
        elif name=='trace_release':value={'released':store.release(thread,view)}
        elif name=='append':
            import base64
            with source.open('ab') as f:f.write(base64.b64decode(q['data']))
            value=True
        elif name=='state':value={'active_views':len(store.views)}
        else:raise ValueError('Unknown benchmark operation')
        print(json.dumps(value),flush=True)
finally:store.close()
`;
function scripts(baseline,mode){return {
  main:fs.readFileSync(mode==='old'?path.join(baseline,'assets/index.html'):path.join(root,'index.html'),'utf8')
    .split('<script>')[1].split('</script>')[0].replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/,''),
  bridge:fs.readFileSync(mode==='old'?path.join(baseline,'viewer-bridge.js'):path.join(root,'plugins/codex-trace-probe/viewer-bridge.js'),'utf8')};}
function makePage(code,call,thread){
  const boxes=new Map(),callbacks=new Map(),timers=new Set(),errors=[];
  let serial=0,queue=Promise.resolve(),closed=false,tracking=false;
  const counters={parses:0,renders:0,html_writes:0,html_bytes:0,outline_bytes:0,detail_bytes:0,rpc:0,detail_rpc:0,peak_heap:0,peak_rss:0};
  function sample(){if(tracking){const m=process.memoryUsage();counters.peak_heap=Math.max(counters.peak_heap,m.heapUsed);counters.peak_rss=Math.max(counters.peak_rss,m.rss);}}
  const owners={'#expall':'#filter','#colall':'#filter','#btn-sys':'#insp'};
  const box=key=>{if(owners[key]&&!boxes.get(owners[key])?.innerHTML.includes(`id="${key.slice(1)}"`))return null;
    if(!boxes.has(key)){
    let html='';boxes.set(key,{get innerHTML(){return html;},set innerHTML(v){html=v;
      for(const [child,owner] of Object.entries(owners))if(owner===key)boxes.delete(child);
      if(tracking&&key==='#stream'){
      counters.html_writes++;counters.html_bytes+=Buffer.byteLength(v);sample();}},textContent:'',scrollTop:0,scrollLeft:0,style:{},dataset:{},
      classList:{add(){},remove(){},toggle(){}},querySelectorAll:()=>[],querySelector:()=>null});}return boxes.get(key);};
  box('#viewer-config').textContent='{"thread_id":null}';
  let emit;
  const parent={postMessage(message){
    if(message.id==='benchmark-close'&&message.result){closed=true;return;}
    if(!message.method||!message.id)return;
    queue=queue.then(async()=>{
      if(message.method==='ui/initialize'){emit({jsonrpc:'2.0',id:message.id,result:{protocolVersion:'2026-01-26'}});return;}
      assert.equal(message.method,'tools/call');
      const {name,arguments:args}=JSON.parse(JSON.stringify(message.params));
      const data=await call({method:name,arguments:args});
      if(tracking){counters.rpc++;if(name==='trace_detail')counters.detail_rpc++;
        if(name==='trace_chunk')counters[args.detail_id?'detail_bytes':'outline_bytes']+=data.byte_length;}
      emit({jsonrpc:'2.0',id:message.id,result:{structuredContent:{thread_id:thread,identity_status:'verified',snapshot_match:true,
        ...data,view_id:args.view_id,request_nonce:args.request_nonce,server_instance_id:'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff',request_id:++serial}}});
    }).catch(error=>{errors.push(error);emit({jsonrpc:'2.0',id:message.id,error:{message:error.message}});});
  }};
  const window={parent,addEventListener:(name,fn)=>{if(!callbacks.has(name))callbacks.set(name,new Set());callbacks.get(name).add(fn);},
    removeEventListener:(name,fn)=>callbacks.get(name)?.delete(fn)};
  const context=vm.createContext({window,document:{hidden:false,querySelector:box,querySelectorAll:()=>[],body:box('body'),
    documentElement:{dataset:{},style:{setProperty(){}}},addEventListener(){},removeEventListener(){}},localStorage:{getItem:()=>null},
    console:{...console,warn:message=>errors.push(new Error(message))},URL,AbortController,TextDecoder,Uint8Array,atob,performance,
    IntersectionObserver:class { constructor(callback){this.callback=callback;} observe(){this.callback([{isIntersecting:true,intersectionRect:{width:100,height:100}}]);} disconnect(){} }, crypto:{randomUUID:crypto.randomUUID},setTimeout:(fn,ms)=>{const timer=setTimeout(()=>{timers.delete(timer);fn();},ms);timers.add(timer);return timer;},
    clearTimeout:timer=>{timers.delete(timer);clearTimeout(timer);},setInterval:()=>1,clearInterval(){},
    __parse:()=>{if(tracking)counters.parses++;sample();},__render:()=>{if(tracking)counters.renders++;sample();},__sample:sample});
  const run=text=>vm.runInContext(text,context);run(code.main);
  run(`var originalParse=parseRecords,originalRender=renderStream;
    parseRecords=function(...args){__parse();const value=originalParse.apply(this,args);__sample();return value;};
    renderStream=function(...args){__render();const value=originalRender.apply(this,args);__sample();return value;};
    function ownRound(t){const own={...t};delete own.sourceTurn;return own;}`);
  run(code.bridge);emit=data=>{for(const callback of callbacks.get('message')||[])callback({source:parent,data});};
  return {run,box,context,errors,
    mark(){for(const key of Object.keys(counters))counters[key]=0;tracking=true;sample();},
    stop(){sample();tracking=false;assert.equal(errors.length,0,errors.map(e=>e.message).join('\n'));return {...counters};},
    async close(){emit({jsonrpc:'2.0',id:'benchmark-close',method:'ui/resource-teardown'});
      for(let i=0;!closed&&i<200;i++)await new Promise(resolve=>setTimeout(resolve,10));
      assert.equal(closed,true,'bridge did not acknowledge teardown');assert.equal(timers.size,0);assert.equal(errors.length,0);
      assert.equal(run('S.parsed'),null);assert.equal(run('S.lazyDetails'),null);assert.equal(box('#stream').innerHTML,'');},
    async settle(){await queue;await tick();assert.equal(errors.length,0,errors.map(e=>e.message).join('\n'));}};
}
function memory(){const m=process.memoryUsage();return {heap_mib:mib(m.heapUsed),rss_mib:mib(m.rss)};}
function hashes(page){return {summary:digest(page.run('traceSummarySignature(S.parsed)')),
  raw:digest(page.run('ownRound(S.parsed.turns[S.selTurn])'))};}
async function worker(home,mode,baseline,scenario){
  const plan=JSON.parse(fs.readFileSync(path.join(home,'updates.json'),'utf8'));
  const backend=mode==='old'?path.join(baseline,'server.py'):path.join(root,'plugins/codex-trace-probe/server.py');
  const child=spawn(process.env.PYTHON||'python3',['-B','-u','-c',python],{cwd:root,
    env:{...process.env,CODEX_HOME:home,BENCHMARK_PLUGIN:backend},stdio:['pipe','pipe','inherit']});
  const exited=new Promise(resolve=>child.on('exit',resolve));
  const lines=readline.createInterface({input:child.stdout})[Symbol.asyncIterator]();
  async function call(q){child.stdin.write(JSON.stringify(q)+'\n');const line=await lines.next();
    assert.equal(line.done,false,'backend exited before replying');return JSON.parse(line.value);}
  const page=makePage(scripts(baseline,mode),call,plan.thread);
  try {
    global.gc();const empty=memory();await page.run('bind();boot()');await page.settle();
    const count=page.run('S.parsed.turns.length'),selected=scenario==='history'?Math.min(30,count-2):count-1;
    assert.ok(selected>=0);if(scenario==='history')await page.run(`openTurn(${selected})`);
    assert.equal(page.run('S.lazyDetails.loadedIndex'),selected);global.gc();const loaded=memory(),initial=hashes(page),updates=[];
    for(const update of plan.updates){
      await call({method:'append',data:update.data});global.gc();page.mark();
      const cpu=process.cpuUsage(),start=performance.now();await page.run('refreshScoped()');
      const elapsed=performance.now()-start,used=process.cpuUsage(cpu),c=page.stop();
      assert.equal(page.run('S.lazyDetails.loadedIndex'),selected);
      assert.doesNotMatch(page.box('#live-status').textContent,/失败|重试/);
      if(mode==='old'){assert.equal(c.parses,2,'0.7 baseline must execute both production parses');
        assert.equal(c.renders,3,'0.7 baseline must execute all three production list builds');}
      global.gc();updates.push({case:update.name,append_bytes:Buffer.from(update.data,'base64').length,
        frontend_cpu_ms:round((used.user+used.system)/1000),elapsed_ms:round(elapsed),
        parse_count:c.parses,render_count:c.renders,html_write_count:c.html_writes,html_constructed_bytes:c.html_bytes,
        outline_transfer_bytes:c.outline_bytes,detail_transfer_bytes:c.detail_bytes,rpc_count:c.rpc,detail_rpc_count:c.detail_rpc,
        observed_peak_heap_mib:mib(c.peak_heap),observed_peak_rss_mib:mib(c.peak_rss),retained:memory(),...hashes(page)});
    }
    await page.close();assert.equal((await call({method:'state'})).active_views,0);await page.settle();
    global.gc();await tick();global.gc();const released=memory();
    return {mode,scenario,rounds:count,selected_index:selected,empty,loaded,released,release_verified:true,initial,updates};
  }finally{child.stdin.end();assert.equal(await exited,0,'backend failed to close');}
}
function oracle(home,baseline,scenario){
  const code=scripts(baseline,'old'),boxes=new Map();
  const box=key=>{if(!boxes.has(key))boxes.set(key,{innerHTML:'',textContent:'',querySelectorAll:()=>[],querySelector:()=>null});return boxes.get(key);};
  const c=vm.createContext({console,localStorage:{getItem:()=>null},document:{querySelector:box,querySelectorAll:()=>[]}});
  const run=text=>vm.runInContext(text,c);run(code.main);run(code.bridge.split('/* Injected')[0]);
  const plan=JSON.parse(fs.readFileSync(path.join(home,'updates.json'),'utf8'));
  const file=path.join(home,'sessions',fs.readdirSync(path.join(home,'sessions'))[0]);
  c.source=fs.readFileSync(file,'utf8');run('var raw=createRolloutStream();raw.feed(source);raw.finish();source=null;var P=raw.build();');
  const selected=scenario==='history'?Math.min(30,run('P.turns.length')-2):run('P.turns.length')-1;
  function snapshot(){return {summary:digest(run('traceSummarySignature(P)')),raw:digest(run(`(()=>{const t={...P.turns[${selected}]};delete t.sourceTurn;return t;})()`))};}
  const initial=snapshot(),updates=[];
  for(const update of plan.updates){c.added=Buffer.from(update.data,'base64').toString();run('raw.feed(added);added=null;P=raw.build();');updates.push(snapshot());}
  return {initial,updates};
}
function numeric(results){
  for(const result of results){delete result.initial;for(const update of result.updates){delete update.summary;delete update.raw;}}
  const median=values=>{const a=values.slice().sort((a,b)=>a-b),i=Math.floor(a.length/2);return round(a.length%2?a[i]:(a[i-1]+a[i])/2);};
  const aggregates=[];
  for(const scenario of ['history','current'])for(const mode of ['old','new']){
    const runs=results.filter(x=>x.mode===mode&&x.scenario===scenario),updates=runs.flatMap(x=>x.updates);
    aggregates.push({scenario,mode,runs:runs.length,updates:updates.length,
      median_cpu_ms:median(updates.map(x=>x.frontend_cpu_ms)),median_elapsed_ms:median(updates.map(x=>x.elapsed_ms)),
      median_retained_heap_mib:median(updates.map(x=>x.retained.heap_mib)),median_rss_mib:median(updates.map(x=>x.retained.rss_mib)),
      maximum_observed_peak_heap_mib:Math.max(...updates.map(x=>x.observed_peak_heap_mib)),
      median_released_heap_mib:median(runs.map(x=>x.released.heap_mib)),
      parse_counts:[...new Set(updates.map(x=>x.parse_count))],render_counts:[...new Set(updates.map(x=>x.render_count))]});
  }
  return aggregates;
}
async function main(){
  assert.equal(typeof global.gc,'function','Run Node with --expose-gc');
  if(process.argv[2]==='--worker'){
    console.log(JSON.stringify(await worker(process.argv[3],process.argv[4],process.argv[5],process.argv[6])));return;}
  if(process.argv[2]==='--oracle'){
    console.log(JSON.stringify(oracle(process.argv[3],process.argv[4],process.argv[5])));return;}
  assert.ok(process.argv[2]&&process.argv[3],'Provide a rollout and an unpacked 0.7.0 plugin directory');
  const baseline=path.resolve(process.argv[3]),repetitions=Number(process.argv[4]||3);
  assert.ok(Number.isSafeInteger(repetitions)&&repetitions>=1&&repetitions<=10,'Repetitions must be 1–10');
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'trace-frontend-benchmark-'));
  try {
    let frozen=fs.readFileSync(path.resolve(process.argv[2])),complete=frozen.subarray(0,frozen.lastIndexOf(10)+1);
    const bytes=complete.length,discarded=frozen.length-bytes,thread=JSON.parse(complete.subarray(0,complete.indexOf(10)).toString()).payload.id;
    assert.match(thread,/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i);
    let baseTime=Date.now(),timestampTailUnusableLines=0;
    for(const line of complete.subarray(Math.max(0,bytes-1024*1024)).toString().trim().split('\n').reverse()){
      try{const parsed=Date.parse(JSON.parse(line)?.timestamp);
        if(Number.isFinite(parsed)){baseTime=parsed;break;}timestampTailUnusableLines++;
      }catch(error){if(!(error instanceof SyntaxError))throw error;timestampTailUnusableLines++;}}
    const turn='frontend-benchmark-turn',record=(type,payload,ms)=>({type,payload,timestamp:new Date(baseTime+ms).toISOString()});
    const line=rows=>Buffer.from(rows.map(JSON.stringify).join('\n')+'\n');
    const message=(role,text,ms)=>record('response_item',{type:'message',role,
      content:[{type:role==='user'?'input_text':'output_text',text}],internal_chat_message_metadata_passthrough:{turn_id:turn}},ms);
    const seed=line([record('turn_context',{turn_id:turn,model:'benchmark',model_context_window:258400},1),
      message('user','Synthetic frontend benchmark',2)]),updates=[];
    for(let i=0;i<6;i++){
      const records=[message('assistant','Synthetic append '+i,1000+i*1000),record('token_usage_record',{
        turn_id:turn,response_id:'frontend-audit-'+i,usage:{input_tokens:1000+i*100,cached_input_tokens:900,
          output_tokens:20,reasoning_output_tokens:10,total_tokens:1020+i*100}},1001+i*1000)];
      if(i===3)records.push(record('event_msg',{type:'item_completed',turn_id:turn,item:{type:'CommandExecution',
        id:'frontend-audit-cmd',command:['synthetic'],exit_code:0,duration:{secs:1,nanos:0},
        aggregated_output:'synthetic output\n'.repeat(32768)}},1002+i*1000));
      updates.push({name:i===3?'execution_output':'append_'+(i+1),data:line(records).toString('base64')});
    }
    const base=path.join(directory,'frozen.jsonl');fs.writeFileSync(base,complete);fs.appendFileSync(base,seed);
    frozen=null;complete=null;global.gc();
    const plan=JSON.stringify({thread,updates}),filename=`rollout-${thread}.jsonl`;
    function home(name){const home=path.join(directory,name);fs.mkdirSync(path.join(home,'sessions'),{recursive:true});
      fs.copyFileSync(base,path.join(home,'sessions',filename));fs.writeFileSync(path.join(home,'updates.json'),plan);return home;}
    function child(args){const result=spawnSync(process.execPath,['--expose-gc',__filename,...args],
      {cwd:root,encoding:'utf8',maxBuffer:16*1024*1024});assert.equal(result.status,0,result.stderr);return JSON.parse(result.stdout);}
    const expected={};
    for(const scenario of ['history','current']){
      const folder=home('oracle-'+scenario);expected[scenario]=child(['--oracle',folder,baseline,scenario]);fs.rmSync(folder,{recursive:true});}
    const results=[];
    for(let repeat=0;repeat<repetitions;repeat++)for(const scenario of ['history','current']){
      for(const mode of repeat%2?['new','old']:['old','new']){
        const folder=home(`${repeat}-${scenario}-${mode}`),result=child(['--worker',folder,mode,baseline,scenario]);
        assert.deepEqual(result.initial,expected[scenario].initial,`${mode}/${scenario}: initial full-source oracle`);
        result.updates.forEach((update,i)=>assert.deepEqual({summary:update.summary,raw:update.raw},expected[scenario].updates[i],
          `${mode}/${scenario}: update ${i} full-source oracle`));
        result.repetition=repeat+1;results.push(result);fs.rmSync(folder,{recursive:true});
      }
    }
    const aggregates=numeric(results);
    console.log(JSON.stringify({equivalent:true,full_source_oracle:true,process_isolation:true,
      gui:false,native_dom:false,cold_disk:false,gc_after_each_update:true,alternating_order:true,
      source_frozen_bytes:bytes,discarded_incomplete_tail_bytes:discarded,
      timestamp_tail_unusable_lines:timestampTailUnusableLines,repetitions,aggregates,results},null,2));
  }finally{fs.rmSync(directory,{recursive:true,force:true});}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
