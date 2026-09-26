/* OmniVec console: Pipelines list + detail */
(() => {
const X = window.OVX;
const { icon, esc, fmt, ms, ago, when, plural, api, need, invalidate, D, M, T, logo, dot, statusBadge, chipFlow, chart, empty, loadErr, codebox, spinner,
  route, go, render, toast, confirmAction, menu, openDrawer, openModal, closeOverlay, $, $$ } = X;

/* ---------- lifecycle actions ---------- */
async function act(p, action, label) {
  try { await api(`/api/pipelines/${p.id}/${action}`, { method:'POST' }); toast(label); invalidate('pipelines','health'); render({ force:true, silent:true }); }
  catch (e) { toast(e.message, 'err'); }
}
function payloadFrom(p, patch = {}) {
  const keys = ['name','description','sources','docgrok_pipeline','destination_id','vector_index_path','process_existing','metadata_mapping','processing_mode','content_strategy',
    'chunk_config','doc_id_pattern','partition_key_pattern','collision_policy','store_content','content_field','metadata_fields','resource_policy'];
  const raw = (D.pipelines||[]).find(x => x.id === p.id) || p; const out = {};
  keys.forEach(k => { if (raw[k] !== undefined) out[k] = raw[k]; });
  return Object.assign(out, patch);
}
function pipelineMenu(btn, p) {
  menu(btn, [
    { label:'Open', icon:'chev', run:() => go('#/pipelines/'+p.id) },
    p.paused ? { label:'Resume', icon:'play', run:() => act(p,'resume','Pipeline resumed') } : { label:'Pause', icon:'pause', run:() => act(p,'pause','Pipeline paused') },
    { label:'Test search', icon:'search', run:() => go(`#/pipelines/${p.id}/search`) },
    { label:'Ask agent about it', icon:'sparkle', run:() => X.openAgent({ pipeline:p }) },
    '-',
    { label:'Reprocess everything…', icon:'rotate', run:() => resetPipeline(p) },
    { label:'Delete…', icon:'trash', danger:true, run:() => deletePipeline(p) },
  ]);
}
function resetPipeline(p) {
  confirmAction({ title:'Reprocess all documents?', confirm:'Reprocess', danger:true, typed:p.name,
    body:`OmniVec will briefly pause <b>${esc(p.name)}</b>, clear its jobs, and replay the source from the beginning. Existing vectors are overwritten as documents are re-embedded. This uses embedding quota for every document.`,
    onOk: async () => { await api(`/api/pipelines/${p.id}/reset`, { method:'POST' }); toast('Reprocessing started'); invalidate('pipelines'); render({ force:true, silent:true }); } });
}
function deletePipeline(p) {
  confirmAction({ title:'Delete pipeline?', confirm:'Delete pipeline', danger:true, typed:p.name,
    body:`This stops ingestion for <b>${esc(p.name)}</b> and removes its configuration. The source, the vector store and vectors already written are <b>not</b> deleted.`,
    onOk: async () => { await api(`/api/pipelines/${p.id}`, { method:'DELETE' }); toast('Pipeline deleted'); invalidate('pipelines'); go('#/pipelines'); } });
}
X.pipelineActions = { act, resetPipeline, deletePipeline, payloadFrom };

/* ---------- list ---------- */
const listState = { f:'all', q:'', src:'', dst:'', sort:'attention' };
route('pipelines', { render(parts) {
  if (parts[0]) return detail(parts[0], parts[1] || 'overview');
  const qp = X.query(); if (qp.get('f')) listState.f = qp.get('f');
  const all = M.pipelines;
  const count = f => all.filter(p => match(p, f)).length;
  function match(p, f) { return f==='all' ? true : f==='attention' ? (p.health==='warn'||p.health==='err') : f==='healthy' ? (p.health==='ok'||p.health==='unknown') : f==='paused' ? p.paused : true; }
  const html = `<div class="page">
   <div class="ph"><div><h1>Pipelines</h1><p>Each pipeline reads from a source, embeds content with a model or recipe, and writes vectors to a vector store.</p></div>
    <div class="acts"><a class="btn pri" href="#/new">${icon('plus')}New pipeline</a></div></div>
   ${loadErr(['pipelines','insights','health'])}
   ${all.length ? `<div class="row sp" style="margin-bottom:12px;flex-wrap:wrap;gap:10px">
     <div class="seg" id="pf">${[['all','All'],['attention','Needs attention','warn'],['healthy','Healthy','ok'],['paused','Paused']].map(([k,l,d]) => `<button data-f="${k}" class="${listState.f===k?'on':''}">${d?dot(d):''}${l} <span class="ct">${count(k)}</span></button>`).join('')}</div>
     <div class="row" style="flex-wrap:wrap">
      <div class="search-inp" style="width:240px">${icon('search','sm')}<input class="inp" id="pq" placeholder="Filter by name or ID…" value="${esc(listState.q)}"></div>
      <span id="psrc"></span>
      <span id="pdst"></span>
      <select class="sel" id="psort" style="width:150px">${[['attention','Sort: attention'],['name','Sort: name'],['recent','Sort: last write'],['vectors','Sort: vectors']].map(([k,l]) => `<option value="${k}" ${listState.sort===k?'selected':''}>${l}</option>`).join('')}</select>
     </div></div>
   <div class="card" id="ptbl"></div>` : `<div class="card">${empty('flow','No pipelines yet','A pipeline keeps a vector store in sync with a source. It takes about two minutes to set one up.', `<a class="btn pri" href="#/new">${icon('plus')}Create your first pipeline</a>`)}</div>`}
  </div>`;
  return { crumbs:[['Pipelines']], html, mount(v) {
    if (!all.length) return;
    const draw = () => {
      const q = listState.q.toLowerCase();
      let rows = all.filter(p => match(p, listState.f) && (!q || (p.name+' '+p.id+' '+p.src.name+' '+p.dst.name).toLowerCase().includes(q)) && (!listState.src || p.src.id===listState.src) && (!listState.dst || p.dst.id===listState.dst));
      const rank = { err:0, warn:1, unknown:2, ok:3, paused:4 };
      rows.sort(listState.sort==='name' ? (a,b)=>a.name.localeCompare(b.name) : listState.sort==='recent' ? (a,b)=>(X.toDate(b.lastWrite)||0)-(X.toDate(a.lastWrite)||0) : listState.sort==='vectors' ? (a,b)=>(b.vectors||0)-(a.vectors||0) : (a,b)=>rank[a.health]-rank[b.health] || (b.qwait||0)-(a.qwait||0));
      $('#ptbl', v).innerHTML = rows.length ? `<table class="tbl hide-c4 hide-c5"><thead><tr><th>Pipeline</th><th>Flow</th><th>Health</th><th>Mode</th><th class="r">Docs → vectors</th><th class="r">Queue wait</th><th>Last write</th><th></th></tr></thead><tbody>
        ${rows.map(p => `<tr class="clickrow" data-id="${p.id}"><td><div class="nm trunc" style="max-width:260px">${esc(p.name)}</div><div class="id">${p.id}</div></td><td>${chipFlow(p)}</td><td>${statusBadge(p)}</td>
          <td class="muted small">${p.processing_mode==='inline'?'Inline':'Queue'} · ${p.content_strategy==='chunk' ? 'chunk '+((p.chunk_config||{}).chunk_size||'') : 'document'}</td>
          <td class="r">${p.progress!=null ? `<div class="row" style="justify-content:flex-end"><span class="num">${fmt(p.st.source_doc_count)} → ${fmt(p.vectors)}</span><div class="prog ${p.progress>=100?'ok':''}" style="width:56px"><i style="width:${p.progress}%"></i></div></div>` : `<span class="num">— → ${fmt(p.vectors)}</span>`}</td>
          <td class="r num" ${p.qwait>X.QW_SLOW?'style="color:var(--warn);font-weight:600"':''}>${ms(p.qwait)}</td><td class="muted">${ago(p.lastWrite)}</td>
          <td class="r"><button class="btn ghost sm icon" data-more="${p.id}" title="Actions">${icon('more')}</button></td></tr>`).join('')}</tbody></table>
        <div class="row sp muted small" style="padding:10px 14px;border-top:1px solid var(--border)"><span>${rows.length < all.length ? `Filtered: ${rows.length} of ${all.length} pipelines` : ''}</span><span>Queue wait = average time work waits for a worker (from allocation telemetry)</span></div>`
        : empty('filter','No pipelines match these filters','Clear the filters to see all pipelines.', '<button class="btn" id="pclear">Clear filters</button>');
      const c = $('#pclear', v); if (c) c.onclick = () => { Object.assign(listState, { f:'all', q:'', src:'', dst:'' }); render({ silent:true }); };
    };
    draw();
    $('#pf', v).onclick = e => { const b = e.target.closest('[data-f]'); if (!b) return; listState.f = b.dataset.f; $$('#pf button', v).forEach(x => x.classList.toggle('on', x===b)); draw(); };
    $('#pq', v).oninput = e => { listState.q = e.target.value; draw(); };
    X.combo($('#psrc', v), { items:M.sources.map(s => ({ v:s.id, l:s.name, s:T(s.type).short+' · '+plural(X.usedBy(s.id).length,'pipeline'), lg:s.type })), value:listState.src, all:'All sources', placeholder:'Search sources', width:180, onPick:val => { listState.src = val; draw(); } });
    X.combo($('#pdst', v), { items:M.dests.map(d => ({ v:d.id, l:d.name, s:T(d.type).short+' · '+plural(X.usedBy(d.id).length,'pipeline'), lg:d.type })), value:listState.dst, all:'All vector stores', placeholder:'Search vector stores', width:190, onPick:val => { listState.dst = val; draw(); } });
    $('#psort', v).onchange = e => { listState.sort = e.target.value; draw(); };
    $('#ptbl', v).addEventListener('click', e => {
      const m = e.target.closest('[data-more]'); if (m) { e.stopPropagation(); pipelineMenu(m, M.PIPE[m.dataset.more]); return; }
      const r = e.target.closest('tr[data-id]'); if (r) go('#/pipelines/'+r.dataset.id);
    });
  } };
}});

/* ---------- detail ---------- */
const listenerOf = s => s.type==='cosmosdb' ? 'Change feed' : s.type==='azure-blob' ? 'Event Grid' : s.type==='sharepoint' ? `Polling · ${(s.config||{}).poll_interval_seconds||60}s` : (s.config||{}).poll_interval_seconds ? `Polling · ${s.config.poll_interval_seconds}s` : 'Scheduled';
function storeIndex(d) { const vi = ((d.config||{}).vector_indexes||[])[0]; const hc = (M.hd[d.id]||{}).checks||[];
  const pol = hc.find(c => c.check==='vector_policy'), idx = hc.find(c => c.check==='vector_index');
  if (vi) return `${vi.indexType||'index'} · ${vi.distanceFunction||''} · ${vi.dimensions||''}d`;
  if (pol && pol.dimensions) return `${(idx&&idx.detail||'').replace(/^Index type '([^']+)'.*$/,'$1')} · ${pol.dimensions}d`;
  return T(d.type).short; }
const isVerified = id => !!localStorage.getItem('ov-verified-'+id);
const chkIcon = s => `<span style="color:${s==='pass'?'var(--ok)':s==='info'?'var(--info)':s==='warn'?'var(--warn)':'var(--err)'};display:inline-flex">${icon(s==='pass'?'check':s==='info'?'info':'alert','sm')}</span>`;
X.chkIcon = chkIcon;

function problemCallout(p, x) {
  const rec = p.ins && p.ins.recommendation;
  const actions = x.code==='queue_wait' ? `${rec && rec.severity!=='healthy' ? `<button class="btn sm pri" data-fix="alloc">${icon('wand','sm')}Apply recommended allocation</button>` : ''}<a class="btn sm" href="#/pipelines/${p.id}/capacity">Tune capacity</a>`
    : x.code==='trigger' ? `<a class="btn sm pri" href="#/connections/source/${p.src.id}/trigger">${icon('bolt','sm')}Set up Event Grid</a>`
    : x.code==='failures' ? `<button class="btn sm pri" data-fix="diag">${icon('search','sm')}Diagnose</button>`
    : x.code==='missing_source' || x.code==='missing_store' ? `<a class="btn sm" href="#/pipelines/${p.id}/settings">Open settings</a>`
    : `<button class="btn sm" data-fix="diag">Diagnose</button>`;
  const extra = x.code==='queue_wait' ? ` ${p.failed ? '' : 'Nothing is failing.'} ${M.workers ? `The shared pool (${M.workers.ready_replicas}/${M.workers.replicas} workers) serves ${plural(M.pipelines.length,'pipeline')}.` : ''} ${rec && rec.severity!=='healthy' ? esc(rec.detail||'') : ''}` : '';
  return `<div class="callout ${x.sev==='err'?'err':'warn'}" style="margin-bottom:12px">${icon(x.code==='queue_wait'?'clock':x.code==='trigger'?'bolt':'alert','ic')}<div class="grow"><b>${esc(x.title)}</b>${esc(x.detail)}${extra}</div><div class="row" style="flex:none;align-self:center">${actions}<button class="btn sm ghost" data-act="agent" data-prompt="${esc('Why does pipeline '+p.name+' ('+p.id+') show: '+x.title+'? What should I do?')}">${icon('sparkle','sm')}Ask</button></div></div>`;
}

function flowMap(p) {
  const hs = M.hs[p.src.id], hd = M.hd[p.dst.id], hm = p.model && M.hm[p.model.id];
  const st = h => !h ? '' : h.status==='healthy' ? 'ok' : h.status==='warning' ? 'warn' : 'err';
  const qw = p.qwait > X.QW_SLOW; const pol = p.resource_policy || {};
  const live = !p.paused && p.health !== 'err';
  return `<div class="flow">
   <a class="fnode ${p.src.missing?'err':''}" href="#/connections/source/${p.src.id}"><div class="k">${logo(p.src.type,'sm')}Source</div><div class="n">${esc(p.src.name)}</div><div class="m">${esc(T(p.src.type).short)} · ${esc(listenerOf(p.src))}${M.trig[p.src.id] && M.trig[p.src.id].status==='not_configured' ? ' · <span style="color:var(--warn)">no trigger</span>' : ''}</div><span class="st">${dot(st(hs))}</span></a>
   <div class="conn ${live?'live':''}"></div>
   <a class="fnode ${qw?'warn':''}" href="#/pipelines/${p.id}/capacity"><div class="k">${icon('queue','sm')}${p.processing_mode==='inline'?'Inline':'Queue'}</div><div class="n">${p.qwait!=null ? ms(p.qwait)+' wait' : p.processing_mode==='inline' ? 'Processed in place' : 'Wait not observed'}</div><div class="m">weight ${pol.weight??10} · ${pol.priority||'normal'} · ${pol.max_concurrency_per_worker??2}/worker</div><span class="st">${dot(qw?'warn':p.qwait!=null?'ok':'')}</span></a>
   <div class="conn ${live?'live':''}"></div>
   <a class="fnode" href="${p.model ? '#/models/'+p.model.id : p.recipe ? '#/models/recipes/'+encodeURIComponent(p.recipe) : '#/models'}"><div class="k">${logo(p.model?p.model.type:'recipe','sm')}Embed</div><div class="n">${esc(p.model ? (p.model.deployment||p.model.name) : (p.recipe||'—'))}</div><div class="m">${p.model ? `${p.model.embedding_dim||'—'} dims · ${esc(T(p.model.type).short)}` : 'Processing recipe'}${p.content_strategy==='chunk' ? ` · chunks ${(p.chunk_config||{}).chunk_size||''}/${(p.chunk_config||{}).chunk_overlap||0}` : ''}</div><span class="st">${dot(st(hm))}</span></a>
   <div class="conn ${live?'live':''}"></div>
   <a class="fnode ${p.dst.missing?'err':''}" href="#/connections/store/${p.dst.id}"><div class="k">${logo(p.dst.type,'sm')}Vector store</div><div class="n">${esc(p.dst.name)}</div><div class="m">${esc(storeIndex(p.dst))}</div><span class="st">${dot(st(hd))}</span></a>
  </div>`;
}

function ladder(p) {
  const h = M.hp[p.id]; const depsOk = h ? !(h.checks||[]).some(c => c.status==='fail') : null;
  const steps = [
    { t:'Saved', s:'done', d:ago(p.created_at) },
    { t:'Dependencies ready', s: depsOk===null ? 'pending' : depsOk ? 'done' : 'warn', d: depsOk===null ? 'Not checked' : depsOk ? 'All checks pass' : 'A check failed' },
    { t:'Processing', s: p.paused ? 'warn' : p.progress>=100 || p.vectors ? 'done' : 'now', d: p.paused ? 'Paused' : p.progress!=null ? `${p.progress}% of source` : p.lastWrite ? 'Syncing changes' : 'Waiting for documents' },
    { t:'Vectors written', s: p.vectors ? 'done' : 'pending', d: p.vectors ? fmt(p.vectors)+' total' : p.statsLoading ? 'Counting…' : 'None yet' },
    { t:'Retrieval verified', s: isVerified(p.id) ? 'done' : 'pending', d: isVerified(p.id) ? 'Expected answer confirmed' : `<a class="link" href="#/pipelines/${p.id}/search">Run a test query</a>` },
  ];
  return `<div class="ladder">${steps.map(s => `<div class="lg ${s.s}"><span class="b">${s.s==='done'?icon('check','sm'):s.s==='warn'?'!':s.s==='now'?icon('loader','sm spin'):''}</span><b>${s.t}</b><span>${s.d}</span></div>`).join('')}</div>`;
}

function detail(id, tab) {
  const p = M.PIPE[id];
  if (!p) return { crumbs:[['Pipelines','#/pipelines'],['Not found']], html:`<div class="page">${empty('flow','Pipeline not found',`No pipeline with ID ${esc(id)} exists. It may have been deleted.`, '<a class="btn" href="#/pipelines">Back to pipelines</a>')}</div>` };
  const tabs = [['overview','Overview'],['activity','Activity'],['search','Search'],['capacity','Capacity'],['settings','Settings']];
  const html = `<div class="page">
   <div class="ph"><div style="min-width:0"><div class="row"><h1 class="trunc">${esc(p.name)}</h1>${statusBadge(p)}</div>
     <p class="mono small">${p.id} · ${p.processing_mode||'queue'} mode · created ${ago(p.created_at)}${p.description ? ' · '+esc(p.description) : ''}</p></div>
    <div class="acts">${p.paused ? `<button class="btn pri" data-pa="resume">${icon('play')}Resume</button>` : `<button class="btn" data-pa="pause">${icon('pause')}Pause</button>`}
     <a class="btn" href="#/pipelines/${p.id}/search">${icon('search')}Test search</a><button class="btn icon" id="pmore" title="More actions">${icon('more')}</button></div></div>
   ${p.problems.slice(0,3).map(x => problemCallout(p, x)).join('')}
   ${flowMap(p)}
   <div class="tabs" style="margin-top:20px">${tabs.map(([k,l]) => `<a href="#/pipelines/${p.id}/${k}" class="${tab===k?'on':''}">${l}</a>`).join('')}</div>
   <div id="ptab">${tab==='overview' ? overview(p) : '<div class="skel" style="height:200px"></div>'}</div>
  </div>`;
  return { crumbs:[['Pipelines','#/pipelines'],[p.name]], html, mount(v) {
    $$('[data-pa]', v).forEach(b => b.onclick = () => act(p, b.dataset.pa, b.dataset.pa==='pause' ? 'Pipeline paused' : 'Pipeline resumed'));
    $('#pmore', v).onclick = e => { e.stopPropagation(); menu(e.currentTarget, [
      { label: p.processing_mode==='inline' ? 'Switch to queue mode…' : 'Switch to inline mode…', icon:'queue', run:() => switchMode(p) },
      { label:'View JSON', icon:'file', run:() => openDrawer(`<div class="drawer-h"><h3>${esc(p.name)}</h3><button class="btn ghost sm icon" data-close style="margin-left:auto">${icon('x')}</button></div><div class="drawer-b">${codebox(JSON.stringify(payloadFrom(p), null, 2))}</div>`, true) },
      { label:'Ask agent about it', icon:'sparkle', run:() => X.openAgent({ pipeline:p }) },
      '-', { label:'Reprocess everything…', icon:'rotate', run:() => resetPipeline(p) }, { label:'Delete…', icon:'trash', danger:true, run:() => deletePipeline(p) } ]); };
    $$('[data-fix]', v).forEach(b => b.onclick = () => b.dataset.fix==='alloc' ? applyAlloc(p) : runDiag(p, $('#ptab', v)));
    const t = $('#ptab', v);
    if (tab==='overview') mountOverview(p, t);
    else if (tab==='activity') activity(p, t);
    else if (tab==='search') X.searchBench(t, { destination_ids:[p.dst.id], pipeline_id:p.id, compact:true });
    else if (tab==='capacity') capacity(p, t);
    else if (tab==='settings') settings(p, t);
  } };
}

function switchMode(p) {
  const to = p.processing_mode==='inline' ? 'queue' : 'inline';
  confirmAction({ title:`Switch to ${to} mode?`, confirm:`Switch to ${to}`,
    body: to==='inline' ? 'Inline mode embeds documents in place inside the change-feed processor. It requires the source and the vector store to be the same physical container and does not support chunking.' : 'Queue mode sends work through the shared worker pool. It supports chunking and every source type, and it scales with workers.',
    onOk: async () => { await api(`/api/pipelines/${p.id}/processing-mode/${to}`, { method:'POST' }); toast(`Switched to ${to} mode`); invalidate('pipelines'); render({ force:true, silent:true }); } });
}
function applyAlloc(p) {
  const rec = p.ins && p.ins.recommendation; if (!rec) return; const cur = p.resource_policy || {}, nx = rec.suggested_policy || {};
  const rows = ['weight','max_concurrency_per_worker','priority','workload_class'].map(k => `<tr><td class="muted">${k.replace(/_/g,' ')}</td><td class="num">${esc(cur[k]??'—')}</td><td class="num"><b>${esc(nx[k]??'—')}</b></td></tr>`).join('');
  confirmAction({ title:'Apply recommended allocation?', confirm:'Apply change',
    body:`<div>${esc(rec.detail||rec.title)}</div><table class="tbl" style="margin-top:10px"><thead><tr><th>Setting</th><th>Now</th><th>Proposed</th></tr></thead><tbody>${rows}</tbody></table><div class="muted small" style="margin-top:8px">Changes the pipeline's share of the shared worker pool. Other pipelines keep their settings. Queue wait should improve within a few minutes; check the Capacity tab to confirm.</div>`,
    onOk: async () => { await api(`/api/pipelines/${p.id}/resource-policy`, { method:'PATCH', body:nx }); toast('Allocation updated'); invalidate('pipelines','insights'); render({ force:true, silent:true }); } });
}
async function runDiag(p, target) {
  target.innerHTML = `<div class="card"><div class="card-b">${spinner('Collecting evidence from dependencies, workloads, queues and counters. No changes are made.')}</div></div>`;
  target.scrollIntoView({ behavior:'smooth', block:'start' });
  try { const d = await api('/api/agent/diagnostics/pipeline', { method:'POST', body:{ pipeline_id:p.id } }); target.innerHTML = X.diagHtml(d, p); }
  catch (e) { target.innerHTML = `<div class="callout err">${icon('alert','ic')}<div><b>Diagnostics unavailable</b>${esc(e.message)}. No health conclusion can be drawn. Retry, or ask the agent.</div></div>`; }
}

function overview(p) {
  const st = p.st, h = M.hp[p.id], L = p.live || {}, obs = (p.ins||{}).observed || {};
  const cfg = p.chunk_config || {}; const srcRef = (p.sources||[])[0] || {};
  return `<div class="grid g-main" style="align-items:start"><div class="stack">
   <div class="card"><div class="card-h"><h3>Progress</h3><div class="acts muted small">${p.paused ? 'Paused' : p.progress>=100 ? 'Backfill complete · syncing changes' : p.progress!=null ? 'Backfilling' : 'Syncing changes'}</div></div>
    <div class="card-b">${ladder(p)}
     <div class="stats" style="margin-top:18px;border-top:1px solid var(--border)">
      <div class="stat"><div class="lbl">Source documents</div>${p.statsLoading ? '<div class="skel" style="height:26px;width:80px;margin-top:6px"></div>' : st.source_doc_count!=null ? `<div class="val">${fmt(st.source_doc_count)}</div>` : '<div class="val na" style="font-size:16px;margin-top:8px">Not reported</div>'}<div class="sub">${st.source_doc_count==null ? 'This source type does not report a count' : 'Counted at the source'}</div></div>
      <div class="stat"><div class="lbl">Vectors written</div><div class="val">${p.statsLoading ? '<div class="skel" style="height:26px;width:80px"></div>' : fmt(p.vectors)}</div><div class="sub">${st.embedded_count!=null && st.embedded_count!==p.vectors ? fmt(st.embedded_count)+' in current generation' : 'Lifetime'}</div></div>
      <div class="stat"><div class="lbl">Failed</div><div class="val" style="color:${p.failed?'var(--err)':'inherit'}">${fmt(p.failed)}</div><div class="sub">${(st.jobs||{}).pending ? fmt(st.jobs.pending)+' jobs pending' : 'No pending jobs'}</div></div>
      <div class="stat"><div class="lbl">Last write</div><div class="val" style="font-size:18px;margin-top:6px">${ago(p.lastWrite)}</div><div class="sub trunc" title="${esc(obs.last_document||L.last_document||'')}">${esc(obs.last_document||L.last_document||'No document recorded')}</div></div>
     </div></div></div>
   <div class="card"><div class="card-h"><h3>Health checks</h3><div class="acts muted small">${h ? 'Checked '+ago(h.checked_at) : 'Not checked yet'}</div><button class="btn sm" id="rechk">${icon('refresh','sm')}Re-check</button></div>
    <div class="card-b stack s8">${h ? (h.checks||[]).map(c => `<div class="chk-row">${chkIcon(c.status)}<div><b style="font-weight:600">${esc(X.humanCheck(c.check))}</b> <span class="muted">${esc(c.detail||'')}</span></div></div>`).join('') : '<div class="muted">Run a health check to verify source, model and vector store access for this pipeline.</div>'}</div></div>
  </div>
  <div class="stack">
   <div class="card"><div class="card-h"><h3>Configuration</h3><a class="btn ghost sm" href="#/pipelines/${p.id}/settings" style="margin-left:auto">Edit</a></div><div class="card-b"><dl class="kv">
    <dt>Content</dt><dd>${p.content_strategy==='chunk' ? `Chunks of ${cfg.chunk_size||'—'} ${cfg.chunk_unit||'chars'}, ${cfg.chunk_overlap||0} overlap` : 'Whole document (truncated to model limit)'}</dd>
    <dt>Reads</dt><dd>${srcRef.content_mode==='field' ? 'Fields: '+esc((srcRef.content_fields||[]).join(', ')||'content') : esc(srcRef.content_mode||'—')}${(srcRef.file_types||[]).length && p.src.type!=='cosmosdb' ? ' · '+esc(srcRef.file_types.join(', ')) : ''}</dd>
    <dt>Vector field</dt><dd class="mono">${esc(p.vector_index_path||'—')}</dd>
    <dt>Document ID</dt><dd class="mono">${esc(p.doc_id_pattern||'—')}${p.content_strategy==='chunk' && cfg.doc_id_pattern ? '<br>'+esc(cfg.doc_id_pattern) : ''}</dd>
    <dt>Partition key</dt><dd class="mono">${esc(p.partition_key_pattern||'—')}</dd>
    <dt>On ID collision</dt><dd>${p.collision_policy==='overwrite' ? 'Overwrite' : 'Reject'}</dd>
    <dt>Existing data</dt><dd>${p.process_existing===false ? 'New changes only' : 'Backfilled on start'}</dd>
    <dt>Updated</dt><dd>${ago(p.updated_at)}</dd></dl></div></div>
   <div class="card"><div class="card-h"><h3>Runtime (this API replica)</h3></div><div class="card-b"><dl class="kv">
    <dt>Requests</dt><dd>${fmt(L.requests ?? obs.requests)}</dd><dt>Tokens</dt><dd>${fmt(L.tokens)}</dd>
    <dt>Retries · throttles</dt><dd>${fmt(L.retries ?? obs.retries)} · ${fmt(L.throttles ?? obs.throttles)}</dd>
    <dt>Avg processing</dt><dd>${ms(obs.avg_processing_latency_ms || null)}</dd>
    <dt>Error types</dt><dd>${L.error_types && Object.keys(L.error_types).length ? Object.entries(L.error_types).map(([k,v]) => `${esc(k)} × ${v}`).join(', ') : 'None observed'}</dd></dl></div></div>
  </div></div>`;
}
function mountOverview(p, t) {
  const b = $('#rechk', t); if (b) b.onclick = async () => { b.disabled = true; b.innerHTML = spinner('Checking…');
    try { D.health = await api('/api/health/checks/run?section=pipelines', { method:'POST' }); toast('Checks complete'); invalidate('health'); render({ force:true, silent:true }); }
    catch (e) { toast(e.message, 'err'); b.disabled = false; b.textContent = 'Re-check'; } };
}

async function activity(p, t) {
  const [series, jobs] = await Promise.all([
    api(`/api/metrics/timeseries?granularity=hour&pipeline_id=${encodeURIComponent(p.id)}`).catch(e => ({ error:e.message })),
    api(`/api/jobs?pipeline_id=${encodeURIComponent(p.id)}&limit=50`).catch(e => ({ error:e.message })),
  ]);
  const b = (series && series.buckets) || []; const js = (jobs && jobs.jobs) || [];
  t.innerHTML = `<div class="stack">
   <div class="card"><div class="card-h"><h3>Processed per hour · 24h</h3><div class="acts legend"><span><i style="background:var(--accent)"></i>processed</span><span><i style="background:var(--err)"></i>failed</span><span><i style="background:var(--warn)"></i>avg latency</span></div></div>
    <div class="card-b">${series.error ? `<div class="muted">Time-series unavailable: ${esc(series.error)}</div>` : b.length ? chart(b.map(x => (x.t||'').slice(11,16)), [{vals:b.map(x=>x.processed||0),color:'var(--accent)',type:'bar'},{vals:b.map(x=>x.failed||0),color:'var(--err)',type:'bar'},{vals:b.map(x=>x.avg_latency_ms||0),color:'var(--warn)',type:'line',axis:'r',fmt:ms}], 190) : empty('chart','No activity in the last 24 hours','When documents are processed they appear here.')}</div></div>
   <div class="card"><div class="card-h"><h3>Jobs</h3><span class="muted small">${js.length ? js.length+' most recent' : ''}</span><button class="btn sm" id="pdiag" style="margin-left:auto">${icon('search','sm')}Run diagnostics</button></div>
    ${jobs.error ? `<div class="card-b muted">Jobs unavailable: ${esc(jobs.error)}</div>` : js.length ? `<table class="tbl"><thead><tr><th>Job</th><th>Status</th><th>Document</th><th>Created</th><th>Error</th><th></th></tr></thead><tbody>${js.map(j => `<tr><td class="mono small">${esc((j.id||'').slice(0,18))}</td><td><span class="badge ${j.status==='failed'?'err':j.status==='completed'?'ok':''}">${esc(j.status)}</span></td><td class="trunc" style="max-width:260px">${esc(j.source_ref||j.document_id||'—')}</td><td class="muted">${ago(j.created_at)}</td><td class="small" style="color:var(--err);max-width:260px">${esc(j.error||'')}</td><td class="r">${j.status==='failed' ? `<button class="btn sm" data-retry="${esc(j.id)}">Retry</button>` : ['pending','processing'].includes(j.status) ? `<button class="btn sm ghost" data-cancel="${esc(j.id)}">Cancel</button>` : ''}</td></tr>`).join('')}</tbody></table>`
      : `<div class="card-b muted">No jobs recorded. ${p.processing_mode==='inline' ? 'Inline pipelines process changes inside the change-feed processor and do not create jobs.' : 'Change-driven work is processed through the queue; batch jobs appear here when you run a sync.'}</div>`}</div>
   <div id="pdiagout"></div></div>`;
  $('#pdiag', t).onclick = () => runDiag(p, $('#pdiagout', t));
  t.addEventListener('click', async e => { const r = e.target.closest('[data-retry],[data-cancel]'); if (!r) return; const id = r.dataset.retry || r.dataset.cancel;
    try { await api(`/api/jobs/${encodeURIComponent(id)}/${r.dataset.retry ? 'retry' : 'cancel'}`, { method:'POST' }); toast(r.dataset.retry ? 'Job queued for retry' : 'Job cancelled'); activity(p, t); } catch (err) { toast(err.message, 'err'); } });
}

function capacity(p, t) {
  const pol = { weight:10, max_concurrency_per_worker:2, priority:'normal', workload_class:'shared', ...(p.resource_policy||{}) };
  const I = p.ins || {}, obs = I.observed || {}, rec = I.recommendation;
  t.innerHTML = `<div class="grid g-main" style="align-items:start">
   <div class="card"><div class="card-h"><h3>Share of the worker pool</h3></div><div class="card-b stack">
    ${rec ? `<div class="callout ${rec.severity==='healthy'?'ok':'warn'}">${icon(rec.severity==='healthy'?'check':'wand','ic')}<div class="grow"><b>${esc(rec.title)}</b>${esc(rec.detail||'')}</div>${rec.severity!=='healthy' ? '<button class="btn sm pri" id="useRec">Use suggestion</button>' : ''}</div>` : ''}
    <div class="grid g2">
     <div class="fld"><label>Weight <span class="muted">(1–100)</span></label><input class="inp" type="number" min="1" max="100" id="cw" value="${pol.weight}"><span class="hint">Relative share when pipelines compete. Default 10.</span></div>
     <div class="fld"><label>Max concurrent per worker <span class="muted">(1–32)</span></label><input class="inp" type="number" min="1" max="32" id="cc" value="${pol.max_concurrency_per_worker}"><span class="hint">Caps parallel documents on each worker replica.</span></div>
     <div class="fld"><label>Priority</label><select class="sel" id="cp">${['low','normal','high','critical'].map(x => `<option ${pol.priority===x?'selected':''}>${x}</option>`).join('')}</select></div>
     <div class="fld"><label>Workload class</label><select class="sel" id="ck">${[['shared','Shared pool'],['burst','Burst'],['dedicated','Dedicated']].map(([k,l]) => `<option value="${k}" ${pol.workload_class===k?'selected':''}>${l}</option>`).join('')}</select></div>
    </div>
    <div class="row sp" style="border-top:1px solid var(--border);padding-top:14px"><span class="muted small">${esc((D.insights||{}).scope_note||'')}</span><button class="btn pri" id="csave">Save allocation</button></div>
   </div></div>
   <div class="card"><div class="card-h"><h3>Observed</h3></div><div class="card-b"><dl class="kv">
    <dt>Estimated share</dt><dd>${I.estimated_share_percent!=null ? I.estimated_share_percent+'%' : '—'}</dd>
    <dt>Avg queue wait</dt><dd ${p.qwait>X.QW_SLOW?'style="color:var(--warn);font-weight:600"':''}>${ms(p.qwait)}</dd>
    <dt>Avg processing</dt><dd>${ms(obs.avg_processing_latency_ms||null)}</dd>
    <dt>Processed · failed</dt><dd>${fmt(obs.processed)} · ${fmt(obs.failed)}</dd>
    <dt>Throttles · retries</dt><dd>${fmt(obs.throttles)} · ${fmt(obs.retries)}</dd>
    <dt>Workers</dt><dd>${M.workers ? `${M.workers.ready_replicas}/${M.workers.replicas} ready${M.workers.autoscaling ? ' · autoscale to '+M.workers.autoscaling.max_replicas : ''}` : 'Not observed'} · <a class="link" href="#/platform">Platform</a></dd></dl></div></div></div>`;
  const u = $('#useRec', t); if (u) u.onclick = () => { const s = rec.suggested_policy||{}; $('#cw',t).value = s.weight; $('#cc',t).value = s.max_concurrency_per_worker; $('#cp',t).value = s.priority; $('#ck',t).value = s.workload_class; };
  $('#csave', t).onclick = async e => { const b = e.currentTarget; const body = { weight:+$('#cw',t).value, max_concurrency_per_worker:+$('#cc',t).value, priority:$('#cp',t).value, workload_class:$('#ck',t).value };
    b.disabled = true; try { await api(`/api/pipelines/${p.id}/resource-policy`, { method:'PATCH', body }); toast('Allocation saved'); invalidate('pipelines','insights'); render({ force:true, silent:true }); } catch (err) { toast(err.message, 'err'); b.disabled = false; } };
}

function settings(p, t) {
  const cfg = p.chunk_config || {}; const meta = p.metadata_fields || [];
  t.innerHTML = `<div class="grid g-main" style="align-items:start"><div class="card"><div class="card-b stack">
    <div class="grid g2"><div class="fld"><label>Name</label><input class="inp" id="sn" value="${esc(p.name)}"></div><div class="fld"><label>Description</label><input class="inp" id="sd" value="${esc(p.description||'')}"></div></div>
    ${p.content_strategy==='chunk' ? `<div class="grid g3"><div class="fld"><label>Chunk size</label><input class="inp" type="number" id="scs" value="${cfg.chunk_size||1000}"></div><div class="fld"><label>Overlap</label><input class="inp" type="number" id="sco" value="${cfg.chunk_overlap||0}"></div><div class="fld"><label>Unit</label><select class="sel" id="scu"><option value="chars" ${cfg.chunk_unit!=='tokens'?'selected':''}>Characters</option><option value="tokens" ${cfg.chunk_unit==='tokens'?'selected':''}>Words (approx. tokens)</option></select></div></div>` : ''}
    <div class="grid g2"><div class="fld"><label>On document ID collision</label><select class="sel" id="scol"><option value="reject" ${p.collision_policy!=='overwrite'?'selected':''}>Reject (safer)</option><option value="overwrite" ${p.collision_policy==='overwrite'?'selected':''}>Overwrite</option></select></div>
     <div class="fld"><label>Extra metadata on each vector</label><div class="row wrap">${['pipeline_name','embedding_dims','source_ref'].map(f => `<label class="check"><input type="checkbox" value="${f}" class="smeta" ${meta.includes(f)?'checked':''}> ${f}</label>`).join('')}</div></div></div>
    <div class="callout">${icon('info','ic')}<div>Source, model, vector store and content strategy define what is in the index, so they cannot be changed in place. To change them, create a new pipeline and delete this one when it's verified.</div></div>
    <div class="inline-err" id="serr"></div>
    <div class="row sp" style="border-top:1px solid var(--border);padding-top:14px"><button class="btn danger" id="sdel">${icon('trash','sm')}Delete pipeline</button><button class="btn pri" id="ssave">Save changes</button></div>
   </div></div>
   <div class="card"><div class="card-h"><h3>Definition</h3></div><div class="card-b">${codebox(JSON.stringify(payloadFrom(p), null, 2))}</div></div></div>`;
  $('#sdel', t).onclick = () => deletePipeline(p);
  $('#ssave', t).onclick = async e => { const b = e.currentTarget; $('#serr', t).textContent = '';
    const patch = { name:$('#sn',t).value.trim(), description:$('#sd',t).value, collision_policy:$('#scol',t).value, metadata_fields:$$('.smeta',t).filter(x=>x.checked).map(x=>x.value) };
    if (!patch.name) { $('#serr', t).textContent = 'Name is required.'; return; }
    if (p.content_strategy==='chunk') patch.chunk_config = { ...cfg, chunk_size:+$('#scs',t).value, chunk_overlap:+$('#sco',t).value, chunk_unit:$('#scu',t).value };
    b.disabled = true; b.innerHTML = spinner('Saving…');
    try { await api(`/api/pipelines/${p.id}`, { method:'PUT', body:payloadFrom(p, patch) }); toast('Pipeline saved'); invalidate('pipelines'); render({ force:true, silent:true }); }
    catch (err) { $('#serr', t).textContent = err.message; b.disabled = false; b.textContent = 'Save changes'; } };
}
})();
