/* Source indices belong to the immutable backend snapshot, not to model-supplied ordinals. */
function createTraceOutline(envelopes) {
  return appendTraceOutline(null, envelopes);
}
function appendTraceOutline(outline, envelopes) {
  if(outline && !envelopes.length) return outline;
  const records = outline ? outline.records.slice() : [], indices = outline ? outline.indices.slice() : [];
  for(const row of envelopes) {
    if(!row || !Number.isSafeInteger(row.index) || row.index < 0 ||
      row.index <= (indices.at(-1) ?? -1) || !row.record || typeof row.record !== 'object' || Array.isArray(row.record))
      throw new Error('轮次摘要记录索引无效');
    indices.push(row.index); records.push(row.record);
  }
  const sources = new WeakMap();
  records.forEach((record,i) => {
    sources.set(record, indices[i]);
    if(record.payload && typeof record.payload === 'object') sources.set(record.payload, indices[i]);
  });
  return {records, indices, sources, parsed:parseRecords(records)};
}
function traceSummarySignature(P) {
  return JSON.stringify({totals:P.totals, rounds:P.turns.map(t=>({anchor:turnAnchor(t), id:t.id, part:t.part, parts:t.parts,
    usage:usageOfTurn(t), cacheIssue:t.cacheIssue, requestCount:t.requestCount, spanMs:t.spanMs, toolMs:t.toolMs,
    firstTs:t.firstTs, lastTs:t.lastTs, ttftMs:t.ttftMs, ctxWindow:t.ctxWindow, context:contextOfTurn(t), collab:t.collab,
    startedAt:t.startedAt, completedAt:t.completedAt, durationMs:t.durationMs,
    tokens:t.tokenSteps.map(st=>[st.total,st.last,st.cw]),
    steps:t.steps.map(st=>[kindOfStep(st),st.callId,st.id,st.ts,st.kind==='tool'||st.kind==='mcp'?toolView(st):null])}))});
}
function traceRoundRecordIndices(outline, ti) {
  const t = outline.parsed.turns[ti];
  if(!t) throw new Error('轮次已不存在，请刷新后重试');
  const found = new Set(), add = record => {
    const index = outline.sources.get(record);
    if(Number.isSafeInteger(index)) found.add(index);
  };
  add(t.ctx);
  for(const step of t.steps) {
    add(step.raw); add(step.context); add(step.result?.raw);
    for(const output of step.outputRecords || []) add(output);
  }
  for(const usage of t.usageRecords) add(usage.raw);
  return [...found].sort((a,b) => a-b);
}
function hydrateTraceRound(outline, ti, envelopes) {
  const expected = traceRoundRecordIndices(outline, ti), replacement = new Map();
  if(envelopes.length !== expected.length) throw new Error('轮次详情记录数量不一致');
  for(let i=0; i<envelopes.length; i++) {
    const row = envelopes[i];
    if(!row || row.index !== expected[i] || !row.record || typeof row.record !== 'object' || Array.isArray(row.record))
      throw new Error('轮次详情记录索引不一致');
    replacement.set(row.index, row.record);
  }
  const P = parseRecords(outline.records.map((record, i) => replacement.get(outline.indices[i]) || record));
  if(P.meta?.id !== outline.parsed.meta?.id || traceSummarySignature(P)!==traceSummarySignature(outline.parsed))
    throw new Error('轮次详情与摘要统计不一致，已拒绝显示');
  return P;
}
function traceRoundSignature(turn) {
  const own = {...turn}; delete own.sourceTurn;
  return JSON.stringify(own);
}
function createTraceDetailCache(outline, ti, envelopes, parsed) {
  const turn = {...parsed.turns[ti]}; delete turn.sourceTurn;
  return {anchor:turnAnchor(outline.parsed.turns[ti]), records:new Map(envelopes.map(row=>[row.index,row])),
    signature:traceRoundSignature(outline.parsed.turns[ti]), turn};
}
function reuseTraceRound(outline, ti, cache) {
  const summary = outline.parsed.turns[ti];
  if(!cache || !summary || cache.anchor!==turnAnchor(summary)) return null;
  const indices = traceRoundRecordIndices(outline,ti);
  if(indices.length!==cache.records.size || indices.some(index=>!cache.records.has(index)) ||
    traceRoundSignature(summary)!==cache.signature) return null;
  // Share the new outline's other rounds and globals; never retain an old sourceTurn graph.
  const turns = outline.parsed.turns.slice(), turn = {...cache.turn};
  if(summary.sourceTurn) turn.sourceTurn=summary.sourceTurn;
  turns[ti]=turn;
  return {...outline.parsed,turns};
}

/* Injected before the existing viewer initializes; the browser version keeps its HTTP source.
 * https://developers.openai.com/plugins/build/app-quickstart (MCP Apps bridge) */
(() => {
  'use strict';
  const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
  let viewId = crypto.randomUUID();
  const pending = new Map();
  const snapshotFormat = 'outline';
  let sequence = 0, initialized = false, initializing = null, disposed = false, expectedThread = null;
  let committedStream = null, committedBytes = 0, forceFull = false, closing = null;
  let active = false, booted = false, intersecting = false, pageShown = true, generation = 0, activated = false;
  let manuallyPaused = false;
  let outline = null, outlineSnapshot = null, cachedDetail = null, detailPromise = null, detailController = null, detailSequence = 0;
  const poll = startScopedPolling;
  startScopedPolling = function() { if(active && !disposed) poll(); };
  const maxChunkBytes = 384 * 1024;
  S.scope = 'plugin-pending'; S.mode = 'server';
  document.body.classList.add('scoped'); $('#pick').style.display = 'none';
  $('#mode').textContent = '会话轨迹 · 插件'; $('#mode').className = 'chip b';
  function send(message) { window.parent.postMessage({jsonrpc:'2.0', ...message}, '*'); }
  function request(method, params, signal, release = false, timeoutMs = 10000, requestView = viewId) {
    if(disposed && !release || !active && !release && method!=='ui/initialize' || signal?.aborted) return Promise.reject(new Error('页面请求已取消'));
    const id = requestView + ':' + ++sequence;
    return new Promise((resolve, reject) => {
      const finish = (error, result) => {
        const item = pending.get(id); if(!item) return;
        pending.delete(id); clearTimeout(item.timer); signal?.removeEventListener('abort', cancel);
        if(error) reject(error); else resolve(result);
      };
      const cancel = () => {
        send({method:'notifications/cancelled', params:{requestId:id, reason:'页面请求已取消'}});
        finish(new Error('页面请求已取消'));
      };
      const timer = setTimeout(() => {
        send({method:'notifications/cancelled', params:{requestId:id, reason:release ? '页面关闭释放超过 1 秒' : '请求超过 10 秒'}});
        finish(new Error(release ? '页面关闭释放超过 1 秒' : '请求超过 10 秒，请重试'));
      }, timeoutMs);
      pending.set(id, {timer, finish, cancel, release}); signal?.addEventListener('abort', cancel, {once:true});
      send({id, method, params});
    });
  }
  function onMessage(event) {
    if(event.source !== window.parent) return;
    const message = event.data;
    if(!message || message.jsonrpc !== '2.0') return;
    if(message.method === 'ui/resource-teardown') {
      dispose().then(() => {
        if(typeof message.id === 'string' || typeof message.id === 'number') send({id:message.id, result:{}});
      }); return;
    }
    const item = pending.get(message.id);
    if(!item || disposed && !item.release || ('result' in message) === ('error' in message)) return;
    item.finish(message.error ? new Error('宿主调用失败：' + String(message.error.message || '未提供错误信息').slice(0,500)) : null,
      message.result);
  }
  function initialize() {
    if(initialized) return Promise.resolve();
    if(initializing) return initializing;
    const promise = request('ui/initialize', {protocolVersion:'2026-01-26', appCapabilities:{},
      appInfo:{name:'codex-trace-viewer', version:'0.11.0'}}).then(result => {
      if(result?.protocolVersion !== '2026-01-26') throw new Error('宿主插件协议不兼容');
      if(!disposed) { initialized = true; send({method:'ui/notifications/initialized', params:{}}); }
    }).finally(() => { if(initializing===promise) initializing = null; });
    initializing=promise; return promise;
  }
  async function callTool(name, args, signal, release = false, timeoutMs = 10000, requestView = viewId, requestThread = expectedThread) {
    const epoch=generation;
    if(!release) {
      if(!active || disposed) throw new Error('页面请求已取消');
      await initialize();
      if(!active || disposed || epoch!==generation || signal?.aborted) throw new Error('页面请求已取消');
    }
    const nonce = crypto.randomUUID();
    const result = await request('tools/call', {name, arguments:{view_id:requestView, request_nonce:nonce, ...args}}, signal, release, timeoutMs, requestView);
    if(!release && (!active || disposed || epoch!==generation)) throw new Error('页面请求已取消');
    if(result?.isError) throw new Error('轨迹工具调用失败：' + String(result.content?.[0]?.text || '未提供错误信息').slice(0,500));
    const data = result?.structuredContent;
    if(!data || data.view_id !== requestView || data.request_nonce !== nonce)
      throw new Error('返回页面或请求标识不一致，已拒绝结果');
    if(!uuid.test(data.server_instance_id || '') || !Number.isSafeInteger(data.request_id) || data.request_id < 1)
      throw new Error('轨迹返回格式无效');
    if(data.identity_status === 'conflict') throw new Error('宿主会话身份冲突，已拒绝结果');
    if(!['candidate','verified','missing'].includes(data.identity_status) ||
      !(data.thread_id === null || typeof data.thread_id === 'string' && uuid.test(data.thread_id)))
      throw new Error('宿主会话身份无效');
    if(requestThread && data.thread_id !== requestThread) throw new Error('宿主会话身份已改变，请重新打开轨迹面板');
    if(!release && data.thread_id && !expectedThread) expectedThread = data.thread_id;
    return data;
  }
  function sessionOf(data) {
    const s = data.session;
    if(!s || s.thread_id !== data.thread_id || typeof s.path !== 'string' || !s.path || s.path.length > 8192 ||
      typeof s.name !== 'string' || s.name.length > 8192 || typeof s.revision !== 'string' || !s.revision || s.revision.length > 1024)
      throw new Error('轨迹记录与本会话绑定不一致');
    return s;
  }
  async function readEnvelopes(data, snapshot, signal, current, previousCount=0) {
    const startOffset = data.status==='delta' ? data.start_offset : 0;
    if(!Number.isSafeInteger(data.total_bytes) || data.total_bytes < 0 ||
      !Number.isSafeInteger(startOffset) || startOffset<0 || startOffset>data.total_bytes ||
      !Number.isSafeInteger(data.chunk_bytes) || data.chunk_bytes < 1 || data.chunk_bytes > maxChunkBytes ||
      data.total_chunks !== Math.ceil((data.total_bytes-startOffset)/data.chunk_bytes) ||
      !Number.isSafeInteger(data.record_count) || data.record_count<previousCount) throw new Error('轮次分页信息无效');
    const records=[], decoder=new TextDecoder('utf-8',{fatal:true});
    let fragments=[];
    const feed=text=>{
      let start=0, end;
      while((end=text.indexOf('\n',start))!==-1) {
        fragments.push(text.slice(start,end));
        records.push(JSON.parse(fragments.join(''))); fragments=[]; start=end+1;
      }
      if(start<text.length) fragments.push(text.slice(start));
    };
    let offset = startOffset;
    for(let i=0; i<data.total_chunks; i++) {
      const args = {snapshot_id:snapshot.snapshot_id, chunk_index:i};
      if(data.detail_id) args.detail_id = data.detail_id;
      const chunk = await callTool('trace_chunk', args, signal);
      if(!current()) throw new Error('轮次请求已取消');
      const expected = Math.min(data.chunk_bytes, data.total_bytes-offset);
      if(chunk.server_instance_id !== snapshot.server_instance_id || chunk.snapshot_id !== snapshot.snapshot_id ||
        chunk.revision !== snapshot.session.revision || chunk.detail_id !== data.detail_id || chunk.chunk_index !== i ||
        chunk.done !== (i===data.total_chunks-1) || chunk.encoding !== 'base64' || chunk.byte_offset !== offset ||
        chunk.byte_length !== expected || typeof chunk.data !== 'string' || chunk.data.length !== 4*Math.ceil(expected/3) ||
        chunk.data.length > 512*1024 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(chunk.data))
        throw new Error('轮次分页与快照不一致');
      const decoded = atob(chunk.data);
      if(decoded.length !== expected) throw new Error('轮次分页解码字节数不一致');
      const bytes = new Uint8Array(decoded.length);
      for(let index=0; index<decoded.length; index++) bytes[index]=decoded.charCodeAt(index);
      feed(decoder.decode(bytes, {stream:true})); offset += bytes.length;
      scopedStatus((data.detail_id? '载入本轮详情 ' : startOffset? '追加轮次摘要 ' : '载入全部轮次摘要 ') +
        (offset-startOffset).toLocaleString() + '/' + (data.total_bytes-startOffset).toLocaleString() + ' 字节');
      if((i+1)%16===0) { await new Promise(resolve=>setTimeout(resolve,0)); if(!current()) throw new Error('轮次请求已取消'); }
    }
    feed(decoder.decode());
    if(fragments.length || offset!==data.total_bytes || records.length!==data.record_count-previousCount)
      throw new Error('轮次分页记录不完整');
    return records;
  }
  async function loadRoundDetails(ti, signal, current, expand=true, presented=false) {
    const source = outline, snapshot = outlineSnapshot, state = S.lazyDetails;
    if(!source || !snapshot || !source.parsed.turns[ti]) return false;
    if(state.loadedIndex===ti) { S.expanded=new Set([ti]); renderStream(); setSelTurn(ti); return true; }
    // One hydrated round is retained. Every other round is rebuilt from the small immutable outline.
    const selection = S.sel?.ti===ti? {idx:S.sel.idx,tab:S.tab} : null;
    state.loadedIndex=null; state.loadingIndex=ti;
    if(!presented) {
      S.schemaRestore=null; S.schemaSeq=(S.schemaSeq||0)+1; S.truncMap.clear();
      S.parsed=source.parsed; S.expanded=new Set(expand? [ti] : []); setSelTurn(ti); renderStream();
    }
    const anchor = turnAnchor(source.parsed.turns[ti]);
    if(cachedDetail?.anchor!==anchor) cachedDetail=null;
    const retained = cachedDetail?.records || new Map(), indices = traceRoundRecordIndices(source, ti);
    const missing = indices.filter(index=>!retained.has(index)), received = new Map();
    try {
      for(let start=0; start<missing.length; start+=8192) {
        const batch = missing.slice(start,start+8192);
        const detail = await callTool('trace_detail', {snapshot_id:snapshot.snapshot_id, record_indices:batch}, signal);
        if(!current()) return false;
        if(detail.server_instance_id!==snapshot.server_instance_id || detail.snapshot_id!==snapshot.snapshot_id ||
          detail.revision!==snapshot.session.revision || !uuid.test(detail.detail_id||'') ||
          JSON.stringify(detail.record_indices)!==JSON.stringify(batch)) throw new Error('轮次详情索引与快照不一致');
        const rows = await readEnvelopes(detail,snapshot,signal,current);
        if(rows.length!==batch.length || rows.some((row,i)=>row?.index!==batch[i])) throw new Error('轮次详情记录索引不一致');
        rows.forEach(row=>received.set(row.index,row));
      }
      if(!current()) return false;
      const envelopes = indices.map(index=>received.get(index)||retained.get(index));
      const hydrated = hydrateTraceRound(source,ti,envelopes);
      cachedDetail=createTraceDetailCache(source,ti,envelopes,hydrated);
      const P = reuseTraceRound(source,ti,cachedDetail);
      state.loadedIndex=ti; state.loadingIndex=null;
      applySessionSnapshot(S.current,P,true);
      if(selection) { S.tab=selection.tab; selectStep(ti,selection.idx); }
      summaryStatus(ti); return true;
    } catch(error) {
      if(!current()) return false;
      cachedDetail=null; state.loadingIndex=null; renderStream(true);
      scopedStatus('本轮详情载入失败：'+String(error.message)+'。再次展开本轮重试。',true); return false;
    }
  }
  function summaryStatus(ti) {
    scopedStatus('全部轮次摘要 · 第 '+(ti+1)+' 轮详情 · 实时更新' +
      (outlineSnapshot.skipped_records? ' · '+outlineSnapshot.skipped_records+' 条源记录无效' : ''));
  }
  async function loadRound(ti) {
    const anchor = outline?.parsed.turns[ti] && turnAnchor(outline.parsed.turns[ti]), seq=++detailSequence;
    detailController?.abort();
    if(S.polling) await S.polling;
    if(detailPromise) await detailPromise;
    if(!active || disposed || seq!==detailSequence || !outline || anchor==null) return false;
    ti=outline.parsed.turns.findIndex(t=>turnAnchor(t)===anchor);
    if(ti<0) return false;
    const controller = new AbortController(); detailController=controller;
    const current = ()=>active && !disposed && seq===detailSequence && !controller.signal.aborted;
    detailPromise=loadRoundDetails(ti,controller.signal,current);
    try { return await detailPromise; }
    finally { if(detailController===controller) { detailController=null; detailPromise=null; } }
  }
  refreshScoped = function() {
    if(!active || disposed) return Promise.resolve();
    if(detailPromise) return detailPromise;
    if(S.polling) return S.polling;
    const token = ++S.loadToken, controller = new AbortController(), current = () => active && !disposed && !controller.signal.aborted && token === S.loadToken;
    S.pollAbort = controller;
    S.polling = (async () => {
      try {
        const args = {format:snapshotFormat};
        if(S.current && (committedStream || outline) && !forceFull) args.known_revision=S.current.revision;
        const data = await callTool('trace_snapshot', args, controller.signal); if(!current()) return;
        if(!['waiting','unchanged','ready','delta'].includes(data.status) || data.truncated !== false)
          throw new Error('轨迹快照状态无效');
        if(data.status === 'waiting') {
          scopedStatus(S.parsed ? '等待本会话记录恢复；保留上次快照' : '等待本会话写入轨迹记录…');
          if(!S.parsed) $('#stream').innerHTML = '<div class="empty">等待本会话的轨迹记录。首次保存后会自动载入。</div>';
          return;
        }
        if(data.identity_status !== 'verified' || data.snapshot_match !== true || !data.thread_id)
          throw new Error('轨迹快照身份尚未核实，已拒绝绑定');
        const s = sessionOf(data);
        if(snapshotFormat==='outline') {
          if(data.format!=='outline' || !uuid.test(data.snapshot_id||'') ||
            !Number.isSafeInteger(data.source_bytes) || data.source_bytes<1 ||
            !Number.isSafeInteger(data.skipped_records) || data.skipped_records<0) throw new Error('插件未返回完整轮次摘要，请更新插件并重新打开页面');
          if(data.status==='unchanged') {
            if(!outline || !outlineSnapshot || !S.current || S.scope!==data.thread_id || s.revision!==S.current.revision ||
              s.path!==S.current.path || data.snapshot_id!==outlineSnapshot.snapshot_id || data.total_bytes!==committedBytes ||
              data.server_instance_id!==outlineSnapshot.server_instance_id || data.source_bytes!==outlineSnapshot.source_bytes ||
              data.skipped_records!==outlineSnapshot.skipped_records ||
              data.start_offset!==committedBytes || data.total_chunks!==0 || data.record_count!==outline.records.length)
              throw new Error('未变轮次摘要与已显示记录不一致');
            if(S.lazyDetails.loadedIndex==null && S.selTurn!=null)
              await loadRoundDetails(S.selTurn,controller.signal,current,S.expanded.has(S.selTurn));
          } else {
            const delta = data.status==='delta';
            if(delta && (!outline || !outlineSnapshot || !S.current || forceFull || S.scope!==data.thread_id ||
              data.server_instance_id!==outlineSnapshot.server_instance_id || data.snapshot_id===outlineSnapshot.snapshot_id ||
              data.base_revision!==S.current.revision || s.revision===S.current.revision || s.path!==S.current.path ||
              data.start_offset!==committedBytes || data.total_bytes<committedBytes ||
              data.source_bytes<=outlineSnapshot.source_bytes || data.skipped_records<outlineSnapshot.skipped_records ||
              data.record_count<outline.records.length)) throw new Error('增量轮次摘要基线与已显示记录不一致');
            if(!delta && (data.start_offset!==0 || data.total_bytes<1)) throw new Error('轮次摘要必须从文件开头读取');
            const envelopes = await readEnvelopes(data,data,controller.signal,current,delta?outline.records.length:0);
            const model = delta? appendTraceOutline(outline,envelopes) : createTraceOutline(envelopes);
            if(!current()) return;
            if(model.parsed.meta?.id!==data.thread_id) throw new Error('轮次摘要与本会话绑定不一致');
            const emptyDelta = delta && model===outline;
            const selected = S.parsed?.turns[S.selTurn];
            const ti = selected? model.parsed.turns.findIndex(t=>turnAnchor(t)===turnAnchor(selected)) : -1;
            const reused = delta && !emptyDelta? reuseTraceRound(model,ti,cachedDetail) : null;
            outline=model; outlineSnapshot=data; committedBytes=data.total_bytes; forceFull=false;
            if(!delta) cachedDetail=null;
            S.scope=data.thread_id; $('#count').textContent=shortId(S.scope); $('#count').title=S.scope;
            if(!emptyDelta) S.lazyDetails={loadedIndex:reused?ti:null,loadingIndex:null,load:loadRound};
            S.crossed=false; setSessions([s]);
            applySessionSnapshot(s,emptyDelta?S.parsed:reused||model.parsed,!!S.parsed,()=>{
              if(!emptyDelta && !reused) {
                S.selTurn ??= model.parsed.turns.length? model.parsed.turns.length-1 : null;
                S.lazyDetails.loadingIndex=S.selTurn;
              }
            });
            if(!emptyDelta && !reused && S.selTurn!=null) {
              await loadRoundDetails(S.selTurn,controller.signal,current,S.expanded.has(S.selTurn),true);
            }
          }
          if(S.lazyDetails?.loadedIndex!=null) summaryStatus(S.lazyDetails.loadedIndex);
          return;
        }
        if(data.status === 'unchanged') {
          if(!S.current || !committedStream || S.scope !== data.thread_id || s.revision !== S.current.revision ||
            s.path !== S.current.path || data.total_bytes !== committedBytes || data.start_offset !== committedBytes || data.total_chunks !== 0)
            throw new Error('未变快照与已显示记录不一致');
        } else {
          if(!uuid.test(data.snapshot_id || '') || !Number.isSafeInteger(data.total_bytes) || data.total_bytes < 1 ||
            !Number.isSafeInteger(data.start_offset) || data.start_offset < 0 || data.start_offset >= data.total_bytes ||
            !Number.isSafeInteger(data.chunk_bytes) || data.chunk_bytes < 1 || data.chunk_bytes > maxChunkBytes ||
            !Number.isSafeInteger(data.total_chunks) || data.total_chunks !== Math.ceil((data.total_bytes-data.start_offset)/data.chunk_bytes))
            throw new Error('轨迹快照分页信息无效');
          const delta = data.status === 'delta';
          if(delta && (!committedStream || !committedStream.canAppend || !S.current || data.base_revision !== S.current.revision ||
            data.start_offset !== committedBytes || s.path !== S.current.path))
            throw new Error('增量快照基线与已显示记录不一致');
          if(!delta && data.start_offset !== 0) throw new Error('完整快照必须从文件开头读取');
          S.scope = data.thread_id; $('#count').textContent = shortId(S.scope); $('#count').title = S.scope;
          const parser = delta ? committedStream.fork() : createRolloutStream(), decoder = new TextDecoder('utf-8');
          let offset = data.start_offset;
          const progress = () => scopedStatus('已读取 ' + offset.toLocaleString() + '/' + data.total_bytes.toLocaleString() +
            ' 字节 · 已解析 ' + parser.records.length.toLocaleString() + ' 条' + (S.parsed ? ' · 保留上次完整快照' : ''));
          progress();
          for(let i = 0; i < data.total_chunks; i++) {
            const chunk = await callTool('trace_chunk', {snapshot_id:data.snapshot_id, chunk_index:i}, controller.signal);
            if(!current()) return;
            const expected = Math.min(data.chunk_bytes, data.total_bytes-offset);
            if(chunk.server_instance_id !== data.server_instance_id || chunk.snapshot_id !== data.snapshot_id ||
              chunk.revision !== s.revision || chunk.chunk_index !== i || chunk.done !== (i === data.total_chunks - 1) ||
              chunk.encoding !== 'base64' || chunk.byte_offset !== offset || chunk.byte_length !== expected ||
              typeof chunk.data !== 'string' || chunk.data.length !== 4*Math.ceil(expected/3) ||
              chunk.data.length > 512*1024 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(chunk.data))
              throw new Error('轨迹快照分页不一致，已拒绝结果');
            const decoded = atob(chunk.data);
            if(decoded.length !== expected) throw new Error('轨迹分页解码字节数不一致');
            const bytes = new Uint8Array(decoded.length);
            for(let index=0; index<decoded.length; index++) bytes[index] = decoded.charCodeAt(index);
            parser.feed(decoder.decode(bytes, {stream:true})); offset += bytes.length; progress();
            if((i+1)%16 === 0) {
              await new Promise(resolve => setTimeout(resolve, 0));
              if(!current()) return;
            }
          }
          parser.feed(decoder.decode()); parser.finish();
          const P = parser.build();
          if(!current()) return;
          if(offset !== data.total_bytes || P.meta?.id !== S.scope) throw new Error('轨迹正文与本会话绑定不一致');
          S.crossed = false; setSessions([s]); applySessionSnapshot(s, P, !!S.parsed);
          committedStream = parser; committedBytes = offset; forceFull = false;
        }
        scopedStatus('完整记录 · 实时更新 · 插件每 2 秒检查本会话记录');
      } catch(error) {
        if(!current()) return;
        forceFull = true;
        scopedStatus('更新失败：' + String(error.message) + '。点击重试。' + (S.parsed ? ' 保留上次快照。' : ''), true);
        if(!S.parsed) $('#stream').innerHTML = '<div class="empty">无法读取会话轨迹。请检查插件连接，再点击右上「重试」。</div>';
      }
    })().finally(() => { if(S.pollAbort === controller) { S.polling = null; S.pollAbort = null; } });
    return S.polling;
  };
  boot = async function() {
    if(disposed) return;
    booted=true; scopedStatus('等待显示会话轨迹…');
    // The host may require the bridge handshake before displaying its iframe.
    // Handshake alone never reads a record or creates a backend view.
    const bootstrap=initialize(), loading=updateVisibility();
    try { await bootstrap; await loading; }
    catch(error) { if(!disposed && !active && !activated) scopedStatus('插件连接失败：'+String(error.message)+'。显示页面后重试。',true); }
  };
  // The existing Schema inspector uses fetch; this adapter exposes no network fallback.
  globalThis.fetch = async function(input, options = {}) {
    if(typeof input !== 'string' || !input.startsWith('/api/toolschema?') || options.method && options.method !== 'GET')
      throw new Error('插件页面仅允许通过宿主读取当前会话');
    const url = new URL(input, 'https://codex-trace.invalid');
    if(url.pathname !== '/api/toolschema' || [...url.searchParams.keys()].some(key => key !== 'name' && key !== 'ns'))
      throw new Error('工具 Schema 查询参数无效');
    const name = url.searchParams.get('name'), ns = url.searchParams.get('ns') || '';
    if(!name || name.length > 256 || ns.length > 256 || S.scope === 'plugin-pending')
      throw new Error('工具 Schema 尚未绑定当前会话或查询参数无效');
    const data = await callTool('trace_schema', {name, ns}, options.signal);
    if(!data.schema || typeof data.schema !== 'object' || typeof data.schema.found !== 'boolean')
      throw new Error('工具 Schema 返回格式无效');
    return {ok:true, json:async () => data.schema};
  };
  pushStats = function(sess, stats) { sess.stats = stats; };
  const releases=new Set();
  const pauseButton=$('#trace-pause');
  pauseButton.style.display=''; pauseButton.textContent='暂停并释放';
  pauseButton.title='暂停本页轮询并释放快照；恢复后重新载入当前会话';
  pauseButton.onclick=async function() {
    if(disposed) return;
    if(manuallyPaused) {
      manuallyPaused=false; pauseButton.textContent='暂停并释放'; $('#reload').disabled=false;
      scopedStatus('等待显示会话轨迹…'); return updateVisibility();
    }
    manuallyPaused=true; pauseButton.textContent='恢复轨迹'; $('#reload').disabled=true;
    const release=suspend(), epoch=generation;
    scopedStatus('会话轨迹已暂停；正在释放本页快照…');
    const acknowledged=release ? await release : true;
    if(disposed || !manuallyPaused || epoch!==generation) return;
    scopedStatus(acknowledged ? '已暂停并释放本页快照；点击「恢复轨迹」重新载入' :
      '会话轨迹已暂停；释放未确认。后台在停止请求 30 秒后每 5 秒检查回收；点击「恢复轨迹」重新载入', !acknowledged);
  };
  window.addEventListener('message', onMessage);
  // CSS-hidden iframe parents do not change document.hidden. Observe actual viewport intersection.
  // https://developer.mozilla.org/en-US/docs/Web/API/Intersection_Observer_API
  // https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API
  const observer=new IntersectionObserver(entries=>{
    const entry=entries.at(-1);
    intersecting=!!entry?.isIntersecting && entry.intersectionRect.width>0 && entry.intersectionRect.height>0;
    updateVisibility();
  // A positive threshold also notifies edge-adjacent zero area -> positive area,
  // where isIntersecting remains true. https://www.w3.org/TR/intersection-observer/#update-intersection-observations-algo
  }, {threshold:[0,Number.EPSILON]});
  observer.observe(document.documentElement);
  document.addEventListener('visibilitychange', updateVisibility);
  // https://developer.mozilla.org/en-US/docs/Web/API/Window/pagehide_event
  const onPageHide=event=>{ pageShown=false; if(event?.persisted) suspend(); else dispose(); };
  const onPageShow=()=>{ pageShown=true; updateVisibility(); };
  window.addEventListener('pagehide', onPageHide);
  window.addEventListener('pageshow', onPageShow);
  function updateVisibility() {
    if(disposed || !booted) return Promise.resolve();
    if(manuallyPaused || !intersecting || !pageShown || document.hidden) { suspend(); return Promise.resolve(); }
    if(!active) {
      if(activated) viewId=crypto.randomUUID();
      activated=true; active=true; generation++;
      scopedStatus('连接会话轨迹插件…'); startScopedPolling();
      return refreshScoped();
    }
    return S.polling || Promise.resolve();
  }
  function clearData() {
    stopScopedPolling(); S.polling=null; S.pollAbort=null;
    detailSequence++; detailController?.abort(); detailController=null;
    for(const item of [...pending.values()]) if(!item.release) item.cancel();
    initializing=null;
    committedStream = null; committedBytes = 0; forceFull = false;
    outline=null; outlineSnapshot=null; cachedDetail=null; detailPromise=null; S.lazyDetails=null;
    S.parsed = null; S.current = null; S.sessions = []; S.filtered = []; S.rows = []; S.tree = [];
    S.sel = null; S.selTurn = null; S.schemaRestore = null; S.schemaSeq = (S.schemaSeq || 0) + 1;
    S.expanded.clear(); S.truncMap.clear(); S.crossed = false;
    for(const selector of ['#stream','#insp','#sbar','#mbody','#mtitle','#tabs','#slist','#filter','#tl']) $(selector).innerHTML = '';
    $('#modal').classList.remove('on');
  }
  function releaseView(id, thread) {
    // A captured view ID prevents late releases from touching a newly resumed page.
    const deadline = performance.now() + 1000;
    const release = (async () => {
      try {
        if(!initialized) {
          const result = await request('ui/initialize', {protocolVersion:'2026-01-26', appCapabilities:{},
            appInfo:{name:'codex-trace-viewer', version:'0.11.0'}}, null, true, Math.max(1, deadline-performance.now()), id);
          if(result?.protocolVersion !== '2026-01-26') throw new Error('宿主插件协议不兼容');
          initialized=true; send({method:'ui/notifications/initialized', params:{}});
        }
        if(performance.now() >= deadline) throw new Error('页面关闭释放超过 1 秒');
        const data = await callTool('trace_release', {}, null, true, Math.max(1, deadline-performance.now()), id, thread);
        if(typeof data.released !== 'boolean') throw new Error('释放快照返回格式无效');
        return true;
      } catch(error) {
        console.warn('会话轨迹关闭释放失败；后台定时清理兜底：' + String(error.message));
        return false;
      }
    })();
    releases.add(release); release.finally(()=>releases.delete(release)); return release;
  }
  function suspend() {
    if(!active) return;
    const id=viewId, thread=expectedThread;
    active=false; generation++; clearData(); scopedStatus('会话轨迹已暂停；显示页面后恢复');
    return releaseView(id,thread);
  }
  function dispose() {
    if(closing) return closing;
    suspend(); disposed=true; clearData();
    pauseButton.disabled=true;
    observer.disconnect();
    document.removeEventListener('visibilitychange', updateVisibility);
    window.removeEventListener('pagehide', onPageHide);
    window.removeEventListener('pageshow', onPageShow);
    closing=Promise.all([...releases]).finally(()=>window.removeEventListener('message',onMessage));
    return closing;
  }
})();
