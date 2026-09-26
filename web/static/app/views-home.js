/* OmniVec console: Home */
(() => {
const { icon, esc, fmt, ms, ago, plural, D, M, T, logo, statusBadge, chipFlow, spark, chart, empty, loadErr, route, session } = window.OVX;

function greeting() { const h = new Date().getHours(); return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'; }

function ring(ok, warn, err, paused, total) {
  const C = 2*Math.PI*27, seg = (n) => total ? C*n/total : 0; let off = 0;
  const arc = (n, col) => { if (!n) return ''; const len = Math.max(seg(n)-2, 1); const s = `<circle cx="32" cy="32" r="27" fill="none" stroke="${col}" stroke-width="7" stroke-dasharray="${len} ${C}" stroke-dashoffset="${-off}" stroke-linecap="round" transform="rotate(-90 32 32)"/>`; off += seg(n); return s; };
  return `<svg class="health-ring" viewBox="0 0 64 64"><circle cx="32" cy="32" r="27" fill="none" stroke="var(--subtle)" stroke-width="7"/>${arc(ok,'var(--ok)')}${arc(warn,'var(--warn)')}${arc(err,'var(--err)')}${arc(paused,'var(--border-strong)')}<text x="32" y="37" text-anchor="middle" font-size="15" font-weight="700" fill="var(--text)">${total}</text></svg>`;
}

function firstRun() {
  const steps = [
    { done: M.sources.length > 0, t:'Connect a source', d:'Cosmos DB, Blob Storage, SharePoint, PostgreSQL or OneLake. We will check permissions with you.', h:'#/connections/new/source', ic:'plug' },
    { done: M.dests.length > 0, t:'Add a vector store', d:'Where embeddings are written and searched.', h:'#/connections/new/store', ic:'db' },
    { done: M.models.some(m => m.model_category !== 'chat'), t:'Register an embedding model', d:'Azure OpenAI with managed identity is recommended.', h:'#/models/new', ic:'cube' },
    { done: M.pipelines.length > 0, t:'Create your first pipeline', d:'Bind a source, an embedding model and a vector store.', h:'#/new', ic:'flow' },
    { done: false, t:'Verify retrieval', d:'Run a query and confirm the expected answer comes back.', h:'#/search', ic:'search' },
  ];
  const next = steps.findIndex(s => !s.done);
  return `<div class="card"><div class="card-h"><h3>Get to your first searchable index</h3><span class="badge acc">${steps.filter(s=>s.done).length} of ${steps.length}</span></div>
   <div class="card-b stack s8">${steps.map((s,i) => `<a class="opt ${i===next?'on':''}" href="${s.h}" style="padding:12px">
     <span class="logo" style="background:${s.done?'var(--ok)':'var(--subtle)'};color:${s.done?'#fff':'var(--muted)'}">${s.done ? icon('check','sm') : i+1}</span>
     <div class="grow"><div class="t">${s.t}</div><div class="d">${s.d}</div></div>${i===next ? `<span class="btn pri sm">Start${icon('chev','sm')}</span>` : ''}</a>`).join('')}</div></div>`;
}

route('home', { needs:[...window.OVX.CORE, 'series'], render() {
  const ps = M.pipelines, total = ps.length;
  const n = k => ps.filter(p => p.health === k).length;
  const ok = n('ok') + n('unknown'), warn = n('warn'), err = n('err'), paused = n('paused');
  const issues = M.issues.filter(i => i.sev !== 'info');
  const L = D.live || {}, S = D.series, W = M.workers;
  const worstQ = Math.max(0, ...ps.map(p => p.qwait || 0));
  const verdict = !total ? { cls:'out', label:'Not set up', h:'No pipelines yet', d:'Connect a source and a vector store to create your first pipeline.' }
    : err ? { cls:'err', label:`${plural(err,'pipeline')} failing`, h:`${plural(err,'pipeline')} need${err===1?'s':''} a fix.`, d:'Documents are failing or a dependency is broken. Start with the issue at the top of the list.' }
    : warn ? { cls:'warn', label:`Working · ${plural(issues.length,'issue')}`, h: ps.filter(p=>p.problems.some(x=>x.code==='queue_wait')).length ? `Everything is processing. ${plural(ps.filter(p=>p.problems.some(x=>x.code==='queue_wait')).length,'pipeline')} ${ps.filter(p=>p.problems.some(x=>x.code==='queue_wait')).length===1?'is':'are'} slow.` : `Everything is processing. ${plural(warn,'pipeline')} need${warn===1?'s':''} attention.`,
        d:`${L.documents_failed ? fmt(L.documents_failed)+' failed documents observed.' : 'No failed documents observed.'} ${worstQ > window.OVX.QW_SLOW ? `Work waits up to <b class="t2">${ms(worstQ)}</b> before a worker picks it up.` : ''} ${M.issues.some(i=>i.kind==='trigger') ? 'A Blob source has no trigger.' : ''}` }
    : { cls:'ok', label:'Healthy', h:'All pipelines are healthy.', d:'Sources, models and vector stores passed their latest checks.' };
  const buckets = (S && S.buckets) || [];
  const attention = ps.filter(p => p.problems.length).sort((a,b) => (a.health==='err'?0:1)-(b.health==='err'?0:1) || (b.qwait||0)-(a.qwait||0));
  const embLat = L.embedding_latency || {};
  const hist = buckets.map(b => b.processed||0);
  const html = `<div class="page">
   <div class="ph"><div><div class="eyebrow">${new Date().toLocaleDateString(undefined,{weekday:'long',day:'numeric',month:'long'})}</div><h1>${greeting()}${session.user && session.user.name && session.user.name !== 'admin' ? ', '+esc(session.user.name) : ''}</h1></div>
     <div class="acts"><button class="btn" data-act="reload">${icon('refresh')}Refresh</button></div></div>
   ${loadErr(['pipelines','health','insights','live'])}
   ${!M.sources.length || !M.pipelines.length ? firstRun() : ''}
   ${total ? `<div class="card hero" ${!M.pipelines.length?'style="margin-top:16px"':''}>
    <div class="l"><div class="row" style="gap:16px;align-items:flex-start">${ring(ok,warn,err,paused,total)}
      <div><span class="badge ${verdict.cls}">${window.OVX.dot(verdict.cls)}${esc(verdict.label)}</span><h2>${esc(verdict.h)}</h2><div class="muted">${verdict.d}</div></div></div>
      <div class="row" style="margin-top:16px">${issues.length ? `<a class="btn pri" href="#/issues">${icon('alert')}Review ${plural(issues.length,'issue')}</a>` : `<a class="btn pri" href="#/search">${icon('search')}Test retrieval</a>`}
        <button class="btn" data-act="agent" data-prompt="${esc(issues.length ? 'Summarize the current issues and what I should fix first.' : 'Is anything at risk in my pipelines right now?')}"><span class="agent-orb" style="width:18px;height:18px;border-radius:6px"></span>${issues.length ? 'Ask agent what to fix first' : 'Ask agent for a health summary'}</button></div>
      <div class="row small muted" style="margin-top:14px;gap:14px"><span>${window.OVX.dot('ok')} ${ok} healthy</span><span>${window.OVX.dot('warn')} ${warn} attention</span><span>${window.OVX.dot('err')} ${err} failing</span><span>${window.OVX.dot()} ${paused} paused</span></div></div>
    <div class="r"><div class="eyebrow">Your data path</div>
      <div class="lc">
        <a href="#/connections/sources"><div class="k">${icon('plug','sm')}Connect</div><div class="v">${M.sources.length} <span class="muted" style="font-size:12px;font-weight:500">sources</span></div></a>
        <a href="#/pipelines"><div class="k">${icon('flow','sm')}Process</div><div class="v">${total} <span class="muted" style="font-size:12px;font-weight:500">pipelines</span></div></a>
        <a href="#/connections/stores"><div class="k">${icon('db','sm')}Store</div><div class="v">${M.dests.length} <span class="muted" style="font-size:12px;font-weight:500">stores</span></div></a>
        <a href="#/models"><div class="k">${icon('cube','sm')}Embed</div><div class="v">${M.models.length} <span class="muted" style="font-size:12px;font-weight:500">models</span></div></a>
      </div>
      <div class="row muted" style="margin-top:10px;font-size:11.5px">${icon('info','sm')}${M.models[0] ? `${esc(M.models[0].deployment || M.models[0].name)} · ${M.models[0].embedding_dim||'—'} dims · ${esc(T(M.models[0].type).short)}` : 'No embedding model registered yet'}</div></div>
   </div>` : ''}
   <div class="card stats" style="margin-top:16px">
    <div class="stat"><div class="lbl">Processed · 24h</div><div class="val">${S ? fmt(hist.reduce((a,b)=>a+b,0)) : '<span class="na">Not reported</span>'}</div><div class="row sp"><span class="sub">${S ? 'From pipeline telemetry' : 'Time-series unavailable'}</span>${spark(hist)}</div></div>
    <div class="stat"><div class="lbl">Failed · since API start</div><div class="val" style="color:${L.documents_failed ? 'var(--err)' : 'var(--ok)'}">${L.documents_failed==null ? '—' : fmt(L.documents_failed)}</div><div class="sub">${L.uptime_seconds ? 'API up '+Math.round(L.uptime_seconds/3600)+'h · '+fmt(L.documents_embedded)+' embedded' : 'Runtime metrics unavailable'}</div></div>
    <div class="stat"><div class="lbl">Embedding latency</div>${embLat.count ? `<div class="val">${ms(embLat.avg)}<small>avg</small></div><div class="sub">p95 ${ms(embLat.p95)} · ${fmt(embLat.count)} samples</div>` : `<div class="val na" style="font-size:16px;margin-top:8px">Not recorded</div><div class="sub">No latency samples on this API replica</div>`}</div>
    <div class="stat"><div class="lbl">Workers</div>${W ? `<div class="val">${W.ready_replicas??'—'}<small>/ ${W.replicas} ready</small></div><div class="row sp"><span class="sub">${W.autoscaling ? 'Autoscale '+W.autoscaling.min_replicas+'–'+W.autoscaling.max_replicas : 'Fixed replicas'} · queue ${fmt((D.triggers||{}).event_queue_size ?? null)}</span><span class="pod">${Array.from({length:Math.min(10,(W.autoscaling&&W.autoscaling.max_replicas)||W.replicas||0)},(_,i)=>`<i class="${i < (W.ready_replicas||0) ? '' : 'off'}"></i>`).join('')}</span></div>` : '<div class="val na" style="font-size:16px;margin-top:8px">Not observed</div>'}</div>
   </div>
   <div class="grid g-main" style="margin-top:16px;align-items:start">
    <div class="card"><div class="card-h"><h3>Needs attention</h3>${attention.length ? `<span class="badge warn">${attention.length}</span>` : ''}<div class="acts"><a class="btn ghost sm" href="#/pipelines">All pipelines${icon('chev','sm')}</a></div></div>
     ${attention.length ? `<table class="tbl hide-c2"><thead><tr><th>Pipeline</th><th>Flow</th><th>Signal</th><th class="r">Vectors</th><th>Last write</th><th></th></tr></thead><tbody>
      ${attention.slice(0,7).map(p => `<tr class="clickrow" data-href="#/pipelines/${p.id}"><td><div class="nm trunc" style="max-width:240px">${esc(p.name)}</div><div class="id">${p.id}</div></td><td>${chipFlow(p)}</td>
        <td><span class="badge ${p.problems[0].sev==='err'?'err':'warn'}">${esc(p.problems[0].title)}</span>${p.problems.length>1?` <span class="muted small">+${p.problems.length-1}</span>`:''}</td><td class="r num">${fmt(p.vectors)}</td><td class="muted">${ago(p.lastWrite)}</td>
        <td class="r"><a class="btn sm" href="#/pipelines/${p.id}">Fix</a></td></tr>`).join('')}
      ${attention.length > 7 ? `<tr><td colspan="6" class="muted small"><a class="link" href="#/pipelines?f=attention">+ ${attention.length-7} more</a></td></tr>` : ''}</tbody></table>`
      : empty('check', 'Nothing needs you right now', total ? 'Every pipeline passed its latest checks.' : 'Pipelines that are slow, failing or missing a trigger will show up here.')}
    </div>
    <div class="stack">
      <div class="card"><div class="card-h"><h3>Throughput · 24h</h3><div class="acts legend"><span><i style="background:var(--accent)"></i>processed</span><span><i style="background:var(--warn)"></i>avg latency</span></div></div>
        <div class="card-b">${buckets.length ? chart(buckets.map(b => (b.t||'').slice(11,16)), [{vals:hist,color:'var(--accent)',type:'bar'},{vals:buckets.map(b=>b.avg_latency_ms||0),color:'var(--warn)',type:'line',axis:'r',fmt:ms}], 170) : empty('chart','No activity in the last 24 hours','Processed documents will chart here.')}</div></div>
      <div class="card"><div class="card-h"><h3>Quick start</h3></div><div class="card-b stack s8">
        <a class="opt" style="padding:10px" href="#/new"><span class="logo l-vec">${icon('plus','sm')}</span><div><div class="t">New pipeline</div><div class="d">Reuse any of ${plural(M.sources.length,'source')} and ${plural(M.dests.length,'store')}</div></div></a>
        <a class="opt" style="padding:10px" href="#/search"><span class="logo l-aoai">${icon('search','sm')}</span><div><div class="t">Test retrieval</div><div class="d">Query a vector store and see where each answer came from</div></div></a>
        <a class="opt" style="padding:10px" href="#/connections/new/source"><span class="logo l-cosmos">${icon('shield','sm')}</span><div><div class="t">Connect a source</div><div class="d">Guided setup with a permissions check</div></div></a>
      </div></div>
    </div>
   </div></div>`;
  return { crumbs:[['Home']], html, mount(v) { v.querySelectorAll('[data-href]').forEach(r => r.addEventListener('click', e => { if (!e.target.closest('a,button')) location.hash = r.dataset.href; })); } };
}});
})();
