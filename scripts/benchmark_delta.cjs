/* Numeric-only local audit: node --expose-gc scripts/benchmark_delta.cjs /rollout.jsonl /unpacked-0.6.0-plugin */
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),vm=require('node:vm');
const readline=require('node:readline'),crypto=require('node:crypto');
const {spawn,spawnSync}=require('node:child_process');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const hash=x=>crypto.createHash('sha256').update(JSON.stringify(x)).digest('hex');
const round=x=>Math.round(x*100)/100;
const mib=x=>round(x/1048576);
const python=String.raw`
import contextlib,importlib.util,json,os,pathlib,resource,sys,time
spec=importlib.util.spec_from_file_location('delta_benchmark',os.environ['BENCHMARK_PLUGIN'])
plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
source=next((pathlib.Path(os.environ['CODEX_HOME'])/'sessions').glob('*.jsonl'))
with source.open('rb') as f:thread=json.loads(f.readline())['payload']['id']
view='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';store=plugin.TraceStore()
counts={'source_read_bytes':0,'projected_source_bytes':0,'generated_outline_bytes':0}
original_open=plugin.open_record;original_build=plugin.build_outline_isolated
class Metered:
    def __init__(self,file):self.file=file
    def read(self,*a):
        data=self.file.read(*a);counts['source_read_bytes']+=len(data);return data
    def readline(self,*a):
        data=self.file.readline(*a);counts['source_read_bytes']+=len(data);return data
    def __getattr__(self,k):return getattr(self.file,k)
@contextlib.contextmanager
def measured_open(*a,**k):
    with original_open(*a,**k) as f:yield Metered(f)
def measured_build(source,*a,**k):
    counts['projected_source_bytes']+=os.fstat(source.fileno()).st_size
    value=original_build(source,*a,**k);counts['generated_outline_bytes']+=value[2];return value
plugin.open_record=measured_open;plugin.build_outline_isolated=measured_build
def usage():
    own=resource.getrusage(resource.RUSAGE_SELF);child=resource.getrusage(resource.RUSAGE_CHILDREN)
    factor=1 if sys.platform=='darwin' else 1024
    return {'backend_cpu_ms':(own.ru_utime+own.ru_stime)*1000,'worker_cpu_ms':(child.ru_utime+child.ru_stime)*1000,
        'backend_peak_rss_bytes':own.ru_maxrss*factor,'worker_peak_rss_bytes':child.ru_maxrss*factor}
mark={};started=0
try:
    for line in sys.stdin:
        q=json.loads(line);method=q['method']
        if method=='snapshot':value=store.snapshot(thread,view,q.get('known_revision'),'outline')
        elif method=='chunk':value=store.chunk(thread,view,q['snapshot_id'],q['index'],q.get('detail_id'))
        elif method=='detail':value=store.detail(thread,view,q['snapshot_id'],q['indices'])
        elif method=='append':
            import base64
            with source.open('ab') as f:f.write(base64.b64decode(q['data']))
            value=True
        elif method=='mark':
            mark=usage();started=time.perf_counter()
            for key in counts:counts[key]=0
            value=True
        elif method=='stats':
            value=usage()
            for key in ['backend_cpu_ms','worker_cpu_ms']:value[key]-=mark[key]
            state=store.views.get(view,{})
            value.update(counts,meter_elapsed_ms=(time.perf_counter()-started)*1000,
                worker_last_peak_rss_bytes=state.get('outline_worker_peak_rss_bytes'))
        elif method=='release':value=store.release(thread,view)
        else:raise ValueError('Unknown audit operation')
        print(json.dumps(value),flush=True)
finally:store.close()
`;
async function worker(home,mode,baseline) {
  const backend=mode==='old'?path.join(baseline,'server.py'):path.join(root,'plugins/codex-trace-probe/server.py');
  const child=spawn(process.env.PYTHON||'python3',['-B','-u','-c',python],{cwd:root,
    env:{...process.env,CODEX_HOME:home,BENCHMARK_PLUGIN:backend},stdio:['pipe','pipe','inherit']});
  const exited=new Promise(resolve=>child.on('exit',resolve));
  const lines=readline.createInterface({input:child.stdout})[Symbol.asyncIterator]();
  let rpc=0;
  async function call(q){child.stdin.write(JSON.stringify(q)+'\n');const line=await lines.next();
    assert.equal(line.done,false,'backend exited before replying');rpc++;return JSON.parse(line.value);}
  const mainPath=mode==='old'?path.join(baseline,'assets/index.html'):path.join(root,'index.html');
  const bridgePath=mode==='old'?path.join(baseline,'viewer-bridge.js'):path.join(root,'plugins/codex-trace-probe/viewer-bridge.js');
  const main=fs.readFileSync(mainPath,'utf8').split('<script>')[1].split('</script>')[0].replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/,'');
  const helpers=fs.readFileSync(bridgePath,'utf8').split('/* Injected')[0];
  const boxes=new Map(),box=key=>{if(!boxes.has(key))boxes.set(key,{innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null});return boxes.get(key);};
  const context=vm.createContext({console,localStorage:{getItem:()=>null},document:{querySelector:box,querySelectorAll:()=>[]}});
  const run=code=>vm.runInContext(code,context);run(main);run(helpers);
  run('var outline=null,P=null;function ownTurn(t){const own={...t};delete own.sourceTurn;return own;}');
  let snapshot=null,cache=new Map(),selected=null,transferred=0;
  async function read(manifest) {
    const stream=run('createRolloutStream()'),decoder=new TextDecoder('utf-8',{fatal:true});
    for(let index=0;index<manifest.total_chunks;index++) {
      const c=await call({method:'chunk',snapshot_id:snapshot.snapshot_id,index,...(manifest.detail_id?{detail_id:manifest.detail_id}:{})});
      const text=atob(c.data),bytes=new Uint8Array(text.length);
      for(let i=0;i<text.length;i++)bytes[i]=text.charCodeAt(i);
      transferred+=bytes.length;stream.feed(decoder.decode(bytes,{stream:true}));
    }
    stream.feed(decoder.decode());stream.finish();return stream.records;
  }
  async function hydrate(ti,reuse) {
    const indices=run(`traceRoundRecordIndices(outline,${ti})`);
    const anchor=run(`turnAnchor(outline.parsed.turns[${ti}])`);
    if(!reuse||selected!==anchor)cache=new Map();
    const missing=indices.filter(i=>!cache.has(i));
    for(let from=0;from<missing.length;from+=8192) {
      const detail=await call({method:'detail',snapshot_id:snapshot.snapshot_id,indices:missing.slice(from,from+8192)});
      for(const row of await read(detail))cache.set(row.index,row);
    }
    context.detail=indices.map(i=>cache.get(i));run(`P=hydrateTraceRound(outline,${ti},detail);detail=null;`);
    cache=new Map(indices.map(i=>[i,cache.get(i)]));selected=anchor;
  }
  // Each mode runs in its own Node and Python processes and receives identical bytes.
  try {
    snapshot=await call({method:'snapshot'});context.rows=await read(snapshot);
    run('outline=createTraceOutline(rows);rows=null;');await hydrate(run('outline.parsed.turns.length-1'),false);
    const updates=JSON.parse(fs.readFileSync(path.join(home,'updates.json'),'utf8')),metrics=[];
    for(let n=0;n<updates.length;n++) {
      const update=updates[n];
      await call({method:'append',data:update.data});
      if(update.historical)await hydrate(0,false);
      global.gc?.();await call({method:'mark'});
      const cpu=process.cpuUsage(),start=performance.now(),beforeRPC=rpc;transferred=0;
      const prior=snapshot;snapshot=await call({method:'snapshot',known_revision:prior.session.revision});
      context.rows=await read(snapshot);
      const outlineBytes=transferred;
      if(mode==='new')assert.equal(snapshot.status,'delta');else assert.equal(snapshot.status,'ready');
      run(snapshot.status==='delta'?'outline=appendTraceOutline(outline,rows);rows=null;':'outline=createTraceOutline(rows);rows=null;');
      const ti=update.historical?0:run('outline.parsed.turns.length-1');
      await hydrate(ti,mode==='new'&&snapshot.status==='delta');
      run(`S.parsed=P;S.current={name:'audit'};S.expanded=new Set([${ti}]);renderSessionBar();renderTimeline();renderFilterBar();renderStream();`);
      const elapsed=performance.now()-start,used=process.cpuUsage(cpu),stats=await call({method:'stats'});
      global.gc?.();
      const rss=spawnSync('ps',['-o','rss=','-p',String(child.pid)],{encoding:'utf8'});
      assert.equal(rss.status,0,'Cannot measure current backend RSS: '+(rss.error?.message||rss.stderr));
      metrics.push({case:update.name,source_append_bytes:Buffer.from(update.data,'base64').length,status:snapshot.status,
        outline_transfer_bytes:outlineBytes,detail_transfer_bytes:transferred-outlineBytes,total_transfer_bytes:transferred,
        elapsed_ms:round(elapsed),frontend_cpu_ms:round((used.user+used.system)/1000),rpc_count:rpc-beforeRPC-1,
        frontend_heap_mib:mib(process.memoryUsage().heapUsed),frontend_rss_mib:mib(process.memoryUsage().rss),
        backend_rss_mib:mib(Number(rss.stdout.trim())*1024),
        ...Object.fromEntries(Object.entries(stats).map(([k,v])=>[k,k.endsWith('_ms')?round(v):v])),
        summary_hash:hash(run('traceSummarySignature(P)')),selected_raw_hash:hash(run(`ownTurn(P.turns[${ti}])`))});
    }
    // This full-source oracle runs only after the measured updates, so it cannot inflate their memory.
    const source=fs.readdirSync(path.join(home,'sessions'))[0];context.source=fs.readFileSync(path.join(home,'sessions',source),'utf8');
    run('var full=parseRollout(source);source=null;');
    assert.equal(run('traceSummarySignature(outline.parsed)'),run('traceSummarySignature(full)'));
    const rounds=run('full.turns.length');
    for(let ti=0;ti<rounds;ti++) {
      await hydrate(ti,false);
      assert.equal(hash(run(`ownTurn(P.turns[${ti}])`)),hash(run(`ownTurn(full.turns[${ti}])`)),`raw round ${ti}`);
    }
    assert.equal(await call({method:'release'}),true);
    return {mode,rounds,full_source_equal:true,updates:metrics};
  } finally {child.stdin.end();assert.equal(await exited,0,'backend failed to close');}
}

async function main(){
  if(process.argv[2]==='--worker'){console.log(JSON.stringify(await worker(process.argv[3],process.argv[4],process.argv[5])));return;}
  assert.ok(process.argv[2]&&process.argv[3],'Provide a rollout and an unpacked 0.6.0 plugin directory');
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'trace-delta-benchmark-'));
  try {
    const frozen=fs.readFileSync(path.resolve(process.argv[2]));
    const complete=frozen.subarray(0,frozen.lastIndexOf(10)+1),thread=JSON.parse(complete.subarray(0,complete.indexOf(10)).toString()).payload.id;
    assert.match(thread,/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i);
    const record=(type,payload,ms)=>({type,payload,timestamp:new Date(ms).toISOString()});
    const baseTime=Date.parse(JSON.parse(complete.toString().trim().split('\n').at(-1)).timestamp)||Date.now();
    const turn='trace-benchmark-turn',line=rows=>Buffer.from(rows.map(JSON.stringify).join('\n')+'\n');
    const message=(role,text,ms)=>record('response_item',{type:'message',role,content:[{type:role==='user'?'input_text':'output_text',text}],
      internal_chat_message_metadata_passthrough:{turn_id:turn}},baseTime+ms);
    const seed=line([record('turn_context',{turn_id:turn,model:'audit',model_context_window:258400},baseTime+1),message('user','Synthetic benchmark update',2)]);
    const updates=[];
    for(let i=0;i<3;i++)updates.push({name:'small_append_'+(i+1),data:line([message('assistant','Synthetic progress '+i,1000+i*1000),
      record('token_usage_record',{turn_id:turn,response_id:'audit-'+i,usage:{input_tokens:1000+i*100,cached_input_tokens:900,output_tokens:20,
        reasoning_output_tokens:10,total_tokens:1020+i*100}},baseTime+1001+i*1000)]).toString('base64')});
    updates.push({name:'large_execution_output',data:line([record('event_msg',{type:'item_completed',turn_id:turn,
      item:{type:'CommandExecution',id:'audit-cmd',command:['synthetic'],exit_code:0,duration:{secs:1,nanos:0},
        aggregated_output:'synthetic output\n'.repeat(131072)}},baseTime+5000)]).toString('base64')});
    updates.push({name:'historical_selection_append',historical:true,data:line([message('assistant','Synthetic final',6000)]).toString('base64')});
    const results=[];
    for(const mode of ['old','new']) {
      const home=path.join(directory,mode);fs.mkdirSync(path.join(home,'sessions'),{recursive:true});
      fs.writeFileSync(path.join(home,'sessions',`rollout-${thread}.jsonl`),Buffer.concat([complete,seed]));
      fs.writeFileSync(path.join(home,'updates.json'),JSON.stringify(updates));
      const child=spawnSync(process.execPath,['--expose-gc',__filename,'--worker',home,mode,path.resolve(process.argv[3])],
        {cwd:root,encoding:'utf8',maxBuffer:16*1024*1024});
      assert.equal(child.status,0,child.stderr);results.push(JSON.parse(child.stdout));
    }
    for(let i=0;i<updates.length;i++) {
      assert.equal(results[0].updates[i].summary_hash,results[1].updates[i].summary_hash,`summary update ${i}`);
      assert.equal(results[0].updates[i].selected_raw_hash,results[1].updates[i].selected_raw_hash,`raw update ${i}`);
    }
    for(const result of results)for(const update of result.updates){delete update.summary_hash;delete update.selected_raw_hash;}
    console.log(JSON.stringify({equivalent:true,process_isolation:true,gui:false,cold_disk:false,
      source_frozen_bytes:complete.length,discarded_incomplete_tail_bytes:frozen.length-complete.length,results},null,2));
  } finally {fs.rmSync(directory,{recursive:true,force:true});}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
