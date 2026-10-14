/* Local numeric-only audit: node --expose-gc scripts/benchmark_lazy.cjs /absolute/rollout.jsonl */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const readline = require('node:readline');
const crypto = require('node:crypto');
const {spawn, spawnSync} = require('node:child_process');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const hash = value => crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
const mib = bytes => Math.round(bytes / 1048576 * 100) / 100;
const ms = value => Math.round(value * 100) / 100;

async function worker(directory, mode) {
  const python = String.raw`
import importlib.util, json, os, pathlib, resource, sys, time
spec = importlib.util.spec_from_file_location('lazy_benchmark', pathlib.Path.cwd() / 'plugins/codex-trace-probe/server.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
source = next((pathlib.Path(os.environ['CODEX_HOME']) / 'sessions').glob('*.jsonl'))
with source.open('rb') as stream:
    thread = json.loads(stream.readline())['payload']['id']
view = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
store = plugin.TraceStore()
try:
    for line in sys.stdin:
        q = json.loads(line)
        start, cpu = time.perf_counter(), time.process_time()
        child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        if q['method'] == 'snapshot': value = store.snapshot(thread, view, None, q['format'])
        elif q['method'] == 'chunk': value = store.chunk(thread, view, q['snapshot_id'], q['index'], q.get('detail_id'))
        elif q['method'] == 'detail': value = store.detail(thread, view, q['snapshot_id'], q['indices'])
        elif q['method'] == 'release': value = store.release(thread, view)
        else: raise ValueError('Unknown audit operation')
        child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        state = store.views.get(view, {})
        worker_reaped = None
        if q['method'] == 'snapshot' and state.get('outline_worker_pid'):
            try:
                os.waitpid(state['outline_worker_pid'], os.WNOHANG)
                worker_reaped = False
            except ChildProcessError:
                worker_reaped = True
        print(json.dumps({'value': value, 'elapsed_ms': (time.perf_counter()-start)*1000,
            'cpu_ms': (time.process_time()-cpu)*1000,
            'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
            'worker_cpu_ms': ((child_after.ru_utime + child_after.ru_stime) - (child_before.ru_utime + child_before.ru_stime))*1000,
            'worker_peak_rss_bytes': child_after.ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
            'worker_reported_peak_rss_bytes': state.get('outline_worker_peak_rss_bytes'),
            'worker_reaped': worker_reaped}), flush=True)
finally:
    store.close()
`;
  const child = spawn(process.env.PYTHON || 'python3', ['-B', '-u', '-c', python], {cwd:root,
    env:{...process.env, CODEX_HOME:directory}, stdio:['pipe','pipe','inherit']});
  const lines = readline.createInterface({input:child.stdout})[Symbol.asyncIterator]();
  let pythonCPU = 0, pythonPeak = 0, workerCPU = 0, workerPeak = 0, rpcCount = 0;
  async function call(request) {
    child.stdin.write(JSON.stringify(request) + '\n');
    const line = await lines.next();
    if(line.done) throw new Error('Audit backend exited before replying');
    const reply = JSON.parse(line.value); rpcCount++;
    pythonCPU += reply.cpu_ms; pythonPeak = Math.max(pythonPeak, reply.peak_rss_bytes);
    workerCPU += reply.worker_cpu_ms; workerPeak = Math.max(workerPeak, reply.worker_peak_rss_bytes);
    return reply;
  }
  const boxes = new Map();
  const box = selector => {
    if(!boxes.has(selector)) boxes.set(selector, {innerHTML:'', scrollTop:0, scrollLeft:0,
      querySelectorAll:() => [], querySelector:() => null});
    return boxes.get(selector);
  };
  const main = fs.readFileSync(path.join(root, 'index.html'), 'utf8').split('<script>')[1].split('</script>')[0]
    .replace(/bind\(\);\s*bindGutters\(\);\s*boot\(\);\s*$/, '');
  const helpers = fs.readFileSync(path.join(root, 'plugins/codex-trace-probe/viewer-bridge.js'), 'utf8').split('/* Injected')[0];
  const context = vm.createContext({console, localStorage:{getItem:() => null},
    document:{querySelector:box, querySelectorAll:() => []}});
  const run = text => vm.runInContext(text, context);
  run(main); run(helpers);
  run(`function auditSummary(P){return {meta:P.meta,world:P.world,settings:P.settings,rateLimits:P.rateLimits,
    compacts:P.compacts,totals:P.totals,turns:P.turns.map(t=>({id:t.id,part:t.part,parts:t.parts,ctx:t.ctx,
    spanMs:t.spanMs,toolMs:t.toolMs,firstTs:t.firstTs,lastTs:t.lastTs,durationMs:t.durationMs,ttftMs:t.ttftMs,
    usage:usageOfTurn(t),ctxWindow:t.ctxWindow,tokenSteps:t.tokenSteps.map(s=>({total:s.total,last:s.last,cw:s.cw})),
    kinds:t.steps.map(kindOfStep),tools:t.steps.filter(s=>s.kind==='tool'||s.kind==='mcp').map(s=>({id:s.id,
      callId:s.callId,view:toolView(s),outputs:(s.outputRecords||[]).map(r=>r.timestamp),result:s.result?.raw.timestamp}))}))};}
    function ownTurn(t){const own={...t};delete own.sourceTurn;return own;}`);
  async function read(manifest) {
    const stream = run('createRolloutStream()'), decoder = new TextDecoder('utf8');
    for(let index = 0; index < manifest.total_chunks; index++) {
      const reply = await call({method:'chunk', snapshot_id:manifest.snapshot_id, index,
        ...(manifest.detail_id ? {detail_id:manifest.detail_id} : {})});
      const chunk = reply.value, decoded = atob(chunk.data), bytes = new Uint8Array(decoded.length);
      for(let index = 0; index < decoded.length; index++) bytes[index] = decoded.charCodeAt(index);
      assert.equal(bytes.length, chunk.byte_length);
      stream.feed(decoder.decode(bytes, {stream:true}));
    }
    stream.feed(decoder.decode()); stream.finish(); return stream;
  }
  let snapshot, firstBytes, latestBytes = 0;
  async function hydrate(ti) {
    const indices = run(`traceRoundRecordIndices(outline,${ti})`);
    let envelopes = [], total = 0;
    for(let from = 0; from < indices.length; from += 8192) {
      const detail = (await call({method:'detail', snapshot_id:snapshot.snapshot_id,
        indices:indices.slice(from, from + 8192)})).value;
      total += detail.total_bytes;
      const stream = await read(detail); envelopes.push(...stream.records);
    }
    context.details = envelopes;
    run(`P=hydrateTraceRound(outline,${ti},details);details=null;`);
    return total;
  }
  function render(ti) {
    run(`S.parsed=P;S.current={name:'audit'};S.expanded=new Set([${ti}]);
      renderSessionBar();renderTimeline();renderFilterBar();renderStream();`);
  }
  global.gc?.();
  const heapBase = process.memoryUsage().heapUsed, started = performance.now(), cpu = process.cpuUsage();
  try {
    const reply = await call({method:'snapshot', format:mode === 'full' ? 'full' : 'outline'});
    if(mode !== 'full') assert.equal(reply.worker_reaped, true, 'The indexing worker must exit before replying');
    snapshot = reply.value; firstBytes = snapshot.total_bytes;
    context.stream = await read(snapshot);
    if(mode === 'full') run('var P=stream.build();stream=null;');
    else {
      run('var outline=createTraceOutline(stream.records);var P=outline.parsed;stream=null;');
      latestBytes = await hydrate(run('P.turns.length-1'));
    }
    const rounds = run('P.turns.length'); render(rounds-1);
    const firstElapsed = performance.now()-started, firstCPU = process.cpuUsage(cpu);
    global.gc?.();
    const firstHeap = process.memoryUsage().heapUsed, firstRSS = process.memoryUsage().rss;
    const firstPythonCPU = pythonCPU, firstPythonPeak = pythonPeak, firstWorkerCPU = workerCPU,
      firstWorkerPeak = workerPeak, firstRPCs = rpcCount;
    const rss = process.platform === 'win32' ? null : spawnSync('ps', ['-o', 'rss=', '-p', String(child.pid)], {encoding:'utf8'});
    const firstPythonRSS = rss?.status === 0 ? Number(rss.stdout.trim()) * 1024 : null;
    const summaryHash = hash(run('auditSummary(P)'));
    const hashes = [], history = [];
    for(let ti = 0; ti < rounds; ti++) {
      const start = performance.now();
      const bytes = mode === 'full' ? 0 : await hydrate(ti);
      assert.equal(hash(run('auditSummary(P)')), summaryHash, 'Hydrated statistics changed');
      hashes.push(hash(run(`ownTurn(P.turns[${ti}])`)));
      if(mode !== 'full') history.push({round:ti+1, bytes, elapsed_ms:ms(performance.now()-start)});
    }
    const released = (await call({method:'release'})).value;
    assert.equal(released, true);
    return {mode, source_bytes:snapshot.source_bytes || snapshot.total_bytes, rounds,
      first_outline_or_full_bytes:firstBytes, first_detail_bytes:latestBytes,
      first_transfer_bytes:firstBytes+latestBytes, snapshot_ms:ms(reply.elapsed_ms),
      first_elapsed_ms:ms(firstElapsed), first_node_cpu_ms:ms((firstCPU.user+firstCPU.system)/1000),
      heap_base_mib:mib(heapBase), first_heap_mib:mib(firstHeap), first_rss_mib:mib(firstRSS),
      first_python_cpu_ms:ms(firstPythonCPU), first_python_peak_rss_mib:mib(firstPythonPeak),
      first_python_rss_mib:firstPythonRSS === null ? null : mib(firstPythonRSS), first_rpc_count:firstRPCs,
      first_worker_cpu_ms:ms(firstWorkerCPU), first_worker_transient_peak_rss_mib:mib(firstWorkerPeak),
      worker_exit_verified:reply.worker_reaped,
      worker_reported_peak_rss_mib:reply.worker_reported_peak_rss_bytes == null ? null : mib(reply.worker_reported_peak_rss_bytes),
      python_all_cpu_ms:ms(pythonCPU), python_peak_rss_mib:mib(pythonPeak), rpc_count:rpcCount,
      worker_all_cpu_ms:ms(workerCPU), worker_transient_peak_rss_mib:mib(workerPeak),
      summary_hash:summaryHash, detail_hashes:hashes, history};
  } finally {
    child.stdin.end();
    const exit = await new Promise(resolve => child.on('exit', resolve));
    assert.equal(exit, 0, 'Audit backend did not exit cleanly');
  }
}

async function main() {
  if(process.argv[2] === '--worker') {
    console.log(JSON.stringify(await worker(process.argv[3], process.argv[4]))); return;
  }
  if(!process.argv[2]) throw new Error('Provide a local rollout JSONL path');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'trace-lazy-benchmark-'));
  try {
    fs.mkdirSync(path.join(directory, 'sessions'));
    const source = path.resolve(process.argv[2]);
    const file = fs.openSync(source, 'r'), prefix = Buffer.alloc(262144);
    let thread;
    try { const size = fs.readSync(file, prefix); thread = JSON.parse(prefix.subarray(0, size).toString().split('\n')[0]).payload.id; }
    finally { fs.closeSync(file); }
    assert.match(thread, /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i, 'Source metadata must contain a UUID');
    fs.copyFileSync(source, path.join(directory, 'sessions', `rollout-${thread}.jsonl`));
    const results = [];
    for(const mode of ['full', 'lazy']) {
      const result = spawnSync(process.execPath, ['--expose-gc', __filename, '--worker', directory, mode],
        {cwd:root, encoding:'utf8', maxBuffer:8 * 1024 * 1024});
      assert.equal(result.status, 0, result.stderr); results.push(JSON.parse(result.stdout));
    }
    assert.equal(results[0].summary_hash, results[1].summary_hash, 'Outline summary differs from full source');
    assert.deepEqual(results[0].detail_hashes, results[1].detail_hashes, 'Hydrated round differs from full source');
    for(const result of results) { delete result.summary_hash; delete result.detail_hashes; }
    console.log(JSON.stringify({equivalent:true, process_isolation:true, gui:false, cold_disk:false, results}, null, 2));
  } finally { fs.rmSync(directory, {recursive:true, force:true}); }
}
main().catch(error => { console.error(error); process.exitCode=1; });
