/* OmniVec console: New pipeline wizard (Source → Content → Embedding → Vector store → Review) */
(() => {
const X = window.OVX;
const { icon, esc, fmt, plural, api, need, invalidate, D, M, T, logo, empty, spinner, route, go, toast, $, $$ } = X;

const STEPS = [['source','Source','Where content comes from'],['content','Content','What to index and how to split it'],['embed','Embedding','Model or processing recipe'],['store','Vector store','Where vectors go'],['review','Review & create','Name it and start']];
let W = null;
const fresh = () => ({ step:0, sourceId:null, fields:null, fileTypes:null, contentMode:'field', strategy:'chunk', chunk:{ size:1000, overlap:200, unit:'chars' }, spTenant:'', spClient:'',
  modelId:null, recipe:null, storeId:null, indexes:null, indexErr:null, vpath:null, mode:'queue', name:'', desc:'', processExisting:true,
  docId:'{source_hash}-{pipeline}', pk:'{source_partition}', chunkId:'{source_hash}-{pipeline}-chunk-{chunk}', collision:'reject', storeText:true, meta:['pipeline_name','embedding_dims','source_ref'], priority:'normal', sample:null, nameTouched:false });

const src = () => M.SRC[W.sourceId]; const dst = () => M.DST[W.storeId]; const mdl = () => M.MDL[W.modelId];
const embModels = () => M.models.filter(m => m.model_category !== 'chat').sort((a,b) => (!!b.embedding_dim - !!a.embedding_dim) || ((M.hm[b.id]||{}).status==='healthy') - ((M.hm[a.id]||{}).status==='healthy'));
const hOk = (h) => !h ? null : h.status === 'healthy';
function storeDims() { const d = dst(); if (!d) return null; const pick = (W.indexes||[]).find(i => (i.path||'').replace(/^\//,'') === W.vpath) || (W.indexes||[])[0];
  if (pick && pick.dimensions) return pick.dimensions; if (d.type==='pgvector') return (d.config||{}).vector_dimensions || null;
  const pol = ((M.hd[d.id]||{}).checks||[]).find(c => c.check==='vector_policy'); return pol ? pol.dimensions : null; }
function sameContainer() { const s = src(), d = dst(); if (!s || !d) return false; const a = s.config||{}, b = d.config||{};
  return s.type==='cosmosdb' && d.type==='cosmosdb-vector' && (a.endpoint||'').replace(/:443\/?$/,'').replace(/\/$/,'') === (b.endpoint||'').replace(/:443\/?$/,'').replace(/\/$/,'') && a.database===b.database && a.container===b.container; }

function checks() {
  const s = src(), d = dst(), m = mdl(), out = [];
  if (s) { const h = M.hs[s.id]; out.push(h ? [hOk(h)?'pass':'warn', hOk(h) ? `Source reachable (${X.humanCheck('read_permission').toLowerCase()} passed)` : 'Source health check reported a problem'] : ['info','Source not health-checked yet']); }
  if (m) { const h = M.hm[m.id]; out.push(h ? [hOk(h)?'pass':'warn', hOk(h) ? 'Model reachable and authorized' : 'Model health check reported a problem'] : ['info','Model not health-checked yet']); }
  else if (W.recipe) out.push(['info', `Recipe “${W.recipe}” runs in DocGrok`]);
  if (d) { const h = M.hd[d.id]; out.push(h ? [hOk(h)?'pass':'warn', hOk(h) ? 'Vector store writable' : 'Vector store check reported a problem'] : ['info','Vector store not health-checked yet']); }
  const sd = storeDims(), md = m && m.embedding_dim;
  if (d && m && !md) out.push(['warn',"${m.deployment||m.name} doesn't report its dimensions yet. It may still be deploying."]);
  else if (d && m) out.push(!sd ? ['info','Store dimensions unknown: test the store to confirm'] : sd===md ? ['pass',`Dimensions match (${md})`] : ['fail',`Dimension mismatch: model ${md}, store ${sd}`]);
  else out.push(['wait','Dimension match: waiting for model and store']);
  if (s && s.type==='sharepoint') out.push(['info','SharePoint needs queue mode and a Cosmos DB vector store. Set automatically.']);
  if (W.mode==='inline' && W.strategy==='chunk') out.push(['fail','Inline mode cannot chunk. Use queue mode.']);
  if (W.mode==='inline' && d && !sameContainer()) out.push(['fail','Inline mode needs the source and store to be the same container.']);
  if (d && m) { const other = M.pipelines.filter(p => p.dst.id===d.id && p.model && p.model.id!==m.id); if (other.length) out.push(['warn',`${plural(other.length,'other pipeline')} write to this store with a different model. Scores won't be comparable.`]); }
  if (d && W.vpath && M.pipelines.some(p => p.dst.id===d.id && p.src.id===W.sourceId)) out.push(['warn','Another pipeline already writes this source to this store. Use a distinct document ID pattern.']);
  return out;
}
const blocking = () => checks().some(c => c[0]==='fail');

function canNext(i) { return i===0 ? !!W.sourceId : i===1 ? (W.strategy!=='chunk' || (W.chunk.size>0 && W.chunk.overlap>=0 && W.chunk.overlap < W.chunk.size)) : i===2 ? !!(W.modelId || W.recipe) : i===3 ? !!(W.storeId && W.vpath) && !blocking() : true; }

route('new', { needs:[...X.CORE, 'transforms'], live:false, render() {
  const qp = X.query();
  if (!W || qp.get('reset')) W = fresh();
  if (qp.get('source') && M.SRC[qp.get('source')]) { W.sourceId = M.SRC[qp.get('source')].id; if (W.step < 1) W.step = 1; }
  if (qp.get('store') && M.DST[qp.get('store')]) { W.storeId = M.DST[qp.get('store')].id; W.indexes = null; W.step = Math.max(W.step, 3); }
  if (!W.modelId && !W.recipe && embModels().length === 1) W.modelId = embModels()[0].id;
  const html = `<div class="page"><div class="ph"><div><h1>New pipeline</h1><p>Keep a vector store in sync with a source. You can change names and tuning later; the source, model and store define the index.</p></div>
    <div class="acts"><button class="btn ghost" id="wreset">${icon('rotate','sm')}Start over</button></div></div>
   <div class="grid" style="grid-template-columns:220px minmax(0,1fr) 320px;align-items:start">
    <div class="steps" id="wsteps"></div><div class="card"><div class="card-b stack" id="wbody"></div></div>
    <div class="stack" style="position:sticky;top:12px"><div class="card"><div class="card-h"><h3>Your pipeline</h3></div><div class="card-b stack s12" id="wsum"></div></div>
     <div class="card"><div class="card-h"><h3>Checks</h3></div><div class="card-b stack s8 small" id="wchk"></div></div></div></div></div>`;
  return { crumbs:[['Pipelines','#/pipelines'],['New pipeline']], html, mount(v) {
    $('#wreset', v).onclick = () => { W = fresh(); history.replaceState(null, '', '#/new'); draw(v); };
    draw(v);
  } };
}});

function draw(v) {
  if (W.storeId && W.indexes === null && !W._loadingIdx) loadIndexes(v);
  if (W.sourceId && W.fields === null) initContent();
  $('#wsteps', v).innerHTML = STEPS.map(([k,l,d], i) => { const done = i < W.step && canNext(i); const sum = summaryFor(i);
    return `<div class="step ${i===W.step?'on':''} ${done?'done':''}" data-step="${i}"><span class="n">${done ? icon('check','sm') : i+1}</span><div style="min-width:0"><b>${l}</b><small class="trunc" style="display:block">${esc(i < W.step && sum ? sum : d)}</small></div></div>`; }).join('');
  $$('[data-step]', v).forEach(s => s.onclick = () => { const i = +s.dataset.step; if (i <= W.step || [...Array(i).keys()].every(canNext)) { W.step = i; draw(v); } });
  $('#wbody', v).innerHTML = [stepSource, stepContent, stepEmbed, stepStore, stepReview][W.step]() + `
    <div class="inline-err" id="werr"></div>
    <div class="row sp" style="border-top:1px solid var(--border);padding-top:14px">${W.step ? '<button class="btn" id="wback">'+icon('back','sm')+'Back</button>' : '<a class="btn" href="#/pipelines">Cancel</a>'}
     ${W.step < 4 ? `<button class="btn pri" id="wnext" ${canNext(W.step)?'':'disabled'}>Continue to ${STEPS[W.step+1][1].toLowerCase()}${icon('chev','sm')}</button>` : `<button class="btn pri" id="wcreate" ${blocking()?'disabled':''}>${icon('play','sm')}Create and start</button>`}</div>`;
  drawSide(v); bind(v);
}
function summaryFor(i) { const s = src(), m = mdl(), d = dst();
  return i===0 ? s && s.name : i===1 ? (W.strategy==='chunk' ? `Chunks ${W.chunk.size}/${W.chunk.overlap}` : 'Whole document') : i===2 ? (m ? (m.deployment||m.name) : W.recipe) : i===3 ? d && d.name : ''; }
function drawSide(v) {
  const s = src(), m = mdl(), d = dst();
  const node = (lg, t, sub, dim) => `<div class="row" ${dim?'style="opacity:.5"':''}>${lg}<div class="grow" style="min-width:0"><div style="font-weight:600" class="trunc">${esc(t)}</div><div class="muted small trunc">${esc(sub)}</div></div></div>`;
  const q = '<span class="logo" style="background:var(--subtle);color:var(--muted)">?</span>';
  $('#wsum', v).innerHTML = [
    s ? node(logo(s.type), s.name, T(s.type).short) : node(q, 'Source', 'Choose in step 1', 1),
    `<div class="muted small" style="padding-left:12px">↓ ${W.strategy==='chunk' ? `chunks ${esc(W.chunk.size)} / ${esc(W.chunk.overlap)} ${esc(W.chunk.unit)}` : 'whole document'}${W.fileTypes && W.fileTypes.length && s && s.type!=='cosmosdb' ? ' · '+esc(W.fileTypes.join(', ')) : ''}</div>`,
    m ? node(logo(m.type), m.deployment || m.name, `${m.embedding_dim||'—'} dims · ${T(m.type).short}`) : W.recipe ? node(logo('recipe'), W.recipe, 'Processing recipe') : node(q, 'Embedding', 'Choose in step 3', 1),
    `<div class="muted small" style="padding-left:12px">↓ ${esc(W.mode)} mode</div>`,
    d ? node(logo(d.type), d.name, `${T(d.type).short}${W.vpath ? ' · /'+W.vpath : ''}`) : node(q, 'Vector store', 'Choose in step 4', 1),
  ].join('');
  const ic = { pass:['check','var(--ok)'], warn:['alert','var(--warn)'], fail:['x','var(--err)'], info:['info','var(--info)'], wait:['clock','var(--faint)'] };
  $('#wchk', v).innerHTML = checks().map(([k,t]) => `<div class="chk-row"><span style="color:${ic[k][1]};display:inline-flex">${icon(ic[k][0],'sm')}</span><span ${k==='wait'?'class="faint"':''}>${esc(t)}</span></div>`).join('') || '<div class="muted">Checks appear as you choose.</div>';
}

/* ---------- steps ---------- */
function stepSource() {
  return `<div><h2 style="font-size:17px;margin:0">Where does the content come from?</h2><div class="muted">Pick a connected source, or connect a new one. Sources can feed many pipelines.</div></div>
   <div class="grid g2">${M.sources.map(s => { const h = M.hs[s.id]; const n = M.pipelines.filter(p => p.src.id===s.id).length;
     return `<div class="opt ${W.sourceId===s.id?'on':''}" data-src="${s.id}"><span class="tick">${icon('check','sm')}</span>${logo(s.type)}<div style="min-width:0"><div class="t trunc">${esc(s.name)}</div><div class="d">${esc(T(s.type).short)} · ${plural(n,'pipeline')} ${h ? '· '+(h.status==='healthy'?'healthy':h.status) : ''}</div></div></div>`; }).join('')}
    <a class="opt" href="#/connections/new/source?then=new" style="border-style:dashed"><span class="logo" style="background:var(--accent-soft);color:var(--accent-text)">${icon('plus','sm')}</span><div><div class="t">Connect a new source</div><div class="d">Guided setup with a permissions check. You'll come back here.</div></div></a></div>`;
}
function initContent() { const s = src(); if (!s) return; const c = s.config||{};
  W.fields = ['content']; W.fileTypes = c.file_types || (c.file_type ? [c.file_type] : ['pdf','docx','txt','md']);
  if (s.type==='onelake-iceberg') W.fields = c.content_fields || ['content'];
  if (!W.nameTouched) W.name = ''; W.sample = null; }
function stepContent() {
  const s = src(); if (!s) return empty('plug','Choose a source first','');
  const files = ['azure-blob','sharepoint'].includes(s.type);
  const sampleKeys = W.sample && W.sample.length && typeof W.sample[0]==='object' ? Object.keys(W.sample[0]).filter(k => !k.startsWith('_')).slice(0, 14) : [];
  return `<div><h2 style="font-size:17px;margin:0">What should be indexed?</h2><div class="muted">${files ? 'Choose which files become searchable and how they are split.' : 'Choose which fields hold the text to embed and how it is split.'}</div></div>
   ${files ? `<div class="fld"><label>File types</label><div class="tagbox" id="wft">${(W.fileTypes||[]).map(t => `<span class="badge acc" data-tag="${esc(t)}">${esc(t)} ${icon('x','sm')}</span>`).join('')}<input placeholder="Add, e.g. pdf"></div><span class="hint">PDF and Office files are text-extracted (with OCR fallback for scanned PDFs).</span></div>`
    : `<div class="fld"><label>Text fields to embed</label><div class="tagbox" id="wcf">${(W.fields||[]).map(t => `<span class="badge acc" data-tag="${esc(t)}">${esc(t)} ${icon('x','sm')}</span>`).join('')}<input placeholder="Add a field name"></div>
       ${sampleKeys.length ? `<div class="row wrap small" style="margin-top:6px"><span class="muted">Fields in sample:</span>${sampleKeys.map(k => `<button class="badge out" data-addf="${esc(k)}">+ ${esc(k)}</button>`).join('')}</div>` : ''}<span class="hint">Multiple fields are concatenated in order.</span></div>`}
   <div class="callout">${icon('file','ic')}<div class="grow"><div class="row sp"><b>Preview</b><button class="btn sm" id="wsample">${W.sample ? 'Reload' : 'Load sample'}</button></div>
     <div id="wsampleout">${W.sample ? sampleHtml() : '<span class="muted">Load a few items to confirm this is the right data.</span>'}</div></div></div>
   <div class="fld"><label>How should documents be split?</label><div class="grid g2">
     <div class="opt ${W.strategy==='chunk'?'on':''}" data-strat="chunk"><span class="tick">${icon('check','sm')}</span><div><div class="t">Chunks <span class="badge ok">Recommended</span></div><div class="d">Best for long documents and precise answers.</div></div></div>
     <div class="opt ${W.strategy==='truncate'?'on':''}" data-strat="truncate"><span class="tick">${icon('check','sm')}</span><div><div class="t">Whole document</div><div class="d">One vector per item, truncated to the model limit. Best for short records.</div></div></div></div></div>
   ${W.strategy==='chunk' ? `<div class="grid g3"><div class="fld"><label>Chunk size</label><input class="inp" type="number" id="wcs" value="${W.chunk.size}"></div><div class="fld"><label>Overlap</label><input class="inp" type="number" id="wco" value="${W.chunk.overlap}"></div>
     <div class="fld"><label>Unit</label><select class="sel" id="wcu"><option value="chars" ${W.chunk.unit==='chars'?'selected':''}>Characters</option><option value="tokens" ${W.chunk.unit==='tokens'?'selected':''}>Words (≈ tokens)</option></select></div></div>
     ${W.chunk.overlap >= W.chunk.size ? '<div class="inline-err">Overlap must be smaller than the chunk size.</div>' : ''}` : ''}
   ${s.type==='sharepoint' ? `<details><summary class="link small">Site in another Microsoft Entra tenant?</summary><div class="grid g2" style="margin-top:10px"><div class="fld"><label>Tenant ID</label><input class="inp mono" id="wspt" value="${esc(W.spTenant)}"></div><div class="fld"><label>App (client) ID</label><input class="inp mono" id="wspc" value="${esc(W.spClient)}"></div></div><span class="hint">The app needs a federated credential for system:serviceaccount:omnivec:omnivec-api and Sites.Selected on the site.</span></details>` : ''}
   ${s.type==='cosmosdb' ? `<details><summary class="link small">Content is a URL to a file?</summary><div class="fld" style="margin-top:10px"><label>Content mode</label><select class="sel" id="wcm"><option value="field" ${W.contentMode==='field'?'selected':''}>Field holds the text</option><option value="blob_url" ${W.contentMode==='blob_url'?'selected':''}>Field holds a Blob Storage URL</option><option value="http_url" ${W.contentMode==='http_url'?'selected':''}>Field holds an HTTP URL</option></select></div></details>` : ''}`;
}
const sampleHtml = () => (W.sample||[]).slice(0,3).map(d => { const o = typeof d==='object' ? d : { value:d }; const name = o.name || o.source_ref || o.id || o.path || '';
  const text = W.fields && W.fields.map(f => o[f]).filter(Boolean).join(' ') || o.content || o.text || '';
  return `<div class="row sp small" style="margin-top:6px;gap:12px"><span class="trunc" style="max-width:55%">${esc(name)}</span><span class="muted trunc" style="max-width:45%">${esc(String(text).slice(0,90) || (o.size ? fmt(o.size)+' bytes' : ''))}</span></div>`; }).join('') || '<span class="muted">No items returned.</span>';
function stepEmbed() {
  const ms = embModels(); const tr = D.transforms || []; const s = src();
  const fileHeavy = s && ['azure-blob','sharepoint'].includes(s.type);
  return `<div><h2 style="font-size:17px;margin:0">How should content be embedded?</h2><div class="muted">Use a model directly, or a processing recipe that extracts, chunks and embeds specific file types.</div></div>
   <div class="eyebrow">Embedding models</div>
   <div class="grid g2">${ms.map(m => { const h = M.hm[m.id]; return `<div class="opt ${W.modelId===m.id?'on':''}" data-model="${m.id}"><span class="tick">${icon('check','sm')}</span>${logo(m.type)}<div style="min-width:0"><div class="t trunc">${esc(m.deployment || m.name)}</div><div class="d">${m.embedding_dim||'—'} dims · ${esc(T(m.type).short)}${m.endpoint && !m.api_key ? ' · managed identity' : ''}${h ? ' · '+(h.status==='healthy'?'healthy':h.status) : ''}</div></div></div>`; }).join('')}
    <a class="opt" href="#/models/new?then=new" style="border-style:dashed"><span class="logo" style="background:var(--accent-soft);color:var(--accent-text)">${icon('plus','sm')}</span><div><div class="t">Register a model</div><div class="d">Azure OpenAI with managed identity</div></div></a></div>
   ${tr.length ? `<details ${W.recipe || fileHeavy ? 'open' : ''}><summary class="eyebrow" style="cursor:pointer">Processing recipes ${fileHeavy ? '<span class="badge acc" style="text-transform:none;letter-spacing:0">Useful for files</span>' : ''}</summary>
     <div class="grid g2" style="margin-top:8px">${tr.map(t => `<div class="opt ${W.recipe===t.name?'on':''}" data-recipe="${esc(t.name)}"><span class="tick">${icon('check','sm')}</span>${logo('recipe')}<div style="min-width:0"><div class="t">${esc(t.name)}</div><div class="d">${esc((t.description||'').slice(0,110))}</div></div></div>`).join('')}</div></details>` : ''}`;
}
async function loadIndexes(v) {
  const d = dst(); if (!d) return; W._loadingIdx = true; W.indexErr = null;
  const cfgIdx = (d.config||{}).vector_indexes;
  try {
    if (d.type==='cosmosdb-vector') { const r = await api('/api/destinations/test-connection', { method:'POST', body:{ type:d.type, config:d.config||{}, destination_id:d.id } });
      W.indexes = r.vector_indexes || cfgIdx || []; if (r.success === false) W.indexErr = r.error || r.message; }
    else if (d.type==='pgvector') W.indexes = [{ path:(d.config||{}).vector_column||'embedding', dimensions:(d.config||{}).vector_dimensions, indexType:(d.config||{}).index_type }];
    else W.indexes = cfgIdx || [{ path:'embedding' }];
  } catch (e) { W.indexes = cfgIdx || []; W.indexErr = e.message; }
  if (!W.indexes.length) { const pol = ((M.hd[d.id]||{}).checks||[]).find(c => c.check==='vector_policy'); if (pol && pol.vector_field) W.indexes = [{ path:'/'+pol.vector_field, dimensions:pol.dimensions }]; }
  if (!W.vpath && W.indexes.length) W.vpath = (W.indexes[0].path||'').replace(/^\//,'');
  W._loadingIdx = false; if (v.isConnected && location.hash.startsWith('#/new')) draw(v);
}
function stepStore() {
  const s = src(); const sp = s && s.type==='sharepoint';
  const inlineOk = sameContainer() && W.strategy!=='chunk' && !sp;
  return `<div><h2 style="font-size:17px;margin:0">Where should vectors be written?</h2><div class="muted">Choose a vector store whose dimensions match the model.</div></div>
   <div class="grid g2">${M.dests.map(d => { const n = M.pipelines.filter(p => p.dst.id===d.id).length; const dis = sp && d.type!=='cosmosdb-vector';
     const vi = ((d.config||{}).vector_indexes||[])[0]; return `<div class="opt ${W.storeId===d.id?'on':''} ${dis?'dis':''}" ${dis?'':`data-store="${d.id}"`}><span class="tick">${icon('check','sm')}</span>${logo(d.type)}<div style="min-width:0"><div class="t trunc">${esc(d.name)}</div><div class="d">${esc(T(d.type).short)}${vi ? ` · ${vi.indexType} · ${vi.dimensions}d` : ''} · ${plural(n,'pipeline')}${dis ? ' · not supported for SharePoint' : ''}</div></div></div>`; }).join('')}
    <a class="opt" href="#/connections/new/store?then=new" style="border-style:dashed"><span class="logo" style="background:var(--accent-soft);color:var(--accent-text)">${icon('plus','sm')}</span><div><div class="t">Add a vector store</div><div class="d">Cosmos DB, pgvector or OneLake</div></div></a></div>
   ${W.storeId ? `<div class="grid g2"><div class="fld"><label>Vector field</label>${W.indexes===null ? `<div class="muted small">${spinner('Reading the vector policy…')}</div>` : W.indexes.length ? `<select class="sel" id="wvp">${W.indexes.map(i => { const p = (i.path||'').replace(/^\//,''); return `<option value="${esc(p)}" ${W.vpath===p?'selected':''}>/${esc(p)}${i.dimensions ? ` · ${i.dimensions}d` : ''}${i.indexType ? ' · '+esc(i.indexType) : ''}${i.distanceFunction ? ' · '+esc(i.distanceFunction) : ''}</option>`; }).join('')}</select>` : `<input class="inp mono" id="wvpi" value="${esc(W.vpath||'embedding')}"><span class="hint">No vector policy was found. Enter the vector path configured on the container.</span>`}
      ${W.indexErr ? `<div class="inline-err">${esc(W.indexErr)} · <a class="link" href="#/connections/store/${W.storeId}/access">Check access</a></div>` : ''}</div>
     <div class="fld"><label>Processing mode</label><div class="seg" id="wmode"><button data-mode="queue" class="${W.mode==='queue'?'on':''}">Queue</button><button data-mode="inline" class="${W.mode==='inline'?'on':''}" ${inlineOk?'':'disabled title="Inline needs the same Cosmos DB container as source and store, and no chunking"'}>Inline</button></div>
      <span class="hint">${W.mode==='queue' ? 'Scales with the shared worker pool. Supports chunking and every source.' : 'Embeds in place inside the change-feed processor. Lowest latency.'}</span></div></div>` : ''}`;
}
function stepReview() {
  const s = src(), d = dst(), m = mdl();
  if (!W.name && s && d) W.name = `${s.name}-to-${d.name}`.toLowerCase().replace(/[^a-z0-9-]+/g,'-').slice(0, 60);
  return `<div><h2 style="font-size:17px;margin:0">Name it and start</h2><div class="muted">OmniVec saves the pipeline, checks dependencies, backfills existing content, then keeps syncing changes.</div></div>
   <div class="grid g2"><div class="fld"><label>Name</label><input class="inp" id="wname" value="${esc(W.name)}"></div><div class="fld"><label>Description <span class="muted">(optional)</span></label><input class="inp" id="wdesc" value="${esc(W.desc)}"></div></div>
   <label class="check"><input type="checkbox" id="wpe" ${W.processExisting?'checked':''}> Process existing content now <span class="muted">(otherwise only new changes are indexed)</span></label>
   <div class="card" style="box-shadow:none"><div class="card-b"><dl class="kv">
    <dt>Source</dt><dd>${s ? esc(s.name)+' · '+esc(T(s.type).short) : '—'}</dd>
    <dt>Content</dt><dd>${W.strategy==='chunk' ? `Chunks of ${W.chunk.size} ${W.chunk.unit}, ${W.chunk.overlap} overlap` : 'Whole document'} · ${s && ['azure-blob','sharepoint'].includes(s.type) ? esc((W.fileTypes||[]).join(', ')) : 'fields '+esc((W.fields||[]).join(', '))}</dd>
    <dt>Embedding</dt><dd>${m ? esc(m.deployment||m.name)+` · ${m.embedding_dim} dims` : esc(W.recipe||'—')}</dd>
    <dt>Vector store</dt><dd>${d ? esc(d.name)+' · /'+esc(W.vpath||'') : '—'}</dd><dt>Mode</dt><dd>${W.mode}</dd></dl></div></div>
   <details><summary class="link small">Advanced: document identity, collisions, metadata, priority</summary><div class="stack s12" style="margin-top:12px">
    <div class="grid g2"><div class="fld"><label>Document ID pattern</label><input class="inp mono" id="wdid" value="${esc(W.docId)}"></div><div class="fld"><label>Partition key pattern</label><input class="inp mono" id="wpk" value="${esc(W.pk)}"></div>
     ${W.strategy==='chunk' ? `<div class="fld"><label>Chunk ID pattern</label><input class="inp mono" id="wcid" value="${esc(W.chunkId)}"><span class="hint">Must include {chunk}.</span></div>` : ''}
     <div class="fld"><label>On ID collision</label><select class="sel" id="wcol"><option value="reject" ${W.collision==='reject'?'selected':''}>Reject (safer)</option><option value="overwrite" ${W.collision==='overwrite'?'selected':''}>Overwrite</option></select></div></div>
    <div class="row wrap">${W.strategy==='chunk' ? `<label class="check"><input type="checkbox" id="wst" ${W.storeText?'checked':''}> Store chunk text with each vector (needed to show snippets in search)</label>` : ''}</div>
    <div class="fld"><label>Metadata on each vector</label><div class="row wrap">${['pipeline_name','embedding_dims','source_ref'].map(f => `<label class="check"><input type="checkbox" class="wmeta" value="${f}" ${W.meta.includes(f)?'checked':''}> ${f}</label>`).join('')}</div></div>
    <div class="fld" style="max-width:240px"><label>Priority in the shared worker pool</label><select class="sel" id="wpri">${['low','normal','high','critical'].map(x => `<option ${W.priority===x?'selected':''}>${x}</option>`).join('')}</select></div>
    <div><button class="btn sm" id="wprev">${icon('eye' in {}?'':'info','sm')}Preview document IDs</button><div id="wprevout" style="margin-top:8px"></div></div></div></details>`;
}

/* ---------- binding ---------- */
function captureInputs(v) {
  const g = id => $('#'+id, v);
  if (g('wcs')) { W.chunk.size = +g('wcs').value; W.chunk.overlap = +g('wco').value; W.chunk.unit = g('wcu').value; }
  if (g('wspt')) { W.spTenant = g('wspt').value.trim(); W.spClient = g('wspc').value.trim(); }
  if (g('wcm')) W.contentMode = g('wcm').value;
  if (g('wft')) W.fileTypes = $$('[data-tag]', g('wft')).map(b => b.dataset.tag);
  if (g('wcf')) W.fields = $$('[data-tag]', g('wcf')).map(b => b.dataset.tag);
  if (g('wvp')) W.vpath = g('wvp').value; if (g('wvpi')) W.vpath = g('wvpi').value.trim().replace(/^\//,'');
  if (g('wname')) { W.name = g('wname').value; W.desc = g('wdesc').value; W.processExisting = g('wpe').checked; }
  if (g('wdid')) { W.docId = g('wdid').value.trim(); W.pk = g('wpk').value.trim(); W.collision = g('wcol').value; W.priority = g('wpri').value; W.meta = $$('.wmeta', v).filter(x => x.checked).map(x => x.value); }
  if (g('wcid')) W.chunkId = g('wcid').value.trim(); if (g('wst')) W.storeText = g('wst').checked;
}
function bind(v) {
  const body = $('#wbody', v);
  body.oninput = () => { captureInputs(v); drawSide(v); const n = $('#wnext', v); if (n) n.disabled = !canNext(W.step); };
  $$('[data-src]', v).forEach(o => o.onclick = () => { if (W.sourceId !== o.dataset.src) { W.sourceId = o.dataset.src; W.fields = null; if (M.SRC[W.sourceId].type==='sharepoint') W.mode = 'queue'; } draw(v); });
  $$('[data-strat]', v).forEach(o => o.onclick = () => { captureInputs(v); W.strategy = o.dataset.strat; if (W.strategy==='chunk') W.mode = 'queue'; draw(v); });
  $$('[data-model]', v).forEach(o => o.onclick = () => { W.modelId = o.dataset.model; W.recipe = null; draw(v); });
  $$('[data-recipe]', v).forEach(o => o.onclick = () => { W.recipe = o.dataset.recipe; W.modelId = null; draw(v); });
  $$('[data-store]', v).forEach(o => o.onclick = () => { if (W.storeId !== o.dataset.store) { W.storeId = o.dataset.store; W.indexes = null; W.vpath = null; } draw(v); });
  $$('[data-mode]', v).forEach(b => b.onclick = () => { if (b.disabled) return; W.mode = b.dataset.mode; draw(v); });
  $$('[data-addf]', v).forEach(b => b.onclick = () => { captureInputs(v); if (!W.fields.includes(b.dataset.addf)) W.fields.push(b.dataset.addf); draw(v); });
  $$('.tagbox', v).forEach(tb => { const inp = $('input', tb);
    tb.onclick = e => { const b = e.target.closest('[data-tag]'); if (b) { b.remove(); captureInputs(v); drawSide(v); } else inp.focus(); };
    inp.onkeydown = e => { if ((e.key==='Enter' || e.key===',') && inp.value.trim()) { e.preventDefault(); const t = inp.value.trim().replace(/^\./,''); inp.insertAdjacentHTML('beforebegin', `<span class="badge acc" data-tag="${esc(t)}">${esc(t)} ${icon('x','sm')}</span>`); inp.value=''; captureInputs(v); drawSide(v); } }; });
  const smp = $('#wsample', v); if (smp) smp.onclick = async () => { $('#wsampleout', v).innerHTML = spinner('Loading…');
    try { const r = await api(`/api/sources/${encodeURIComponent(W.sourceId)}/sample?limit=5`); W.sample = r.documents || r.samples || r.items || r.blobs || r.files || (Array.isArray(r) ? r : []); captureInputs(v); draw(v); }
    catch (e) { $('#wsampleout', v).innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span> · <a class="link" href="#/connections/source/${esc(encodeURIComponent(W.sourceId||''))}/access">Check access</a>`; } };
  const prev = $('#wprev', v); if (prev) prev.onclick = async () => { captureInputs(v); const out = $('#wprevout', v); out.innerHTML = spinner('Rendering…');
    try { const r = await api('/api/pipelines/identity-preview', { method:'POST', body:{ document_id_pattern:W.docId, partition_key_pattern:W.pk, chunk_id_pattern:W.chunkId, source_ref:'folder/example-document.pdf', source_partition:'tenant-a', source_id:W.sourceId, destination_id:W.storeId, model_id:W.modelId || W.recipe } });
      out.innerHTML = `<dl class="kv">${Object.entries(r).map(([k,val]) => `<dt>${esc(k.replace(/_/g,' '))}</dt><dd class="mono small">${esc(typeof val==='object' ? JSON.stringify(val) : val)}</dd>`).join('')}</dl>`; }
    catch (e) { out.innerHTML = `<div class="inline-err">${esc(e.message)}</div>`; } };
  const back = $('#wback', v); if (back) back.onclick = () => { captureInputs(v); W.step--; draw(v); };
  const next = $('#wnext', v); if (next) next.onclick = () => { captureInputs(v); if (!canNext(W.step)) return; W.step++; draw(v); $('#view').scrollTop = 0; };
  const cr = $('#wcreate', v); if (cr) cr.onclick = () => create(v, cr);
}
async function create(v, btn) {
  captureInputs(v); const err = $('#werr', v); err.textContent = '';
  if (!W.name.trim()) { err.textContent = 'Give the pipeline a name.'; return; }
  const bad = [0,1,2,3].find(i => !canNext(i)); if (bad !== undefined) { W.step = bad; draw(v); $('#werr', v).textContent = 'Complete this step first.'; return; }
  const s = src();
  const entry = { source_id:W.sourceId, filters:{}, content_fields:(W.fields && W.fields.length ? W.fields : ['content']), content_mode:W.contentMode };
  if (W.fileTypes && W.fileTypes.length) entry.file_types = W.fileTypes;
  if (s.type==='sharepoint' && (W.spTenant || W.spClient)) { if (!W.spTenant || !W.spClient) { err.textContent = 'Tenant ID and client ID must be set together.'; return; } entry.sharepoint_identity = { tenant_id:W.spTenant, client_id:W.spClient }; }
  const body = { name:W.name.trim(), description:W.desc, sources:[entry], docgrok_pipeline:W.modelId || W.recipe, destination_id:W.storeId, vector_index_path:W.vpath,
    process_existing:W.processExisting, processing_mode:W.mode, content_strategy:W.strategy, doc_id_pattern:W.docId, partition_key_pattern:W.pk, collision_policy:W.collision,
    resource_policy:{ weight:10, max_concurrency_per_worker:2, priority:W.priority, workload_class:'shared' } };
  if (W.meta.length !== 3) body.metadata_fields = W.meta;
  if (W.strategy==='chunk') body.chunk_config = { chunk_size:W.chunk.size, chunk_overlap:W.chunk.overlap, chunk_unit:W.chunk.unit, store_text:W.storeText, text_field:'text', doc_id_pattern:W.chunkId };
  btn.disabled = true; btn.innerHTML = spinner('Creating…');
  try { const r = await api('/api/pipelines', { method:'POST', body }); const p = (r && (r.pipeline || r)) || {};
    toast('Pipeline created. Backfill is starting.'); invalidate('pipelines','health','insights'); W = null; go('#/pipelines/'+(p.id || '')); }
  catch (e) { err.textContent = e.message; btn.disabled = false; btn.innerHTML = icon('play','sm')+'Create and start'; }
}
})();
