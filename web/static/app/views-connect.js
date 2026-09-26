/* OmniVec console: Connections (sources + vector stores): list, detail, guided add/edit */
(() => {
const X = window.OVX;
const { icon, esc, fmt, ago, plural, api, need, invalidate, D, M, T, logo, dot, empty, loadErr, codebox, spinner, route, go, render, toast, confirmAction, menu, openDrawer, $, $$ } = X;

/* ---------- connector form definitions ---------- */
const F = (k, label, o = {}) => ({ k, label, ...o });
const FORMS = {
  source: {
    'cosmosdb': { blurb:'Operational JSON documents. Changes stream in through the change feed.', fields:[
      F('endpoint','Account endpoint',{ req:1, ph:'https://<account>.documents.azure.com:443/' }), F('database','Database',{ req:1 }), F('container','Container',{ req:1 }),
      F('query','Initial query',{ def:'SELECT * FROM c', adv:1, hint:'Used for the first backfill. Later changes come from the change feed.' }) ], extra:{ use_change_feed:true } },
    'azure-blob': { blurb:'Files such as PDF, Word, text and Markdown. New uploads arrive through Event Grid.', fields:[
      F('account_url','Storage account URL',{ req:1, ph:'https://<account>.blob.core.windows.net' }), F('container','Container',{ req:1 }), F('prefix','Folder prefix',{ ph:'docs/ (optional)', hint:'Only blobs under this prefix are indexed.' }) ] },
    'sharepoint': { blurb:'Document libraries in SharePoint Online, read through Microsoft Graph.', fields:[
      F('site_id','Site ID',{ req:1, ph:'contoso.sharepoint.com,<site-guid>,<web-guid>', hint:'Graph: GET /sites/{hostname}:/sites/{path}?$select=id' }), F('drive_id','Document library (drive) ID',{ req:1, ph:'b!…' }),
      F('folder_path','Folder',{ ph:'Shared Documents/Policies (optional)' }), F('file_types','File types',{ type:'tags', def:['pdf','docx','txt','md'] }),
      F('poll_interval_seconds','Check for changes every (seconds)',{ type:'number', def:60, adv:1 }), F('max_file_size_mib','Max file size (MiB)',{ type:'number', def:50, adv:1 }) ],
      build: v => ({ site_id:v.site_id, drive_id:v.drive_id, folder_path:v.folder_path||'', file_types:v.file_types, poll_interval_seconds:+v.poll_interval_seconds||60, max_file_size_bytes:(+v.max_file_size_mib||50)*1048576, auth_type:'managed-identity' }),
      load: c => ({ ...c, max_file_size_mib: c.max_file_size_bytes ? Math.round(c.max_file_size_bytes/1048576) : 50 }) },
    'postgresql': { blurb:'Rows from a PostgreSQL table, polled by timestamp.', fields:[
      F('host','Host',{ req:1, ph:'<server>.postgres.database.azure.com' }), F('port','Port',{ type:'number', def:5432 }), F('database','Database',{ req:1 }), F('table','Table',{ req:1, ph:'public.documents' }),
      F('id_column','ID column',{ def:'id' }), F('timestamp_column','Updated-at column',{ def:'updated_at' }), F('user','User',{ req:1 }), F('password','Password',{ type:'password' }),
      F('ssl_mode','SSL mode',{ type:'select', opts:['require','verify-full','prefer','disable'], def:'require', adv:1 }), F('poll_interval_seconds','Poll every (seconds)',{ type:'number', def:60, adv:1 }) ] },
    'onelake-iceberg': { blurb:'Apache Iceberg tables in a Microsoft Fabric lakehouse.', fields:[
      F('warehouse','Workspace / lakehouse',{ req:1, ph:'<workspace-id>/<lakehouse-id>' }), F('namespace','Namespace',{ def:'dbo' }), F('table','Table',{ req:1 }),
      F('content_fields','Content fields',{ type:'tags', def:['content'] }), F('id_field','ID field',{ def:'id' }), F('poll_interval_seconds','Poll every (seconds)',{ type:'number', def:60, adv:1 }) ] },
  },
  destination: {
    'cosmosdb-vector': { blurb:'Vectors stored next to your data with DiskANN or quantized flat indexes.', fields:[
      F('endpoint','Account endpoint',{ req:1, ph:'https://<account>.documents.azure.com:443/' }), F('database','Database',{ req:1 }), F('container','Container',{ req:1, hint:'Must have a vector embedding policy and vector index.' }) ], extra:{ auth_type:'managed-identity' } },
    'pgvector': { blurb:'PostgreSQL with the pgvector extension (HNSW or IVFFlat).', fields:[
      F('host','Host',{ req:1 }), F('port','Port',{ type:'number', def:5432 }), F('database','Database',{ req:1 }), F('table','Table',{ req:1, ph:'public.embeddings' }),
      F('vector_dimensions','Dimensions',{ type:'number', def:1536, hint:'Must match the embedding model.' }), F('index_type','Index',{ type:'select', opts:['hnsw','ivfflat'], def:'hnsw' }),
      F('user','User',{ req:1 }), F('password','Password',{ type:'password' }),
      F('vector_column','Vector column',{ def:'embedding', adv:1 }), F('content_column','Content column',{ def:'content', adv:1 }), F('id_column','ID column',{ def:'id', adv:1 }), F('ssl_mode','SSL mode',{ type:'select', opts:['require','verify-full','prefer','disable'], def:'require', adv:1 }) ] },
    'onelake-iceberg': { blurb:'Iceberg tables in OneLake, merged by a Spark job, with an optional Garnet mirror for low-latency search.', fields:[
      F('workspace_id','Workspace ID',{ req:1 }), F('lakehouse_item_id','Lakehouse item ID',{ req:1 }), F('spark_job_definition_item_id','Spark Job Definition item ID',{ req:1 }),
      F('staging_file_system','Staging file system',{ req:1, ph:'<workspace-id>' }), F('staging_path','Staging path',{ ph:'<lakehouse-id>/Files/omnivec/staging' }), F('target_table','Target table',{ req:1 }),
      F('garnet_endpoint','Garnet endpoint (optional mirror)',{ adv:1 }), F('garnet_vector_set','Garnet vector set',{ def:'omnivec-vectors', adv:1 }) ],
      build: v => { const c = { workspace_id:v.workspace_id, lakehouse_item_id:v.lakehouse_item_id, spark_job_definition_item_id:v.spark_job_definition_item_id, staging_file_system:v.staging_file_system, staging_path:v.staging_path || `${v.lakehouse_item_id}/Files/omnivec/staging`, target_table:v.target_table };
        if (v.garnet_endpoint) c.mirror = { type:'garnet', best_effort:false, config:{ endpoint:v.garnet_endpoint, vector_set:v.garnet_vector_set||'omnivec-vectors', tls:true, use_entra_auth:true } }; return c; },
      load: c => ({ ...c, garnet_endpoint: c.mirror && c.mirror.config && c.mirror.config.endpoint, garnet_vector_set: c.mirror && c.mirror.config && c.mirror.config.vector_set }) },
  },
};
const kindOf = k => k==='store' ? 'destination' : 'source';
const listOf = kind => kind==='source' ? M.sources : M.dests;
const healthOf = (kind, id) => (kind==='source' ? M.hs : M.hd)[id];
const hdot = h => dot(!h ? '' : h.status==='healthy' ? 'ok' : h.status==='warning' ? 'warn' : 'err');
const keyInfo = (x) => { const c = x.config||{};
  return x.type==='azure-blob' ? `${c.container||'—'}${c.prefix?' / '+c.prefix:''}` : x.type==='sharepoint' ? (c.folder_path||'Document library') : x.type==='onelake-iceberg' ? (c.table||c.target_table||'—')
    : c.container ? `${c.database||''} / ${c.container}` : c.table ? `${c.database||''} / ${c.table}` : '—'; };

/* ---------- list ---------- */
route('connections', { needs:[...X.CORE, 'caps'], render(parts) {
  if (parts[0]==='new') return addPage(kindOf(parts[1]));
  if (parts[0]==='source' || parts[0]==='store') return parts[2]==='edit' ? addPage(kindOf(parts[0]), parts[1]) : detail(kindOf(parts[0]), parts[1], parts[2]);
  const tab = parts[0]==='stores' ? 'stores' : 'sources'; const kind = tab==='stores' ? 'destination' : 'source'; const list = listOf(kind);
  const qp = X.query(); const seg = qp.get('show') || 'all', q = (qp.get('q') || '').toLowerCase();
  const probOf = x => { const h = healthOf(kind, x.id), t = M.trig[x.id];
    return x.enabled===false ? ['warn','Disabled'] : h && h.status!=='healthy' ? [/unhealthy|error/.test(h.status)?'err':'warn', X.humanCheck(((h.checks||[]).find(k => k.status==='fail'||k.status==='warn')||{}).check||'check')+' failed'] : t && t.status==='not_configured' ? ['warn','No Event Grid trigger'] : null; };
  const attn = list.filter(probOf);
  const rows = list.filter(x => (seg==='all' || (seg==='attention' ? probOf(x) : x.type===seg)) && (!q || (x.name+' '+keyInfo(x)+' '+x.type).toLowerCase().includes(q)))
    .sort((a,b) => (!!probOf(b) - !!probOf(a)) || a.name.localeCompare(b.name));
  const types = [...new Set(list.map(x => x.type))];
  const nav = (k, v) => { const p = new URLSearchParams(qp); v ? p.set(k, v) : p.delete(k); return `#/connections/${tab}?${p}`; };
  const path = kind==='source' ? 'source' : 'store';
  const change = x => { const t = M.trig[x.id];
    return x.type==='cosmosdb' ? 'Change feed' : x.type==='azure-blob' ? (t && t.status==='not_configured' ? `<span class="badge warn">${icon('bolt','sm')}No trigger</span>` : 'Event Grid') : x.type==='sharepoint' ? 'Delta polling' : kind==='destination' ? '—' : 'Scheduled sync'; };
  const tbl = rows.length ? `<table class="tbl"><thead><tr><th>Name</th><th>Location</th><th>Health</th>${kind==='source' ? '<th>Change detection</th>' : '<th>Index</th>'}<th class="r">Used by</th><th>Checked</th><th></th></tr></thead><tbody>
    ${rows.map(x => { const h = healthOf(kind, x.id), used = X.usedBy(x.id), pr = probOf(x); const vi = ((x.config||{}).vector_indexes||[])[0];
      return `<tr class="clickrow" data-href="#/connections/${path}/${x.id}"><td><div class="row" style="gap:10px">${logo(x.type)}<div style="min-width:0"><div class="nm">${esc(x.name)}</div><div class="muted small">${esc(T(x.type).short)}</div></div></div></td>
       <td class="mono small muted">${esc(keyInfo(x))}</td>
       <td>${pr ? `<span class="badge ${pr[0]}">${X.dot(pr[0])}${esc(pr[1])}</span>` : h ? `<span class="badge ok">${X.dot('ok')}Healthy</span>` : '<span class="badge out">Not checked</span>'}</td>
       <td class="small">${kind==='source' ? change(x) : vi ? `${esc(vi.indexType||'')} · ${esc(vi.dimensions||'—')}d` : '<span class="muted">—</span>'}</td>
       <td class="r num">${used.length ? plural(used.length,'pipeline') : '<span class="muted">Unused</span>'}</td><td class="small muted">${h ? ago(h.checked_at) : '—'}</td>
       <td class="r"><button class="btn ghost sm icon" data-rm="${x.id}">${icon('more','sm')}</button></td></tr>`; }).join('')}</tbody></table>`
    : empty('search', 'No matches', 'Try another filter.');
  const firstRun = !list.length;
  const tiles = (kind==='source' ? ['cosmosdb','azure-blob','sharepoint','postgresql','onelake-iceberg','mssql'] : ['cosmosdb-vector','pgvector','onelake-iceberg'])
    .map(t => `<a class="opt" href="#/connections/new/${path}?type=${t}">${logo(t)}<div><div class="t">${esc(T(t).label)}</div><div class="d">Guided setup with a permissions check</div></div></a>`).join('');
  const html = `<div class="page">
   <div class="ph"><div><h1>Connections</h1><p>Sources are where content comes from. Vector stores are where embeddings are written and searched. Each can be reused by many pipelines.</p></div>
    <div class="acts"><a class="btn ${tab==='sources'?'pri':''}" href="#/connections/new/source">${icon('plug')}Add source</a><a class="btn ${tab==='stores'?'pri':''}" href="#/connections/new/store">${icon('db')}Add vector store</a></div></div>
   ${loadErr(['sources','dests','health'])}
   <div class="tabs"><a href="#/connections/sources" class="${tab==='sources'?'on':''}">${icon('plug','sm')}Sources <span class="ct">${M.sources.length}</span></a><a href="#/connections/stores" class="${tab==='stores'?'on':''}">${icon('db','sm')}Vector stores <span class="ct">${M.dests.length}</span></a></div>
   ${firstRun ? `<div class="card"><div class="card-b stack s12"><div><b>Connect your first ${kind==='source' ? 'source' : 'vector store'}</b><div class="muted small">OmniVec uses its managed identity, so no keys are stored for Azure services.</div></div><div class="grid g3">${tiles}</div></div></div>` : `
   <div class="row sp wrap" style="margin-bottom:12px;gap:10px"><div class="seg"><button data-nav="${nav('show','')}" class="${seg==='all'?'on':''}">All<span class="ct">${list.length}</span></button><button data-nav="${nav('show','attention')}" class="${seg==='attention'?'on':''}">Needs attention<span class="ct">${attn.length}</span></button>
     ${types.length > 1 ? types.map(t => `<button data-nav="${nav('show',t)}" class="${seg===t?'on':''}">${esc(T(t).short)}<span class="ct">${list.filter(x=>x.type===t).length}</span></button>`).join('') : ''}</div>
    <div class="search-inp" style="width:260px">${icon('search','sm')}<input class="inp" id="cq" placeholder="Filter by name or location" value="${esc(qp.get('q')||'')}"></div></div>
   <div class="card">${tbl}</div>`}
  </div>`;
  return { crumbs:[['Connections']], html, mount(v) {
    $$('[data-nav]', v).forEach(b => b.onclick = () => go(b.dataset.nav));
    const cq = $('#cq', v); if (cq) { let t; cq.oninput = () => { clearTimeout(t); t = setTimeout(() => { history.replaceState(null, '', nav('q', cq.value.trim())); X.render({ silent:true }).then(() => { const n = $('#cq'); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } }); }, 250); }; }
    $$('tr[data-href]', v).forEach(r => r.onclick = e => { if (!e.target.closest('button')) go(r.dataset.href); });
    $$('[data-rm]', v).forEach(b => b.onclick = e => { e.stopPropagation(); const id = b.dataset.rm, base = `#/connections/${path}/${id}`;
      X.menu(b, [{ label:'Open', icon:'chev', run:() => go(base) }, { label:'Access & permissions', icon:'shield', run:() => go(base+'/access') }, { label:'Edit', icon:'edit', run:() => go(base+'/edit') },
        { label:'New pipeline from this', icon:'plus', run:() => go(`#/new?${kind==='source'?'source':'store'}=${id}`) }]); });
  } };
}});
/* ---------- detail ---------- */
function detail(kind, id, section) {
  const x = (kind==='source' ? M.SRC : M.DST)[id]; const noun = kind==='source' ? 'source' : 'vector store';
  if (!x) return { crumbs:[['Connections','#/connections'],['Not found']], html:`<div class="page">${empty('plug',`This ${noun} was not found`,'It may have been deleted.','<a class="btn" href="#/connections">Back to connections</a>')}</div>` };
  const h = healthOf(kind, x.id), used = X.usedBy(x.id), c = x.config||{}, t = M.trig[x.id];
  const base = `#/connections/${kind==='source'?'source':'store'}/${x.id}`;
  const cfgRows = Object.entries(c).filter(([k,v]) => v!=null && v!=='' && typeof v !== 'object').map(([k,v]) => `<dt>${esc(k.replace(/_/g,' '))}</dt><dd class="mono small">${esc(/password|secret|key$/i.test(k) ? '••••••' : v)}</dd>`).join('');
  const vi = (c.vector_indexes||[])[0]; const pol = h && (h.checks||[]).find(k => k.check==='vector_policy');
  const html = `<div class="page">
   <div class="ph"><div style="min-width:0"><div class="row">${logo(x.type,'lg')}<div style="min-width:0"><div class="row"><h1 class="trunc">${esc(x.name)}</h1>${h ? `<span class="badge ${h.status==='healthy'?'ok':'warn'}">${hdot(h)}${h.status==='healthy'?'Healthy':esc(h.status)}</span>` : '<span class="badge out">Not checked</span>'}</div>
     <p class="mono small">${x.id} · ${esc(T(x.type).label)} · created ${ago(x.created_at)}</p></div></div></div>
    <div class="acts"><button class="btn" id="ctest">${icon('bolt')}Test connection</button><a class="btn" href="${base}/edit">${icon('edit')}Edit</a><button class="btn icon" id="cmore">${icon('more')}</button></div></div>
   <div id="ctestout"></div>
   <div class="grid g-main" style="align-items:start"><div class="stack">
    <div class="card" id="perm"><div class="card-h"><h3>${icon('shield','sm')} Access &amp; permissions</h3><span class="muted small" style="margin-left:auto">What OmniVec needs, and a check that it has it</span></div><div class="card-b" id="permb"></div></div>
    ${kind==='source' && x.type==='azure-blob' ? `<div class="card" id="trigger"><div class="card-h"><h3>${icon('bolt','sm')} Automatic ingestion</h3>${t ? `<span class="badge ${t.status==='not_configured'?'warn':'ok'}" style="margin-left:auto">${t.status==='not_configured'?'Not configured':esc(t.status)}</span>` : ''}</div><div class="card-b stack s12">
      ${t && t.status==='not_configured' ? `<div class="callout warn">${icon('info','ic')}<div class="grow"><b>New uploads are not picked up automatically</b>Existing blobs were processed when the pipeline started. To ingest new and changed files as they arrive, subscribe the storage account to BlobCreated and BlobDeleted events. Until then, use <b>Sync now</b>.</div></div>
        <div class="row"><button class="btn pri" id="egcreate">${icon('bolt','sm')}Create Event Grid subscription</button><button class="btn" id="csync">${icon('refresh','sm')}Sync now</button></div>` : `<div class="muted">Event Grid delivers new and changed files to OmniVec as they arrive.</div><div class="row"><button class="btn" id="csync">${icon('refresh','sm')}Sync now</button></div>`}
      <div id="egout"></div></div></div>` : ''}
    ${kind==='source' && x.type!=='azure-blob' ? `<div class="card"><div class="card-h"><h3>${icon('refresh','sm')} Change detection</h3><span class="muted small" style="margin-left:auto">${esc(x.type==='cosmosdb' ? (t ? 'Change feed · '+t.status.replace(/_/g,' ') : 'Change feed') : 'Polling every '+(c.poll_interval_seconds||60)+'s')}</span></div>
      <div class="card-b row sp"><span class="muted">${x.type==='cosmosdb' ? 'Inserts and updates flow automatically through the Cosmos DB change feed.' : 'OmniVec checks for new and changed items on a schedule.'}</span><button class="btn" id="csync">${icon('refresh','sm')}Sync now</button></div></div>` : ''}
    <div class="card"><div class="card-h"><h3>Used by</h3><span class="badge">${used.length}</span>${kind==='source' ? `<a class="btn sm" style="margin-left:auto" href="#/new?source=${x.id}">${icon('plus','sm')}New pipeline from this source</a>` : `<a class="btn sm" style="margin-left:auto" href="#/search?store=${x.id}">${icon('search','sm')}Search this store</a>`}</div>
     ${used.length ? `<table class="tbl"><tbody>${used.map(p => `<tr class="clickrow" onclick="location.hash='#/pipelines/${p.id}'"><td><div class="nm">${esc(p.name)}</div><div class="id">${p.id}</div></td><td>${X.statusBadge(p)}</td><td class="r num">${p.statsLoading ? '<span class="skel" style="display:inline-block;width:44px;height:12px;vertical-align:middle"></span>' : fmt(p.vectors)} vectors</td><td class="muted">${ago(p.lastWrite)}</td></tr>`).join('')}</tbody></table>` : `<div class="card-b muted">No pipelines use this ${noun} yet.</div>`}</div>
    <div class="card"><div class="card-h"><h3>Sample ${kind==='source'?'documents':'vectors'}</h3><button class="btn sm" id="csample" style="margin-left:auto">${icon('file','sm')}Load sample</button></div><div class="card-b" id="csampleout"><div class="muted small">Preview a few items to confirm the right data is connected.</div></div></div>
   </div><div class="stack">
    <div class="card"><div class="card-h"><h3>Health checks</h3><span class="muted small" style="margin-left:auto">${h ? ago(h.checked_at) : ''}</span></div><div class="card-b stack s8">${h ? (h.checks||[]).map(k => `<div class="chk-row">${X.chkIcon(k.status)}<div><b style="font-weight:600">${esc(X.humanCheck(k.check))}</b> <span class="muted">${esc(k.detail||'')}</span></div></div>`).join('') : '<div class="muted">Not checked yet.</div>'}</div></div>
    ${kind==='destination' ? `<div class="card"><div class="card-h"><h3>Vector index</h3></div><div class="card-b"><dl class="kv">${vi ? `<dt>Path</dt><dd class="mono">${esc(vi.path)}</dd><dt>Index</dt><dd>${esc(vi.indexType)}</dd><dt>Dimensions</dt><dd>${esc(vi.dimensions)}</dd><dt>Distance</dt><dd>${esc(vi.distanceFunction||'—')}</dd>` : pol ? `<dt>Field</dt><dd class="mono">${esc(pol.vector_field||'—')}</dd><dt>Dimensions</dt><dd>${esc(pol.dimensions||'—')}</dd>` : x.type==='pgvector' ? `<dt>Column</dt><dd class="mono">${esc(c.vector_column||'embedding')}</dd><dt>Dimensions</dt><dd>${esc(c.vector_dimensions||'—')}</dd><dt>Index</dt><dd>${esc(c.index_type||'—')}</dd>` : '<dt>Index</dt><dd>Not reported</dd>'}</dl></div></div>` : ''}
    <div class="card"><div class="card-h"><h3>Configuration</h3></div><div class="card-b"><dl class="kv">${cfgRows || '<dt>—</dt><dd></dd>'}</dl></div></div>
   </div></div></div>`;
  return { crumbs:[['Connections','#/connections/'+(kind==='source'?'sources':'stores')],[x.name]], html, mount(v) {
    X.perm.permAssistant($('#permb', v), { kind, type:x.type, resourceRef:x.id, getConfig:() => c });
    if (section==='trigger' && $('#trigger', v)) setTimeout(() => $('#trigger', v).scrollIntoView({ behavior:'smooth' }), 50);
    if (section==='access') setTimeout(() => $('#perm', v).scrollIntoView({ behavior:'smooth' }), 50);
    $('#ctest', v).onclick = async e => { const b = e.currentTarget; b.disabled = true; b.innerHTML = spinner('Testing…'); const out = $('#ctestout', v);
      try { const r = await api(`/api/${kind==='source'?'sources':'destinations'}/${x.id}/test`, { method:'POST' });
        const ok = r.success !== false; const res = r.result || r; const msg = typeof res === 'string' ? res : res.message || res.error || r.error || (ok ? 'Connection works.' : 'Connection failed.');
        out.innerHTML = `<div class="callout ${ok?'ok':'err'}" style="margin-bottom:16px">${icon(ok?'check':'alert','ic')}<div class="grow"><b>${ok?'Connection works':'Connection failed'}</b>${esc(msg)}${res && res.details ? ' · '+esc(typeof res.details==='string'?res.details:JSON.stringify(res.details)) : ''}${ok ? '' : '<div style="margin-top:6px">This is usually a permission or network issue. The access check below explains what is needed.</div>'}</div></div>`;
        if (!ok) $('#perm', v).scrollIntoView({ behavior:'smooth' }); }
      catch (err) { out.innerHTML = `<div class="callout err" style="margin-bottom:16px">${icon('alert','ic')}<div><b>Test failed</b>${esc(err.message)}</div></div>`; }
      b.disabled = false; b.innerHTML = icon('bolt')+'Test connection'; };
    $('#cmore', v).onclick = e => { e.stopPropagation(); menu(e.currentTarget, [
      { label:'Edit', icon:'edit', run:() => go(base+'/edit') },
      { label:'Ask agent about it', icon:'sparkle', run:() => X.openAgent({}, `Check the ${noun} "${x.name}" (${x.id}). Is it healthy and does OmniVec have the access it needs?`) },
      '-', ...(kind==='source' ? [{ label:'Delete all vectors from this source…', icon:'trash', danger:true, run:() => confirmAction({ title:'Delete vectors from this source?', danger:true, typed:x.name, confirm:'Delete vectors',
        body:`Removes every vector that came from <b>${esc(x.name)}</b> in all vector stores. Pipelines keep running and will re-create vectors for new changes.`,
        onOk: async () => { const r = await api(`/api/sources/${x.id}/vectors`, { method:'DELETE' }); toast((r && r.message) || 'Vectors deleted'); } }) }] : []),
      { label:`Delete ${noun}…`, icon:'trash', danger:true, run:() => confirmAction({ title:`Delete ${noun}?`, danger:true, typed:x.name, confirm:'Delete',
        body: used.length ? `<div class="callout warn">${icon('alert','ic')}<div>${plural(used.length,'pipeline')} still use this ${noun}. Delete or change ${used.length===1?'it':'them'} first.</div></div>` : `Removes the connection. Data in the underlying resource is not touched.`,
        onOk: async () => { await api(`/api/${kind==='source'?'sources':'destinations'}/${x.id}`, { method:'DELETE' }); toast(`${noun[0].toUpperCase()+noun.slice(1)} deleted`); invalidate('sources','dests'); go('#/connections/'+(kind==='source'?'sources':'stores')); } }) } ]); };
    const sync = $('#csync', v); if (sync) sync.onclick = async () => { sync.disabled = true; sync.innerHTML = spinner('Starting…');
      try { const r = await api(`/api/sources/${x.id}/sync`, { method:'POST', body:{ full_sync:false } }); toast(r.message || 'Sync started'); } catch (err) { toast(err.message, 'err'); }
      sync.disabled = false; sync.innerHTML = icon('refresh','sm')+'Sync now'; };
    const eg = $('#egcreate', v); if (eg) eg.onclick = async () => { eg.disabled = true; eg.innerHTML = spinner('Creating subscription…'); const out = $('#egout', v);
      try { const r = await api('/api/triggers/eventgrid/create', { method:'POST', body:{ source_id:x.id } });
        if (r.success) { out.innerHTML = `<div class="callout ok">${icon('check','ic')}<div><b>Event Grid subscription created</b>Storage account ${esc(r.storage_account||'')} · container ${esc(r.container||'')}. New uploads now start ingestion automatically.</div></div>`; invalidate('triggers'); }
        else out.innerHTML = egManual(r, x); }
      catch (err) { out.innerHTML = egManual({ error: err.message }, x); }
      eg.disabled = false; eg.innerHTML = icon('bolt','sm')+'Create Event Grid subscription'; };
    const smp = $('#csample', v); smp.onclick = async () => { const out = $('#csampleout', v); out.innerHTML = spinner('Loading…');
      try { const r = await api(`/api/${kind==='source'?'sources':'destinations'}/${x.id}/sample?limit=5`); const items = r.documents || r.samples || r.items || r.blobs || r.files || (Array.isArray(r) ? r : []);
        out.innerHTML = items.length ? items.map(d => `<div class="code" style="margin-bottom:8px;max-height:160px;white-space:pre-wrap">${esc(JSON.stringify(stripVec(d), null, 2).slice(0, 1200))}</div>`).join('') : `<div class="muted">No items returned.${r.message ? ' '+esc(r.message) : ''}</div>`; }
      catch (err) { out.innerHTML = `<div class="callout warn">${icon('alert','ic')}<div>${esc(err.message)}</div></div>`; } };
  } };
}
const stripVec = d => { if (!d || typeof d !== 'object') return d; const o = {}; Object.entries(d).forEach(([k,v]) => { o[k] = Array.isArray(v) && v.length > 16 && typeof v[0]==='number' ? `[${v.length} floats]` : (k.startsWith('_') ? undefined : v); }); return o; };
function egManual(r, x) {
  const c = x.config||{}; const acctName = (() => { try { return new URL(c.account_url).hostname.split('.')[0]; } catch { return '<account>'; } })();
  const ms = r.manual_setup || {};
  const cmd = ms.command || ms.cli || `az eventgrid event-subscription create \\\n  --name omnivec-${x.id} \\\n  --source-resource-id "$(az storage account show -n ${acctName} --query id -o tsv)" \\\n  --endpoint-type ${ms.endpoint_type || 'servicebusqueue'} \\\n  --endpoint "${ms.endpoint || ms.webhook_url || '<omnivec-event-endpoint>'}" \\\n  --included-event-types Microsoft.Storage.BlobCreated Microsoft.Storage.BlobDeleted \\\n  --subject-begins-with "/blobServices/default/containers/${c.container||'<container>'}/"`;
  return `<div class="callout warn">${icon('info','ic')}<div class="grow"><b>OmniVec could not create the subscription automatically</b>${esc(r.error||'')}${/SUBSCRIPTION_ID/.test(r.error||'') ? '<div style="margin-top:4px">The API does not know which Azure subscription to use. An administrator can set <span class="mono">AZURE_SUBSCRIPTION_ID</span> on the omnivec-api deployment, or create the subscription manually:</div>' : '<div style="margin-top:4px">An administrator can create it manually:</div>'}</div></div>
   <div style="margin-top:10px">${codebox(cmd)}</div><div class="muted small" style="margin-top:6px">Requires <b>EventGrid Contributor</b> on the storage account. Delivery to Service Bus also needs the OmniVec identity to hold <b>Azure Service Bus Data Sender</b> on the queue's namespace.</div>`;
}

/* ---------- add / edit ---------- */
function addPage(kind, editId) {
  const noun = kind==='source' ? 'source' : 'vector store';
  const existing = editId ? (kind==='source' ? M.SRC : M.DST)[editId] : null;
  const caps = (D.caps || {}).allowed_source_types;
  const types = Object.keys(FORMS[kind]).filter(t => kind!=='source' || !caps || caps.includes(t));
  const qp = X.query(); const then = qp.get('then');
  const st = { type: existing ? existing.type : qp.get('type') || null, tested:null, perm:null };
  const html = `<div class="page"><div class="ph"><div><h1>${existing ? 'Edit '+esc(existing.name) : 'Add '+noun}</h1><p>${kind==='source' ? 'Connect where your content lives. OmniVec reads it with its managed identity, so no keys are stored for Azure services.' : 'Choose where embeddings are written. Pipelines can share one vector store.'}</p></div></div>
   <div class="grid" style="grid-template-columns:minmax(0,1fr) 380px;align-items:start">
    <div class="stack" id="aform"></div>
    <div class="stack" style="position:sticky;top:12px"><div class="card"><div class="card-h"><h3>${icon('shield','sm')} Access OmniVec needs</h3></div><div class="card-b" id="aguide"><div class="muted">Pick a type to see exactly which role is needed and where.</div></div></div>
     <div class="card"><div class="card-h"><h3>Before you create</h3></div><div class="card-b stack s8 small" id="achecks"></div></div></div>
   </div></div>`;
  return { crumbs:[['Connections','#/connections/'+(kind==='source'?'sources':'stores')],[existing ? 'Edit' : 'Add '+noun]], html, live:false, mount(v) {
    const vals = existing ? { name:existing.name, ...(FORMS[kind][existing.type] && FORMS[kind][existing.type].load ? FORMS[kind][existing.type].load(existing.config||{}) : existing.config||{}) } : {};
    const drawChecks = () => { $('#achecks', v).innerHTML = [
      [!!st.type, 'Type chosen'], [st.type && required().every(f => (read()[f.k]??'')!==''), 'Required fields filled'],
      [st.tested===true, st.tested===false ? 'Connection test failed' : 'Connection tested'], [st.perm && st.perm.status==='read_verified', st.perm ? 'Access: '+((X.perm.STATUS[st.perm.status]||[])[2]||st.perm.status) : 'Access checked (recommended)'],
    ].map(([ok, t]) => `<div class="chk-row">${ok ? X.chkIcon('pass') : `<span style="color:var(--faint);display:inline-flex">${icon('clock','sm')}</span>`}<span>${esc(t)}</span></div>`).join(''); };
    const def = () => FORMS[kind][st.type]; const required = () => st.type ? def().fields.filter(f => f.req) : [];
    const read = () => { const o = {}; if (!st.type) return o; def().fields.forEach(f => { const el = $(`[data-k="${f.k}"]`, v); if (!el) return;
      o[f.k] = f.type==='tags' ? $$('.badge[data-tag]', el).map(b => b.dataset.tag) : f.type==='number' ? (el.value==='' ? '' : +el.value) : el.value.trim(); }); return o; };
    const config = () => { const r = read(); if (def().build) return def().build(r); const c = {}; Object.entries(r).forEach(([k,val]) => { if (val!=='' && val!=null) c[k] = val; }); return { ...(existing ? existing.config : {}), ...c, ...(def().extra||{}) }; };
    const fieldHtml = f => { const val = vals[f.k] ?? f.def ?? '';
      const ctl = f.type==='select' ? `<select class="sel" data-k="${f.k}">${f.opts.map(o => `<option ${String(val)===o?'selected':''}>${o}</option>`).join('')}</select>`
        : f.type==='tags' ? `<div class="tagbox" data-k="${f.k}">${(Array.isArray(val)?val:String(val).split(',')).filter(Boolean).map(t => `<span class="badge acc" data-tag="${esc(t)}">${esc(t)} ${icon('x','sm')}</span>`).join('')}<input placeholder="Add and press Enter"></div>`
        : `<input class="inp ${/id$|url|endpoint|host/.test(f.k)?'mono':''}" data-k="${f.k}" type="${f.type==='password'?'password':f.type==='number'?'number':'text'}" value="${esc(f.type==='password' && existing ? '***' : val)}" placeholder="${esc(f.ph||'')}" autocomplete="off">`;
      return `<div class="fld"><label>${esc(f.label)}${f.req?'':' <span class="muted">(optional)</span>'}</label>${ctl}${f.hint?`<span class="hint">${esc(f.hint)}</span>`:''}</div>`; };
    const draw = () => {
      const out = $('#aform', v);
      out.innerHTML = `${existing ? '' : `<div class="card"><div class="card-h"><h3><span class="badge acc">1</span> Type</h3></div><div class="card-b grid g2">${types.map(t => `<div class="opt ${st.type===t?'on':''}" data-type="${t}"><span class="tick">${icon('check','sm')}</span>${logo(t)}<div><div class="t">${esc(T(t).label)}</div><div class="d">${esc(FORMS[kind][t].blurb)}</div></div></div>`).join('')}</div></div>`}
       ${st.type ? `<div class="card"><div class="card-h"><h3>${existing?'':'<span class="badge acc">2</span> '}Connection details</h3></div><div class="card-b stack s12">
         <div class="fld"><label>Display name</label><input class="inp" data-name value="${esc(vals.name || '')}" placeholder="e.g. ${kind==='source'?'Support articles':'Support vectors'}"></div>
         <div class="grid g2">${def().fields.filter(f => !f.adv).map(fieldHtml).join('')}</div>
         ${def().fields.some(f => f.adv) ? `<details><summary class="link small">Advanced settings</summary><div class="grid g2" style="margin-top:10px">${def().fields.filter(f => f.adv).map(fieldHtml).join('')}</div></details>` : ''}</div></div>
        <div class="card"><div class="card-h"><h3>${existing?'':'<span class="badge acc">3</span> '}Test and verify access</h3></div><div class="card-b stack s12">
         <div class="row"><button class="btn" id="atest">${icon('bolt','sm')}Test connection</button><span class="muted small">Connects with the OmniVec identity and reads a few items. Nothing is written.</span></div><div id="atestout"></div>
         <div id="aperm"></div></div></div>
        <div class="inline-err" id="aerr"></div>
        <div class="row sp"><a class="btn" href="${existing ? `#/connections/${kind==='source'?'source':'store'}/${existing.id}` : '#/connections'}">Cancel</a><button class="btn pri" id="asave">${existing ? 'Save changes' : 'Create '+noun}</button></div>` : ''}`;
      if (st.type) {
        $('#aguide', v).innerHTML = X.perm.guideHtml(kind, st.type, config(), {});
        X.perm.identity().then(id => { if (v.isConnected && st.type) $('#aguide', v).innerHTML = X.perm.guideHtml(kind, st.type, config(), id); });
        X.perm.permAssistant($('#aperm', v), { kind, type:st.type, getConfig:config, noGuide:true, onResult:r => { st.perm = r; drawChecks(); } });
      }
      drawChecks(); bind();
    };
    const bind = () => {
      $$('[data-type]', v).forEach(o => o.onclick = () => { st.type = o.dataset.type; st.tested = null; st.perm = null; draw(); });
      $$('.tagbox', v).forEach(tb => { const inp = $('input', tb); tb.onclick = e => { const b = e.target.closest('[data-tag]'); if (b) { b.remove(); drawChecks(); } else inp.focus(); };
        inp.onkeydown = e => { if ((e.key==='Enter' || e.key===',') && inp.value.trim()) { e.preventDefault(); const t = inp.value.trim().replace(/^\./,''); inp.insertAdjacentHTML('beforebegin', `<span class="badge acc" data-tag="${esc(t)}">${esc(t)} ${icon('x','sm')}</span>`); inp.value = ''; } }; });
      const form = $('#aform', v); form.oninput = () => { st.tested = null; drawChecks(); clearTimeout(form._t); form._t = setTimeout(() => { if (st.type) X.perm.identity().then(id => $('#aguide', v).innerHTML = X.perm.guideHtml(kind, st.type, config(), id)); }, 400); };
      const tb = $('#atest', v); if (tb) tb.onclick = async () => { tb.disabled = true; tb.innerHTML = spinner('Testing…'); const out = $('#atestout', v);
        try { const body = { type:st.type, config:config() }; if (existing) body[kind==='source'?'source_id':'destination_id'] = existing.id;
          const r = await api(`/api/${kind==='source'?'sources':'destinations'}/test-connection`, { method:'POST', body }); st.tested = r.success !== false;
          out.innerHTML = `<div class="callout ${st.tested?'ok':'err'}">${icon(st.tested?'check':'alert','ic')}<div class="grow"><b>${st.tested?'Connected':'Could not connect'}</b>${esc(r.message || r.error || '')}${r.details ? ' · '+esc(typeof r.details==='string'?r.details:JSON.stringify(r.details)) : ''}${st.tested ? '' : '<div style="margin-top:6px">Check the values above, then use <b>Check access</b> below to see whether a role or network rule is missing.</div>'}</div></div>`;
          if (!st.tested) { const pc = $('#pchk', v); pc && pc.click(); } }
        catch (err) { st.tested = false; out.innerHTML = `<div class="callout err">${icon('alert','ic')}<div><b>Test failed</b>${esc(err.message)}</div></div>`; }
        tb.disabled = false; tb.innerHTML = icon('bolt','sm')+'Test connection'; drawChecks(); };
      const sv = $('#asave', v); if (sv) sv.onclick = async () => { const err = $('#aerr', v); err.textContent = '';
        const name = $('[data-name]', v).value.trim(); const miss = required().filter(f => (read()[f.k]??'')==='').map(f => f.label);
        if (!name) { err.textContent = 'Give it a display name.'; return; } if (miss.length) { err.textContent = 'Required: '+miss.join(', '); return; }
        const doSave = async () => { sv.disabled = true; sv.innerHTML = spinner('Saving…');
          try { const body = { name, type:st.type, config:config() }; if (existing) { body.enabled = existing.enabled !== false; body.triggers = existing.triggers || []; }
            const url = `/api/${kind==='source'?'sources':'destinations'}${existing ? '/'+existing.id : ''}`;
            const r = await api(url, { method: existing ? 'PUT' : 'POST', body }); const obj = (r && (r.source || r.destination)) || r || {};
            invalidate('sources','dests','health','triggers'); await need(['sources','dests'], true);
            (r && r.warnings || []).forEach(w => toast(w, 'info')); toast(existing ? 'Saved' : `${noun[0].toUpperCase()+noun.slice(1)} created`);
            const nid = obj.id || (existing && existing.id);
            if (then==='new' && nid) go(`#/new?${kind==='source'?'source':'store'}=${nid}`); else go(`#/connections/${kind==='source'?'source':'store'}/${nid}`); }
          catch (e2) { err.textContent = e2.message; sv.disabled = false; sv.textContent = existing ? 'Save changes' : 'Create '+noun; } };
        if (st.tested !== true && !existing) confirmAction({ title:'Create without a successful test?', confirm:'Create anyway', body:'The connection has not been tested successfully. Pipelines using it may fail until access is fixed. You can re-test from the connection page at any time.', onOk: doSave });
        else doSave(); };
    };
    draw();
  } };
}
X.connectForms = FORMS;
})();
