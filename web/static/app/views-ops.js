/* OmniVec console: Search bench, Issues, Metrics, Capacity, Platform, diagnostics renderer */
(() => {
const X = window.OVX;
const { icon, esc, fmt, ms, ago, when, plural, api, need, invalidate, D, M, T, logo, dot, statusBadge, chipFlow, chart, empty, codebox, loadErr, spinner, route, go, toast, confirmAction, openModal, closeOverlay, $, $$ } = X;
const sevBadge = s => `<span class="badge ${s==='err'?'err':s==='warn'?'warn':'acc'}">${dot(s)}${s==='err'?'Failing':s==='warn'?'Attention':'Info'}</span>`;
const hl = (t, q) => { let h = esc(t); (q||'').split(/\s+/).filter(w => w.length > 3).slice(0,6).forEach(w => { h = h.replace(new RegExp('('+w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','gi'), '<mark>$1</mark>'); }); return h; };

/* ---------- diagnostics ---------- */
X.diagHtml = (d, p) => {
  const f = d.findings || [], plan = d.repair_plan || [];
  return `<div class="stack s12">${f.length ? f.map(x => `<div class="callout ${/ok|healthy/i.test(x.code)?'info':'warn'}">${icon(/ok|healthy/i.test(x.code)?'check':'alert','ic')}<div class="grow"><b>${esc(String(x.code||'finding').replace(/_/g,' '))}</b>
      <div class="t2">${esc(typeof x.evidence==='string' ? x.evidence : JSON.stringify(x.evidence||''))}</div>${x.next_action ? `<div class="small" style="margin-top:4px"><b>Next:</b> ${esc(x.next_action)}</div>` : ''}</div></div>`).join('')
    : `<div class="callout info">${icon('check','ic')}<div>No problems found${p ? ' for '+esc(p.name) : ''}.</div></div>`}
   ${plan.length ? `<div class="eyebrow">Repair plan</div><ol class="stack s8" style="margin:0;padding-left:18px">${plan.map(r => `<li><b>${esc(String(r.code||'').replace(/_/g,' '))}</b> <span class="t2">${esc(r.instructions||'')}</span>${r.verification ? `<div class="muted small">Verify: ${esc(r.verification)}</div>` : ''}</li>`).join('')}</ol>
     <button class="btn sm" data-act="agent" data-prompt="${esc('Apply the repair plan'+(p?' for pipeline '+p.name:'')+'. Ask me before each change.')}">${icon('sparkle','sm')}Apply with the agent (asks before each change)</button>` : ''}
   ${(d.unknown||[]).length || (d.limitations||[]).length ? `<details class="small"><summary class="link">What diagnostics couldn't verify</summary><ul>${[...(d.unknown||[]),...(d.limitations||[])].map(u => `<li class="muted">${esc(typeof u==='string'?u:JSON.stringify(u))}</li>`).join('')}</ul></details>` : ''}</div>`;
};

/* ---------- search bench ---------- */
X.searchBench = (el, opts = {}) => {
  const S = { q:'', stores:opts.destination_ids || (M.dests[0] ? [M.dests[0].id] : []), k:5, mode:'vector', res:null, busy:false, sel:null };
  const pid = opts.pipeline_id;
  const storePicker = host => {
    if ($('.msel-pop', host)) { $('.msel-pop', host).remove(); return; }
    const pop = document.createElement('div'); pop.className = 'msel-pop'; host.append(pop);
    const dims = d => { const vi = ((d.config||{}).vector_indexes||[])[0]; return (vi && vi.dimensions) || (d.config||{}).vector_dimensions || null; };
    const sync = () => { const b = $('#mselb', el); if (b) $('.trunc', b).innerHTML = S.stores.length ? esc((M.DST[S.stores[0]]||{}).name||S.stores[0]) + (S.stores.length > 1 ? ` <span class="badge acc">+${S.stores.length-1}</span>` : '') : '<span class="muted">Choose vector stores</span>'; };
    const paint = () => { const q = ($('input', pop) || {}).value || '';
      const list = M.dests.filter(d => !q || (d.name+' '+d.type).toLowerCase().includes(q.toLowerCase()));
      const selDims = [...new Set(S.stores.map(id => M.DST[id]).filter(Boolean).map(dims).filter(Boolean))];
      $('.msel-list', pop).innerHTML = list.slice(0, 200).map(d => { const on = S.stores.includes(d.id), n = X.usedBy(d.id).length, dm = dims(d);
        return `<label class="msel-it ${on?'on':''}"><input type="checkbox" data-sid="${d.id}" ${on?'checked':''}>${logo(d.type,'sm')}<div class="grow" style="min-width:0"><div class="trunc" style="font-weight:550">${esc(d.name)}</div><div class="muted small">${esc(X.T(d.type).short)}${dm ? ' · '+dm+'d' : ''} · ${plural(n,'pipeline')}</div></div></label>`; }).join('')
        || '<div class="muted small" style="padding:12px">No stores match.</div>';
      $('.msel-foot span', pop).innerHTML = `${S.stores.length} selected${selDims.length > 1 ? ` · <span style="color:var(--warn)">mixed dimensions (${selDims.join(', ')})</span>` : ''}`;
      $$('[data-sid]', pop).forEach(cb => cb.onchange = () => { const i = S.stores.indexOf(cb.dataset.sid); cb.checked ? (i<0 && S.stores.push(cb.dataset.sid)) : (i>=0 && S.stores.splice(i,1)); paint(); sync(); }); };
    pop.innerHTML = `<div style="padding:8px"><div class="search-inp">${icon('search','sm')}<input class="inp" placeholder="Search ${M.dests.length} stores"></div></div><div class="msel-list"></div>
      <div class="msel-foot"><span></span><div class="row" style="gap:6px"><button class="btn ghost sm" data-clr>Clear</button><button class="btn sm pri" data-done>Done</button></div></div>`;
    $('input', pop).oninput = paint; $('[data-clr]', pop).onclick = () => { S.stores.length = 0; paint(); sync(); }; $('[data-done]', pop).onclick = () => pop.remove();
    pop.onclick = e => e.stopPropagation(); setTimeout(() => document.addEventListener('click', () => pop.remove(), { once:true }), 0);
    paint(); $('input', pop).focus();
  };  const draw = () => {
    el.innerHTML = `<div class="stack">
     <div class="card"><div class="card-b stack s12"><div class="row" style="gap:8px"><div class="search-inp grow">${icon('search','sm')}<input class="inp" id="sq" placeholder="Ask a question the way a user of your AI app would…" value="${esc(S.q)}"></div>
       <button class="btn pri" id="sgo" ${S.busy?'disabled':''}>${S.busy ? spinner('Searching…') : 'Search'}</button></div>
      <div class="row wrap" style="gap:14px">${opts.compact ? '' : `<div class="msel" id="msel"><button class="sel msel-btn" id="mselb" type="button">${icon('db','sm')}<span class="trunc">${S.stores.length ? esc((M.DST[S.stores[0]]||{}).name||S.stores[0]) + (S.stores.length > 1 ? ` <span class="badge acc">+${S.stores.length-1}</span>` : '') : '<span class="muted">Choose vector stores</span>'}</span></button></div>`}
       <div class="seg" id="smode">${[['vector','Vector'],['hybrid','Hybrid'],['fts','Keyword']].map(([k,l]) => `<button data-m="${k}" class="${S.mode===k?'on':''}">${l}</button>`).join('')}</div>
       <label class="row small muted" style="gap:6px">Top <select class="sel" id="sk" style="width:64px;height:28px">${[3,5,10,20].map(n => `<option ${S.k===n?'selected':''}>${n}</option>`).join('')}</select></label></div></div></div>
     <div id="sout">${out()}</div></div>`;
    const q = $('#sq', el); q.onkeydown = e => { if (e.key==='Enter') run(); }; q.oninput = () => S.q = q.value;
    $('#sgo', el).onclick = run; $('#sk', el).onchange = e => S.k = +e.target.value;
    $$('[data-m]', el).forEach(b => b.onclick = () => { S.mode = b.dataset.m; draw(); });
    const mb = $('#mselb', el); if (mb) mb.onclick = e => { e.stopPropagation(); storePicker($('#msel', el)); };
    $$('[data-res]', el).forEach(r => r.onclick = e => { if (e.target.closest('button')) return; S.sel = +r.dataset.res; draw(); });
    $$('[data-good]', el).forEach(b => b.onclick = () => { const r = S.res.results[+b.dataset.good]; const p = (r.metadata||{}).pipeline_id || pid;
      if (p) localStorage.setItem('ov-verified-'+p, JSON.stringify({ q:S.q, at:Date.now() })); toast(p ? 'Marked as expected. Retrieval is verified for this pipeline.' : 'Noted'); b.outerHTML = `<span class="badge ok">${icon('check','sm')}Expected</span>`; });
    if (!opts.compact) setTimeout(() => q.focus(), 0);
  };
  const out = () => {
    if (!S.res) return `<div class="card">${empty('search','Test what your AI app will retrieve', 'Run a real question against one or more vector stores. Results show score, source document and the pipeline that wrote them. Mark a good answer as expected to verify the pipeline.')}</div>`;
    if (S.res.error) return `<div class="callout warn">${icon('alert','ic')}<div class="grow"><b>Search failed</b><div>${esc(S.res.error)}</div></div><button class="btn sm" data-act="agent" data-prompt="${esc('Search failed with: '+S.res.error+'. Why?')}">${icon('sparkle','sm')}Ask why</button></div>`;
    const rs = S.res.results || [], mx = Math.max(...rs.map(r => r.score||r.rrf_score||0), 1e-9), sel = rs[S.sel];
    const meta = (S.res.indexes_searched||[]).map(i => `${esc(i.index_name||i.index_id)} · ${i.result_count} hits · ${ms(i.search_time_ms)}${i.error ? ' · <span style="color:var(--err)">'+esc(i.error)+'</span>' : ''}`).join(' &nbsp;|&nbsp; ');
    return `${(S.res.warnings||[]).map(w => `<div class="callout warn" style="margin-bottom:10px">${icon('alert','ic')}<div>${esc(w)}</div></div>`).join('')}
     <div class="grid ${sel && !opts.compact ? 'g-main' : ''}" style="align-items:start"><div class="card"><div class="card-h"><h3>${plural(rs.length,'result')}</h3><span class="muted small" style="margin-left:auto">${ms(S.res.total_search_time_ms)} total</span></div>
      <div class="muted small" style="padding:8px 16px;border-bottom:1px solid var(--border)">${meta}</div>
      ${rs.length ? rs.map((r, i) => { const sc = r.score ?? r.rrf_score ?? 0; const pp = M.PIPE[(r.metadata||{}).pipeline_id];
        return `<div class="result clickrow ${S.sel===i?'on':''}" data-res="${i}"><div class="row" style="gap:10px"><span class="rk">${i+1}</span><b class="trunc grow">${esc(r.source_ref || r.id)}</b>
          <span class="scorebar"><i style="width:${Math.max(4, 100*sc/mx)}%"></i></span><span class="mono small muted">${sc.toFixed ? sc.toFixed(4) : esc(sc)}</span>
          <button class="btn ghost sm" data-good="${i}" title="This is a good answer">${icon('check','sm')}Expected</button></div>
         <div class="snip">${r.text ? hl(String(r.text).slice(0, 420), S.q) : '<span class="muted">No text stored with this vector. Enable “store chunk text” on the pipeline to see snippets.</span>'}</div>
         <div class="row small muted" style="margin-left:34px;gap:12px">${pp ? `<a class="link" href="#/pipelines/${pp.id}">${icon('flow','sm')}${esc(pp.name)}</a>` : ''}<span>${esc(r.index_name||'')}</span></div></div>`; }).join('')
       : empty('search','No matches', 'Nothing came back. Check that the pipeline has written vectors, that the model matches the store, or try Hybrid mode.')}</div>
      ${sel && !opts.compact ? `<div class="card" style="position:sticky;top:12px"><div class="card-h"><h3>Result ${S.sel+1}</h3></div><div class="card-b stack s12"><dl class="kv"><dt>Document</dt><dd class="mono small">${esc(sel.id)}</dd><dt>Source ref</dt><dd class="small">${esc(sel.source_ref||'—')}</dd><dt>Score</dt><dd>${esc(sel.score ?? '—')}${sel.rrf_score!=null ? ' · RRF '+esc(sel.rrf_score) : ''}</dd>
        ${Object.entries(sel.metadata||{}).map(([k,v]) => `<dt>${esc(k)}</dt><dd class="small">${esc(typeof v==='object'?JSON.stringify(v):v)}</dd>`).join('')}</dl>
        ${sel.text ? `<div class="codebox"><div class="code" style="white-space:pre-wrap;max-height:320px;overflow:auto">${esc(sel.text)}</div></div>` : ''}</div></div>` : ''}</div>`;
  };
  const run = async () => { S.q = $('#sq', el).value.trim(); if (!S.q || !S.stores.length) { toast(S.q ? 'Choose at least one store' : 'Type a question', 'info'); return; }
    S.busy = true; S.sel = null; draw();
    try { S.res = await api('/api/playground/search', { method:'POST', body:{ query:S.q, destination_ids:S.stores, top_k:S.k, search_mode:S.mode, ...(pid ? { pipeline_id:pid } : {}) } }); }
    catch (e) { S.res = { error:e.message }; }
    S.busy = false; draw(); };
  draw();
};
route('search', { live:false, render() {
  const st = X.query().get('store');
  return { crumbs:[['Search']], html:`<div class="page"><div class="ph"><div><h1>Search</h1><p>Check retrieval quality before connecting an AI app. Results come from the same search service that published apps use.</p></div></div><div id="bench"></div></div>`,
    mount(v) { X.searchBench($('#bench', v), st ? { destination_ids:[st] } : {}); } };
}});

/* ---------- issues ---------- */
async function applyRecs(recs) {
  const rows = recs.map(p => { const c = p.resource_policy || {}, n = p.ins.recommendation.suggested_policy || {};
    return `<tr><td>${esc(p.name)}</td><td class="mono small">${c.weight??10} → <b>${n.weight}</b></td><td class="mono small">${c.max_concurrency_per_worker??2} → <b>${n.max_concurrency_per_worker}</b></td><td class="small">${esc(c.priority||'normal')} → <b>${esc(n.priority)}</b></td></tr>`; }).join('');
  confirmAction({ title:'Apply recommended allocation', confirm:`Apply to ${plural(recs.length,'pipeline')}`,
    body:`<div>Gives these pipelines a larger share of the shared worker pool. No data is reprocessed. You can revert on each pipeline's Capacity tab.</div><table class="tbl" style="margin-top:10px"><thead><tr><th>Pipeline</th><th>Weight</th><th>Concurrency</th><th>Priority</th></tr></thead><tbody>${rows}</tbody></table>`,
    onOk: async () => { for (const p of recs) await api(`/api/pipelines/${p.id}/resource-policy`, { method:'PATCH', body:p.ins.recommendation.suggested_policy });
      toast('Allocation updated. Queue wait should drop within a few minutes.'); invalidate('pipelines','insights'); X.render({ force:true, silent:true }); } });
}
X.applyRecs = applyRecs;
function fixFor(i) {
  const aff = (i.affected||[]).map(id => M.PIPE[id]).filter(Boolean);
  if (i.kind==='capacity') return `<div class="stack s12"><div class="t2">Each pipeline gets a weighted share of the shared worker pool. The pool is busy, so OmniVec recommends raising the share for the slow pipelines. Adding workers is the alternative if every pipeline is slow.</div>
    ${(i.recs||[]).length ? `<button class="btn pri" id="fixrec">${icon('wand','sm')}Apply recommended allocation (${plural(i.recs.length,'pipeline')})</button>` : '<div class="muted">No per-pipeline recommendation available.</div>'}
    <a class="btn" href="#/platform">${icon('server','sm')}Review worker capacity</a></div>`;
  if (i.kind==='trigger') return `<div class="stack s12"><div class="t2">Create an Event Grid subscription so new blobs start ingestion within seconds. Until then, use Sync now to pick up new files manually.</div><a class="btn pri" href="#/connections/source/${i.sourceId}/trigger">${icon('bolt','sm')}Set up the trigger</a></div>`;
  if (i.kind==='failures') return `<div class="stack s12"><div class="t2">Open a pipeline's Activity tab to see failure types and retry failed jobs after fixing the cause.</div>${aff.slice(0,4).map(p => `<a class="btn" href="#/pipelines/${p.id}/activity">${icon('flow','sm')}${esc(p.name)}</a>`).join('')}</div>`;
  if (i.kind==='check') return `<div class="stack s12">${(i.checks||[]).filter(c => c.status!=='pass').map(c => `<div class="chk-row">${X.chkIcon ? X.chkIcon(c.status) : ''}<div><b>${esc(X.humanCheck(c.check))}</b> <span class="muted">${esc(c.detail||'')}</span></div></div>`).join('')}
    ${i.link ? `<a class="btn pri" href="${i.link}${/connections/.test(i.link) ? '/access' : ''}">${icon(/connections/.test(i.link)?'shield':'chev','sm')}${/connections/.test(i.link) ? 'Open the permissions assistant' : 'Open'}</a>` : ''}</div>`;
  if (i.kind==='workload') return `<a class="btn pri" href="#/platform">${icon('server','sm')}Open platform</a>`;
  return '<div class="muted">No automatic fix. This is informational.</div>';
}
route('issues', { render(parts) {
  if (parts[0]==='run') return runPage();
  const i = parts[0] && M.issues.find(x => x.id===parts[0]);
  if (parts[0] && !i) return { crumbs:[['Issues','#/issues'],['Resolved']], html:`<div class="page">${empty('check','This issue is resolved', 'It no longer appears in the latest checks.', '<a class="btn" href="#/issues">All issues</a>')}</div>` };
  if (i) { const aff = (i.affected||[]).map(id => M.PIPE[id]).filter(Boolean);
    return { crumbs:[['Issues','#/issues'],[i.title]], html:`<div class="page"><div class="ph"><div><div class="row" style="gap:8px">${sevBadge(i.sev)}<span class="muted small">${esc(i.area)}</span></div><h1 style="margin-top:6px">${esc(i.title)}</h1><p>${esc(i.detail)}</p></div>
      <div class="acts"><button class="btn" data-act="agent" data-prompt="${esc('Investigate: '+i.title+'. '+i.detail)}">${icon('sparkle','sm')}Investigate with agent</button></div></div>
     <div class="grid g-main" style="align-items:start"><div class="stack">
      <div class="card"><div class="card-h"><h3>${icon('wand','sm')} Recommended fix</h3></div><div class="card-b">${fixFor(i)}</div></div>
      ${aff.length ? `<div class="card"><div class="card-h"><h3>Affected pipelines</h3></div><table class="tbl"><tbody>${aff.map(p => `<tr class="clickrow" onclick="location.hash='#/pipelines/${p.id}'"><td><b>${esc(p.name)}</b></td><td>${chipFlow(p)}</td><td>${statusBadge(p)}</td><td class="r mono small">${p.qwait!=null ? ms(p.qwait)+' wait' : ''}</td></tr>`).join('')}</tbody></table></div>` : ''}
      <div class="card"><div class="card-h"><h3>Diagnostics</h3><button class="btn sm" id="rundiag" style="margin-left:auto">${icon('terminal','sm')}Run diagnostics</button></div><div class="card-b" id="diagout"><div class="muted">Diagnostics inspect deployments, queues and recent failures, then propose a repair plan. Takes up to a minute.</div></div></div></div>
     <div class="card"><div class="card-h"><h3>Evidence</h3></div><div class="card-b"><dl class="kv">${(i.evidence||[]).map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}${i.checkedAt ? `<dt>Checked</dt><dd>${ago(i.checkedAt)}</dd>` : ''}</dl></div></div></div></div>`,
     mount(v) { const f = $('#fixrec', v); if (f) f.onclick = () => applyRecs(i.recs);
       $('#rundiag', v).onclick = async e => { const b = e.currentTarget; b.disabled = true; const o = $('#diagout', v); o.innerHTML = spinner('Collecting evidence… this can take up to a minute');
         try { const d = aff.length===1 ? await api('/api/agent/diagnostics/pipeline', { method:'POST', body:{ pipeline_id:aff[0].id } }) : await api('/api/agent/diagnostics/system'); o.innerHTML = X.diagHtml(d, aff.length===1 ? aff[0] : null); }
         catch (er) { o.innerHTML = `<div class="inline-err">${esc(er.message)}</div>`; } b.disabled = false; }; } };
  }
  const seg = X.query().get('show') || 'open'; const all = M.issues; const list = all.filter(x => seg==='all' || (seg==='info' ? x.sev==='info' : x.sev!=='info'));
  const H = D.health || {};
  return { crumbs:[['Issues']], html:`<div class="page"><div class="ph"><div><h1>Issues</h1><p>Everything that needs attention across pipelines, connections, models and the platform, with a proposed fix for each.</p></div>
    <div class="acts"><a class="btn" href="#/issues/run">${icon('refresh','sm')}Run health checks</a><button class="btn" data-act="agent" data-prompt="Diagnose the whole system and tell me what to fix first.">${icon('sparkle','sm')}Diagnose with agent</button></div></div>
   ${loadErr(['health','insights','triggers'])}
   <div class="row sp" style="margin-bottom:12px"><div class="seg">${[['open','Open',all.filter(x=>x.sev!=='info').length],['info','Informational',all.filter(x=>x.sev==='info').length],['all','All',all.length]].map(([k,l,n]) => `<button onclick="location.hash='#/issues?show=${k}'" class="${seg===k?'on':''}">${l}<span class="ct">${n}</span></button>`).join('')}</div>
    <span class="muted small">Health checked ${ago(H.checked_at)}</span></div>
   <div class="card">${list.length ? list.map(x => `<a class="issue" href="#/issues/${x.id}" style="color:inherit"><span class="sev ${x.sev}">${icon(x.icon||'alert','sm')}</span><div style="min-width:0"><div class="t">${esc(x.title)}</div><div class="d">${esc(x.detail)}</div>
      <div class="row small muted" style="margin-top:6px;gap:10px"><span>${esc(x.area)}</span>${(x.affected||[]).length ? `<span>${plural(x.affected.length,'pipeline')} affected</span>` : ''}</div></div><span class="row" style="gap:6px">${sevBadge(x.sev)}${icon('chev','sm')}</span></a>`).join('')
    : empty('check', seg==='open' ? 'Nothing needs attention' : 'No issues', 'All health checks pass and no pipeline is failing or slow.', '<a class="btn" href="#/issues/run">Re-run health checks</a>')}</div></div>` };
}});
function runPage() {
  return { crumbs:[['Issues','#/issues'],['Health checks']], html:`<div class="page"><div class="ph"><div><h1>Run health checks</h1><p>Verifies connectivity, permissions, vector policies and model access for every resource.</p></div></div>
    <div class="grid g4" id="secs">${[['sources','Sources','plug'],['destinations','Vector stores','db'],['models','Models','cube'],['pipelines','Pipelines','flow']].map(([k,l,ic]) => `<div class="card"><div class="card-b stack s8"><div class="row">${icon(ic)}<b>${l}</b></div><div class="small muted" data-sec="${k}">Queued</div></div></div>`).join('')}</div>
    <div class="row" style="margin-top:16px"><a class="btn" href="#/issues">Back to issues</a></div></div>`,
    mount(v) { (async () => { for (const k of ['sources','destinations','models','pipelines']) { const el = $(`[data-sec="${k}"]`, v); if (!el) return; el.innerHTML = spinner('Checking…');
        try { const r = await api('/api/health/checks/run?section='+k, { method:'POST' }); const items = r[k]||[]; const bad = items.filter(x => x.status!=='healthy').length;
          el.innerHTML = bad ? `<span style="color:var(--warn)">${icon('alert','sm')} ${bad} of ${items.length} need attention</span>` : `<span style="color:var(--ok)">${icon('check','sm')} ${items.length} healthy</span>`; }
        catch (e) { el.innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`; } }
      invalidate('health'); await need(['health'], true); toast('Health checks complete'); if (location.hash.startsWith('#/issues/run')) setTimeout(() => go('#/issues'), 900); })(); } };
}

/* ---------- metrics ---------- */
const tsCache = {};
route('metrics', { needs:[...X.CORE], render() {
  const qp = X.query(); const g = qp.get('g') || 'hour'; const pid = qp.get('p') || '';
  return { crumbs:[['Metrics']], html:`<div class="page"><div class="ph"><div><h1>Metrics</h1><p>Throughput, failures and latency over time, for everything or one pipeline.</p></div>
    <div class="acts"><span id="mp"></span>
    <div class="seg">${[['minute','Minute'],['hour','Hour'],['day','Day']].map(([k,l]) => `<button data-g="${k}" class="${g===k?'on':''}">${l}</button>`).join('')}</div></div></div>
   <div class="card" id="mstats"><div class="stats">${[1,2,3,4].map(() => '<div class="stat"><div class="skel" style="height:44px"></div></div>').join('')}</div></div>
   <div class="grid g2" style="margin-top:16px"><div class="card"><div class="card-h"><h3>Documents processed</h3><span class="legend small muted" style="margin-left:auto">${dot('info')} processed ${dot('err')} failed</span></div><div class="card-b" id="mc1">${spinner('Loading…')}</div></div>
    <div class="card"><div class="card-h"><h3>Average processing latency</h3></div><div class="card-b" id="mc2">${spinner('Loading…')}</div></div></div>
   ${pid ? (() => { const p = M.PIPE[pid] || {}; const L = p.live || {};
      return `<div class="card" style="margin-top:16px"><div class="card-h"><h3>${icon('flow','sm')} ${esc(p.name||pid)}</h3>${p.id ? X.statusBadge(p) : ''}<span class="muted small" style="margin-left:8px">Live counters since this API replica started</span><a class="btn sm" style="margin-left:auto" href="#/pipelines/${pid}/activity">Open pipeline ${icon('chev','sm')}</a></div>
       <div class="stats" style="grid-template-columns:repeat(6,minmax(0,1fr))">${[['Embedded', fmt(L.embedded)], ['Failed', fmt(L.failed), L.failed ? 'var(--err)' : ''], ['Retries', fmt(L.retries)], ['Throttles', fmt(L.throttles), L.throttles ? 'var(--warn)' : ''], ['Queue wait', ms(p.qwait)], ['Last success', ago(L.last_success_at)]].map(([l, val, col]) => `<div class="stat"><div class="lbl">${l}</div><div class="val" style="font-size:20px;${col ? 'color:'+col : ''}">${val}</div></div>`).join('')}</div></div>`; })()
   : (() => { const rank = (key, f, col, note) => { const l = M.pipelines.filter(p => (p[key]||0) > 0).sort((a,b) => b[key]-a[key]).slice(0, 8); const mx = l.length ? l[0][key] : 1;
        return l.length ? `<div class="hbars">${l.map(p => `<button class="hbar" data-pp="${p.id}" title="Show metrics for ${esc(p.name)}"><span class="hb-n trunc">${esc(p.name)}</span><span class="hb-t"><span style="width:${Math.max(2, 100*p[key]/mx)}%;background:${col}"></span></span><span class="hb-v num">${f(p[key])}</span></button>`).join('')}</div>` : `<div class="muted small">${note}</div>`; };
      return `<div class="grid g2" style="margin-top:16px">
       <div class="card"><div class="card-h"><h3>Most vectors written</h3><span class="muted small" style="margin-left:auto">Click a pipeline to drill in</span></div><div class="card-b">${rank('vectors', fmt, 'var(--info)', M.pipelines.some(p => p.statsLoading) ? 'Loading pipeline stats…' : 'No vectors written yet.')}</div></div>
       <div class="card"><div class="card-h"><h3>Longest queue wait</h3><span class="muted small" style="margin-left:auto">Time work waits for a worker</span></div><div class="card-b">${rank('qwait', ms, 'var(--warn)', 'No queue wait recorded.')}</div></div></div>`; })()}
  </div>`,
    mount(v) {
      const nav = (k, val) => { const q = new URLSearchParams(X.query()); val ? q.set(k, val) : q.delete(k); go('#/metrics?'+q); };
      X.combo($('#mp', v), { items:M.pipelines.map(p => ({ v:p.id, l:p.name, s:p.id+' · '+p.src.name+' → '+p.dst.name })), value:pid, all:'All pipelines', placeholder:'Search pipelines', width:240, onPick:val => nav('p', val) }); $$('[data-pp]', v).forEach(b => b.onclick = () => nav('p', b.dataset.pp)); $$('[data-g]', v).forEach(b => b.onclick = () => nav('g', b.dataset.g));
      const span = { minute:3600e3, hour:48*3600e3, day:30*86400e3 }[g];
      const key = g+'|'+pid, hit = tsCache[key] && Date.now() - tsCache[key].at < 30000 ? tsCache[key].p : null;
      const req = hit || (tsCache[key] = { at:Date.now(), p: api(`/api/metrics/timeseries?granularity=${g}&start=${new Date(Date.now()-span).toISOString()}${pid ? '&pipeline_id='+pid : ''}`) }).p;
      req.then(r => { if (!v.contains($('#mc1', v)) || (X.query().get('p')||'') !== pid || (X.query().get('g')||'hour') !== g || !location.hash.startsWith('#/metrics')) return;
        const b = r.buckets || []; const lab = b.map(x => { const d = X.toDate(x.t); return g==='day' ? d.toLocaleDateString(undefined,{month:'short',day:'numeric'}) : d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}); });
        const tot = b.reduce((a,x) => a+(x.processed||0), 0), fl = b.reduce((a,x) => a+(x.failed||0), 0), lat = b.filter(x => x.avg_latency_ms).map(x => x.avg_latency_ms);
        const L = D.live || {}; const el = L.embedding_latency || {};
        const partial = r.coverage && r.coverage !== 'hourly';
        $('#mstats', v).innerHTML = `${partial ? `<div class="callout warn" style="margin-bottom:12px"><span class="ic">${icon('info','sm')}</span><div><b>Partial history</b>Some pipelines only have their last 60 worker reports, so these totals undercount. Full hourly history builds up from new activity. Pipeline pages show lifetime totals.</div></div>` : ''}<div class="stats"><div class="stat"><div class="lbl">Processed</div><div class="val">${fmt(tot)}</div><div class="sub">${partial ? 'recent samples only' : 'in this window'}</div></div>
          <div class="stat"><div class="lbl">Failed</div><div class="val" ${fl?'style="color:var(--err)"':''}>${fmt(fl)}</div><div class="sub">${tot ? (100*fl/(tot+fl)).toFixed(2)+'% failure rate' : '—'}</div></div>
          <div class="stat"><div class="lbl">Avg processing latency</div><div class="val">${lat.length ? ms(lat.reduce((a,x)=>a+x,0)/lat.length) : '<span class="muted" style="font-size:15px">Not recorded</span>'}</div><div class="sub">bucket average</div></div>
          <div class="stat"><div class="lbl">Embedding p95</div><div class="val">${el.count ? ms(el.p95) : '<span class="muted" style="font-size:15px">Not recorded</span>'}</div><div class="sub">${el.count ? `p50 ${ms(el.p50)} · ${fmt(el.count)} samples` : 'no samples on this replica'}</div></div></div>`;
        $('#mc1', v).innerHTML = b.length ? chart(lab, [{ type:'bar', vals:b.map(x=>x.processed||0), color:'var(--info)' }, { vals:b.map(x=>x.failed||0), color:'var(--err)' }]) : empty('chart','No activity in this window','');
        $('#mc2', v).innerHTML = lat.length ? chart(lab, [{ vals:b.map(x=>x.avg_latency_ms||0), color:'var(--accent)', fmt:ms }]) : empty('clock','Latency not recorded','Processing latency is recorded when workers report timings for this window.');
      }).catch(e => { delete tsCache[key]; if (!$('#mc1', v)) return; $('#mc1', v).innerHTML = `<div class="inline-err">${esc(e.message)}</div>`; $('#mc2', v).innerHTML = ''; $('#mstats', v).innerHTML = ''; });
    } };
}});

/* ---------- capacity ---------- */
route('capacity', { render() {
  const I = D.insights || {}; const rows = M.pipelines.filter(p => p.ins); const tw = rows.reduce((a,p) => a+((p.resource_policy||{}).weight||10), 0) || 1;
  const recs = rows.filter(p => p.ins.recommendation && p.ins.recommendation.severity!=='healthy' && p.ins.recommendation.suggested_policy);
  const w = M.workers;
  return { crumbs:[['Capacity']], html:`<div class="page"><div class="ph"><div><h1>Capacity</h1><p>How the shared worker pool is divided between pipelines. Raise a pipeline's weight or concurrency to give it more of the pool.</p></div>
    <div class="acts">${recs.length ? `<button class="btn pri" id="allrec">${icon('wand','sm')}Apply ${plural(recs.length,'recommendation')}</button>` : ''}</div></div>
   ${loadErr(['insights'])}
   <div class="card"><div class="stats"><div class="stat"><div class="lbl">${icon('server','sm')}Workers</div><div class="val">${w ? `${w.ready_replicas??'—'}<small>/ ${w.replicas}</small>` : '—'}</div><div class="sub">${w && w.autoscaling ? `autoscale ${w.autoscaling.min_replicas}–${w.autoscaling.max_replicas}` : 'autoscale unknown'}</div></div>
    <div class="stat"><div class="lbl">Pipelines sharing</div><div class="val">${rows.length}</div><div class="sub">total weight ${tw}</div></div>
    <div class="stat"><div class="lbl">Slow pipelines</div><div class="val" ${recs.length?'style="color:var(--warn)"':''}>${M.pipelines.filter(p => p.qwait > X.QW_SLOW).length}</div><div class="sub">queue wait above ${X.QW_SLOW/1000}s</div></div>
    <div class="stat"><div class="lbl">Throttles observed</div><div class="val">${fmt(rows.reduce((a,p) => a+((p.ins.observed||{}).throttles||0), 0))}</div><div class="sub">429s from models or stores</div></div></div></div>
   ${I.scope_note ? `<div class="callout info" style="margin-top:16px">${icon('info','ic')}<div class="small">${esc(I.scope_note)}</div></div>` : ''}
   <div class="card" style="margin-top:16px"><table class="tbl hide-c7 hide-s5 hide-s6"><thead><tr><th>Pipeline</th><th>Share of pool</th><th class="r">Queue wait</th><th class="r">Processing</th><th class="r">Weight</th><th class="r">Concurrency</th><th>Priority</th><th>Recommendation</th></tr></thead><tbody>
    ${rows.map(p => { const pol = p.resource_policy || {}, o = p.ins.observed || {}, r = p.ins.recommendation || {}; const sh = p.ins.estimated_share_percent ?? Math.round(100*(pol.weight||10)/tw);
      return `<tr><td><a href="#/pipelines/${p.id}/capacity"><b>${esc(p.name)}</b></a></td><td style="min-width:140px"><div class="row" style="gap:8px"><div class="prog grow"><i style="width:${sh}%"></i></div><span class="mono small">${sh}%</span></div></td>
        <td class="r mono" ${p.qwait > X.QW_SLOW ? 'style="color:var(--warn)"' : ''}>${ms(o.avg_queue_wait_ms)}</td><td class="r mono">${ms(o.avg_processing_latency_ms)}</td>
        <td class="r mono">${pol.weight??10}</td><td class="r mono">${pol.max_concurrency_per_worker??2}</td><td class="small">${esc(pol.priority||'normal')}</td>
        <td class="small">${r.severity && r.severity!=='healthy' ? `<span class="badge warn">${esc(r.title||'Increase share')}</span>` : '<span class="muted">Balanced</span>'}</td></tr>`; }).join('') || '<tr><td colspan="8" class="muted">No capacity data yet.</td></tr>'}</tbody></table></div></div>`,
    mount(v) { const b = $('#allrec', v); if (b) b.onclick = () => applyRecs(recs); } };
}});

/* ---------- platform ---------- */
route('platform', { needs:[...X.CORE, 'stats'], render() {
  const deps = D.deployments || []; const H = (D.health||{}).services || [];
  return { crumbs:[['Platform']], html:`<div class="page"><div class="ph"><div><h1>Platform</h1><p>The OmniVec services running in your cluster. Most problems are fixed at the pipeline level; restart or scale here only when a workload is unhealthy.</p></div></div>
   ${loadErr(['deployments'])}
   <div class="grid g2">${deps.map(d => { const ok = (d.ready_replicas||0) >= d.replicas && d.replicas > 0; const a = d.autoscaling || {}; const scalable = /source-connector|^omnivec-worker$/.test(d.name);
     return `<div class="card"><div class="card-h">${dot(d.replicas===0 ? '' : ok ? 'ok' : (d.ready_replicas||0) ? 'warn' : 'err')}<h3 class="trunc">${esc(d.name)}</h3><span class="muted small" style="margin-left:auto">${d.ready_replicas||0} / ${d.replicas} ready</span>
       <button class="btn ghost sm icon" data-dm="${esc(d.name)}" data-sc="${scalable?1:''}">${icon('more','sm')}</button></div>
      <div class="card-b stack s8"><div class="pod">${(d.pods||[]).map(p => `<i class="${/running/i.test(p.status)?'':'off'}" title="${esc(p.name)} · ${esc(p.status)} · ${p.restarts} restarts"></i>`).join('')}</div>
       <div class="row wrap small muted" style="gap:12px">${a.enabled ? `<span>${icon('gauge','sm')} autoscale ${a.min_replicas}–${a.max_replicas}</span>` : '<span>fixed replicas</span>'}<span class="mono trunc" style="max-width:260px">${esc((d.image||'').split('/').pop())}</span>
        ${(d.pods||[]).some(p => p.restarts > 3) ? `<span style="color:var(--warn)">${icon('alert','sm')} ${Math.max(...d.pods.map(p=>p.restarts))} restarts</span>` : ''}</div></div></div>`; }).join('') || `<div class="card">${empty('server','No deployment data', 'The API could not list cluster workloads.')}</div>`}</div>
   ${H.length ? `<div class="card" style="margin-top:16px"><div class="card-h"><h3>Service checks</h3></div><div class="card-b stack s8">${H.map(s => `<div class="chk-row">${dot(s.status==='healthy'?'ok':s.status==='warning'?'warn':'err')}<div><b>${esc(s.name||s.id)}</b> <span class="muted">${esc((s.checks||[]).filter(c=>c.status!=='pass').map(c=>c.detail).join(' · ') || s.status)}</span></div></div>`).join('')}</div></div>` : ''}</div>`,
    mount(v) { $$('[data-dm]', v).forEach(b => b.onclick = () => { const n = b.dataset.dm; const d = deps.find(x => x.name===n);
      X.menu(b, [{ label:'Restart pods', icon:'rotate', run:() => confirmAction({ title:`Restart ${n}?`, body:'Pods are replaced one at a time. In-flight work is retried from the queue.', confirm:'Restart', danger:true, typed:n,
          onOk: async () => { await api(`/api/operations/deployments/${n}/restart`, { method:'POST' }); toast('Rolling restart started'); invalidate('deployments'); setTimeout(() => X.render({ force:true, silent:true }), 3000); } }) },
        ...(b.dataset.sc ? [{ label:'Set minimum replicas', icon:'gauge', run:() => scaleModal(d) }] : [])]); }); } };
}});
function scaleModal(d) {
  const a = d.autoscaling || {};
  const m = openModal(`<div class="modal-h"><h3>Scale ${esc(d.name)}</h3></div><div class="modal-b stack s12"><div class="t2">Sets the autoscaler minimum. More replicas cost more; prefer pipeline allocation for a single slow pipeline.</div>
    <div class="grid g2"><div class="fld"><label>Minimum replicas</label><input class="inp" type="number" id="smin" value="${a.min_replicas ?? d.replicas}" min="0" max="50"></div><div class="fld"><label>Maximum replicas</label><input class="inp" type="number" id="smax" value="${a.max_replicas ?? d.replicas}" min="1" max="50"></div></div><div class="inline-err" id="serr2"></div></div>
    <div class="modal-f"><button class="btn" data-close>Cancel</button><button class="btn pri" id="sok">Apply</button></div>`);
  $('#sok', m).onclick = async () => { const mn = +$('#smin', m).value, mx = +$('#smax', m).value; if (mx < mn) { $('#serr2', m).textContent = 'Maximum must be at least the minimum.'; return; }
    try { await api(`/api/operations/deployments/${d.name}/scale`, { method:'POST', body:{ replicas:mn, max_replicas:mx } }); closeOverlay(); toast('Scale updated'); invalidate('deployments'); X.render({ force:true, silent:true }); }
    catch (e) { $('#serr2', m).textContent = e.message; } };
}
})();
