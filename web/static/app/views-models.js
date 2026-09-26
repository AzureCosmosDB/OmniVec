/* OmniVec console · model and recipe detail pages */
(() => {
const X = window.OVX;
const { icon, esc, fmt, ms, ago, plural, api, invalidate, D, M, T, logo, dot, empty, spinner, go, toast, confirmAction, openModal, $, $$ } = X;

const nice = k => String(k).replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
const val = v => v === true ? 'Yes' : v === false ? 'No' : v == null || v === '' ? '<span class="muted">Default</span>' : esc(typeof v === 'object' ? JSON.stringify(v) : v);
const kv = rows => `<dl class="kv">${rows.filter(r => r && r[1] != null && r[1] !== '').map(([k, v, mono]) => `<dt>${esc(k)}</dt><dd class="${mono ? 'mono small' : ''}">${v}</dd>`).join('')}</dl>`;
const healthBadge = h => h ? `<span class="badge ${h.status==='healthy'?'ok':'warn'}">${dot(h.status==='healthy'?'ok':'warn')}${h.status==='healthy'?'Healthy':esc(h.status)}</span>` : '<span class="badge out">Not checked</span>';
const pipeTable = (list, note) => list.length
  ? `<table class="tbl"><thead><tr><th>Pipeline</th><th>Health</th><th>Vector store</th><th class="r">Vectors</th><th>Last write</th></tr></thead><tbody>${list.map(p => `<tr class="clickrow" data-href="#/pipelines/${p.id}"><td><div class="nm">${esc(p.name)}</div><div class="id">${p.id}</div></td><td>${X.statusBadge(p)}</td><td><div class="row" style="gap:8px">${logo(p.dst.type,'sm')}<span class="trunc">${esc(p.dst.name)}</span></div></td><td class="r num">${p.statsLoading ? '<span class="skel" style="display:inline-block;width:44px;height:12px;vertical-align:middle"></span>' : fmt(p.vectors)}</td><td class="muted small">${ago(p.lastWrite)}</td></tr>`).join('')}</tbody></table>`
  : `<div class="card-b muted">${note}</div>`;
const rowLinks = v => $$('tr[data-href]', v).forEach(r => r.onclick = e => { if (!e.target.closest('button,a')) go(r.dataset.href); });
const pid = () => ((D.identity||{}).principal_id) || ((D.caps||{}).identity||{}).principal_id || null;

/* ---------- model detail ---------- */
X.modelDetail = id => {
  const m = M.MDL[id];
  if (!m) return { crumbs:[['Models & recipes','#/models'],['Not found']], html:`<div class="page">${empty('cube','This model was not found','It may have been deleted.','<a class="btn" href="#/models">Back to models</a>')}</div>` };
  const h = M.hm[m.id], used = X.usedBy(m.id), chat = m.model_category === 'chat', native = m.kind === 'native' || !m.type;
  const title = m.deployment || m.name;
  const auth = m.auth_type==='managed-identity' || (m.endpoint && !m.api_key) ? 'Managed identity' : m.api_key ? 'API key (Key Vault)' : 'In-cluster, no auth';
  const conf = native
    ? [['Runs as', 'OmniVec native model (in-cluster)'], ['Model type', esc(m.model_type||'text')], ['Image', esc(m.image||'—'), 1], ['Replicas', m.replicas != null ? `${m.ready_replicas ?? '—'} of ${m.replicas} ready` : null], ['Memory', esc(m.memory||'')], ['GPU', m.gpu && m.gpu !== '0' ? esc(m.gpu) : 'None (CPU)'], ['Dimensions', m.embedding_dim ? esc(m.embedding_dim) : '<span class="muted">Not reported by the model</span>']]
    : [['Provider', esc(T(m.type).label)], ['Deployment', esc(m.deployment||'—'), 1], ['Endpoint', esc(m.endpoint||'—'), 1], ['API version', esc(m.api_version||''), 1], chat ? null : ['Dimensions', m.embedding_dim ? esc(m.embedding_dim) : '<span class="muted">Not set</span>'], ['Authentication', auth]];
  const html = `<div class="page">
   <div class="ph"><div style="min-width:0"><div class="row">${logo(m.type||'native','lg')}<div style="min-width:0"><div class="row"><h1 class="trunc">${esc(title)}</h1>${healthBadge(h)}</div>
     <p class="mono small">${m.id} · ${chat ? 'Chat model (agent)' : 'Embedding model'}${m.name && m.name !== title ? ' · registered as '+esc(m.name) : ''}</p></div></div></div>
    <div class="acts"><button class="btn pri" id="mtest">${icon('play','sm')}Test model</button>${native ? '' : `<button class="btn" id="macc">${icon('shield','sm')}Access guidance</button>`}<button class="btn icon" id="mdel" title="Delete model">${icon('trash','sm')}</button></div></div>
   <div id="mtestout"></div>
   <div class="grid g-main" style="align-items:start"><div class="stack">
    <div class="card"><div class="card-h"><h3>${icon('flow','sm')} Used by</h3><span class="badge">${used.length}</span>${chat ? '' : `<a class="btn sm" style="margin-left:auto" href="#/new">${icon('plus','sm')}New pipeline</a>`}</div>
     ${chat ? `<div class="card-b muted">Chat models power the OmniVec agent, not pipelines.</div>` : pipeTable(used, 'No pipelines use this model yet. New pipelines can choose it in the Embedding step.')}</div>
    ${!chat && used.length ? `<div class="callout info">${icon('info','ic')}<div><b>Changing or removing this model affects ${plural(used.length,'pipeline')}</b>Vectors from different models aren't comparable. To switch models, create a new pipeline that writes to a store with matching dimensions, then retire the old one.</div></div>` : ''}
   </div><div class="stack">
    <div class="card"><div class="card-h"><h3>Health checks</h3><span class="muted small" style="margin-left:auto">${h ? ago(h.checked_at) : ''}</span></div><div class="card-b stack s8">${h && (h.checks||[]).length ? h.checks.map(k => `<div class="chk-row">${X.chkIcon(k.status)}<div><b style="font-weight:600">${esc(X.humanCheck(k.check))}</b> <span class="muted">${esc(k.detail||'')}</span></div></div>`).join('') : '<div class="muted">No checks have run yet.</div>'}</div></div>
    <div class="card"><div class="card-h"><h3>Configuration</h3></div><div class="card-b">${kv(conf)}</div></div>
   </div></div></div>`;
  return { crumbs:[['Models & recipes','#/models'],[title]], html, mount(v) {
    rowLinks(v);
    $('#mtest', v).onclick = async e => { const b = e.currentTarget, out = $('#mtestout', v); b.disabled = true; out.innerHTML = `<div class="callout">${spinner('Sending a test request…')}</div>`;
      try { const r = await api(`/api/models/${m.id}/test`, { method:'POST' }); const ok = r.success !== false, d = r.dimensions || r.embedding_dim;
        out.innerHTML = `<div class="callout ${ok?'ok':'err'}">${icon(ok?'check':'alert','ic')}<div><b>${ok ? 'Model responded' : 'Test failed'}</b>${ok ? [d ? d+' dimensions' : '', r.latency_ms ? ms(r.latency_ms) : ''].filter(Boolean).join(' · ') + (d && m.embedding_dim && +d !== +m.embedding_dim ? ` <span style="color:var(--warn)">Registered as ${m.embedding_dim} dims.</span>` : '') : esc(r.error||r.message||'No detail returned')}</div></div>`; }
      catch (er) { out.innerHTML = `<div class="callout err">${icon('alert','ic')}<div><b>Test failed</b>${esc(er.message)}</div></div>`; }
      b.disabled = false; };
    const acc = $('#macc', v); if (acc) acc.onclick = () => openModal(`<div class="modal-h"><h3>Grant access to ${esc(title)}</h3></div><div class="modal-b">${X.perm.guideHtml('open','azure-openai', m, pid())}</div><div class="modal-f"><button class="btn" data-close>Close</button></div>`, 'wide');
    $('#mdel', v).onclick = () => confirmAction({ title:`Delete ${title}?`, danger:true, typed: used.length ? null : m.name,
      body: used.length ? `This model is used by ${plural(used.length,'pipeline')}. Change or delete those pipelines first.` : 'The registration is removed. Vectors already written are not affected.', confirm:'Delete',
      onOk: async () => { if (used.length) return; await api('/api/models/'+m.id, { method:'DELETE' }); toast('Model deleted'); invalidate('models'); go('#/models'); } });
  } };
};

/* ---------- recipe detail ---------- */
const STAGE = { extract:['file','Extract','Reads the file and pulls out text'], chunk:['layers','Chunk','Splits text into passages'], embed:['cube','Embed','Turns each passage into a vector'], sample:['play','Sample','Picks frames from the media'] };
X.recipeDetail = name => {
  const t = (D.transforms||[]).find(x => x.name === name);
  if (!t) return { crumbs:[['Models & recipes','#/models'],['Recipes','#/models/recipes'],['Not found']], html:`<div class="page">${empty('layers','This recipe was not found','DocGrok may be unavailable, or the recipe was removed.','<a class="btn" href="#/models/recipes">Back to recipes</a>')}</div>` };
  const a = t.applies_to || {}, ex = Array.isArray(a) ? a : a.extensions || [], ct = Array.isArray(a) ? [] : a.content_types || [];
  const used = M.pipelines.filter(p => p.recipe === t.name);
  const stages = (t.stages||[]).map((s, i) => { const st = STAGE[s.type] || ['layers', nice(s.type||'Stage'), ''];
    const cfg = Object.entries(s.config||{});
    return `<div class="rstage"><div class="rstage-n">${i+1}</div><div class="card grow"><div class="card-h"><h3>${icon(st[0],'sm')} ${esc(st[1])} <span class="mono muted small" style="font-weight:400">${esc(s.name||'')}</span></h3><span class="muted small" style="margin-left:auto">${esc(st[2])}</span></div>
      ${cfg.length ? `<div class="card-b">${kv(cfg.map(([k, v]) => [nice(k), s.type==='embed' && k==='model_id' && v == null ? '<span class="muted">Pipeline\'s embedding model</span>' : val(v)]))}</div>` : ''}</div></div>`; }).join('');
  const html = `<div class="page">
   <div class="ph"><div style="min-width:0"><div class="row">${logo('recipe','lg')}<div style="min-width:0"><div class="row"><h1 class="trunc">${esc(t.name)}</h1><span class="badge out">v${esc(t.version||'1')}</span>${t.source ? `<span class="badge">${esc(t.source==='builtin'?'Built-in':t.source)}</span>` : ''}</div>
     <p>${esc(t.description||'')}</p></div></div></div>
    <div class="acts"><a class="btn pri" href="#/new">${icon('plus','sm')}Use in a new pipeline</a></div></div>
   <div class="grid g-main" style="align-items:start"><div class="stack">
    <div class="eyebrow">How a file is processed</div>
    <div class="rstages">${stages || '<div class="muted">No stages defined.</div>'}</div>
    <div class="card"><div class="card-h"><h3>${icon('flow','sm')} Used by</h3><span class="badge">${used.length}</span></div>${pipeTable(used, 'No pipelines use this recipe yet. Choose it in the Embedding step when you create a pipeline.')}</div>
   </div><div class="stack">
    <div class="card"><div class="card-h"><h3>Applies to</h3></div><div class="card-b stack s12">
     <div><div class="muted small" style="margin-bottom:6px">File extensions</div><div class="row wrap" style="gap:6px">${ex.map(x => `<span class="badge out mono">${esc(x)}</span>`).join('') || '<span class="muted">Any</span>'}</div></div>
     ${ct.length ? `<div><div class="muted small" style="margin-bottom:6px">Content types</div><div class="stack s4">${ct.map(x => `<span class="mono small" style="overflow-wrap:anywhere">${esc(x)}</span>`).join('')}</div></div>` : ''}
    </div></div>
    <div class="card"><div class="card-h"><h3>Details</h3></div><div class="card-b">${kv([['Priority', t.priority != null ? esc(t.priority)+' <span class="muted small">(lower runs first when several recipes match)</span>' : null], ['Stages', esc((t.stages||[]).length)], ['Source', esc(t.source==='builtin' ? 'Built into DocGrok' : t.source||'—')]])}</div></div>
   </div></div></div>`;
  return { crumbs:[['Models & recipes','#/models'],['Recipes','#/models/recipes'],[t.name]], html, mount: rowLinks };
};
})();
