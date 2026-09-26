/* OmniVec console: core runtime (icons, API client, live store, derived health, shell, router, overlays) */
(() => {
'use strict';
const P = {
  home:'<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
  flow:'<circle cx="5" cy="6" r="2.2"/><circle cx="19" cy="6" r="2.2"/><circle cx="12" cy="18" r="2.2"/><path d="M7 7.2 10.6 16M17 7.2 13.4 16M7.2 6h9.6"/>',
  plug:'<path d="M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5"/>',
  db:'<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  cube:'<path d="m12 2 9 5v10l-9 5-9-5V7z"/><path d="m3 7 9 5 9-5M12 12v10"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  send:'<path d="M4 12h16M14 6l6 6-6 6"/>',
  bolt:'<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  alert:'<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17h.01"/>',
  chart:'<path d="M3 3v18h18"/><path d="m7 15 4-5 3 3 5-7"/>',
  gauge:'<path d="M12 14l4-4"/><path d="M3.5 18a9 9 0 1 1 17 0"/>',
  server:'<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  plus:'<path d="M12 5v14M5 12h14"/>', check:'<path d="m5 12.5 4.5 4.5L19 7.5"/>', x:'<path d="M6 6l12 12M18 6 6 18"/>',
  chev:'<path d="m9 6 6 6-6 6"/>', back:'<path d="m15 6-6 6 6 6"/>', down:'<path d="m6 9 6 6 6-6"/>',
  refresh:'<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
  play:'<path d="M7 4.5v15l12-7.5z"/>', pause:'<path d="M8 5v14M16 5v14"/>',
  more:'<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
  sparkle:'<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  shield:'<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
  lock:'<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon:'<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
  logout:'<path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 17l5-5-5-5M15 12H3"/>',
  box:'<path d="M3 7l9-4 9 4-9 4z"/><path d="M3 7v10l9 4 9-4V7"/>', layers:'<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  book:'<path d="M4 4h10a4 4 0 0 1 4 4v12H8a4 4 0 0 1-4-4z"/><path d="M4 16a4 4 0 0 1 4-4h10"/>',
  upload:'<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>', download:'<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
  key:'<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3"/>',
  link:'<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  file:'<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/>',
  bot:'<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4M9 14h.01M15 14h.01"/>',
  queue:'<path d="M4 6h16M4 12h10M4 18h6"/>',
  ext:'<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  copy:'<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  filter:'<path d="M3 5h18l-7 8v6l-4 2v-8z"/>', trash:'<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  rotate:'<path d="M3 12a9 9 0 0 0 15.4 6.4L21 16"/><path d="M21 21v-5h-5M21 12A9 9 0 0 0 5.6 5.6L3 8"/><path d="M3 3v5h5"/>',
  wand:'<path d="m15 4 5 5L9 20H4v-5z"/><path d="m13 6 5 5"/>', edit:'<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13 7 4 4"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>', terminal:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
  loader:'<path d="M21 12a9 9 0 1 1-6.2-8.6"/>',
};
const icon = (n, c='') => `<svg class="i ${c}" viewBox="0 0 24 24">${P[n]||''}</svg>`;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => n==null || Number.isNaN(n) ? '—' : Number(n).toLocaleString('en-US');
const ms = v => v==null ? '—' : v >= 10000 ? (v/1000).toFixed(0)+'s' : v >= 1000 ? (v/1000).toFixed(1)+'s' : Math.round(v)+'ms';
const toDate = v => { if (v==null || v==='') return null; if (typeof v === 'number') return new Date(v < 1e12 ? v*1000 : v);
  const s = String(v); return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s+'Z'); };
const ago = v => { const d = toDate(v); if (!d || isNaN(d)) return '—'; const s = (Date.now()-d)/1000;
  if (s < 60) return 'just now'; if (s < 3600) return Math.round(s/60)+'m ago'; if (s < 86400) return Math.round(s/3600)+'h ago'; return Math.round(s/86400)+'d ago'; };
const when = v => { const d = toDate(v); return d && !isNaN(d) ? d.toLocaleString() : '—'; };
const plural = (n, w, pl) => `${n} ${n===1 ? w : (pl || w+'s')}`;
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];

/* ---------- API client ---------- */
const TOKEN_KEY = 'omnivec_token';
const session = { user: null };
const getToken = () => localStorage.getItem(TOKEN_KEY) || '';
function errMessage(data, status) {
  const d = data && (data.detail ?? data.error ?? data.message);
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map(x => (x.loc ? x.loc.slice(1).join('.')+': ' : '') + (x.msg || JSON.stringify(x))).join('; ');
  if (d && typeof d === 'object') return d.message || JSON.stringify(d);
  return `Request failed (HTTP ${status})`;
}
async function api(path, { method='GET', body, raw=false, signal, headers={}, anon=false } = {}) {
  const h = { ...headers }; const t = getToken();
  if (t && !anon) h.Authorization = 'Bearer ' + t;
  let payload = body;
  if (body !== undefined && !(body instanceof FormData) && typeof body !== 'string') { h['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, { method, headers: h, body: payload, signal }); }
  catch (e) { if (e.name === 'AbortError') throw e; const err = new Error('Network error: the OmniVec API is unreachable.'); err.status = 0; throw err; }
  if (r.status === 401 && !anon) { signOut('Your session ended. Sign in again to continue.'); const e = new Error('Not signed in'); e.status = 401; throw e; }
  if (raw) return r;
  const txt = await r.text(); let data = null; try { data = txt ? JSON.parse(txt) : null; } catch { data = txt; }
  if (!r.ok) { const e = new Error(errMessage(data, r.status)); e.status = r.status; e.data = data; throw e; }
  return data;
}

/* ---------- live store ---------- */
const D = {}, stamp = {}, errs = {};
const arr = (d, k) => Array.isArray(d) ? d : (d && Array.isArray(d[k]) ? d[k] : []);
const LOADERS = {
  sources: () => api('/api/sources').then(d => arr(d, 'sources')),
  dests: () => api('/api/destinations').then(d => arr(d, 'destinations')),
  pipelines: () => api('/api/pipelines?include_stats=false').then(d => arr(d, 'pipelines')),
  pstats: () => api('/api/pipelines').then(d => arr(d, 'pipelines')),
  models: () => api('/api/models').then(d => arr(d, 'models')),
  health: () => api('/api/health/checks'),
  triggers: () => api('/api/triggers/status'),
  insights: () => api('/api/operations/resource-insights'),
  live: () => api('/api/metrics/live'),
  deployments: () => api('/api/operations/deployments').then(d => arr(d, 'deployments')),
  stats: () => api('/api/stats'),
  caps: () => api('/api/capabilities'),
  series: () => api('/api/metrics/timeseries?granularity=hour'),
  transforms: () => api('/api/docgrok/transforms').then(d => arr(d, 'transforms')),
  publish: () => api('/api/cloud-deployments'),
};
const CORE = ['sources','dests','pipelines','models','health','triggers','insights','live','deployments'];
const inflight = {};
async function need(keys = CORE, force = false) {
  await Promise.all(keys.map(k => {
    if (!force && k in D && Date.now() - stamp[k] < 45000) return null;
    if (inflight[k]) return inflight[k];
    return inflight[k] = LOADERS[k]().then(v => { D[k] = v; errs[k] = null; stamp[k] = Date.now(); })
      .catch(e => { if (e.status === 401) throw e; errs[k] = e.message; if (!(k in D)) D[k] = null; stamp[k] = Date.now(); })
      .finally(() => { delete inflight[k]; });
  }));
  derive();
}
const invalidate = (...keys) => keys.forEach(k => { delete stamp[k]; });
const lastUpdated = () => Math.max(0, ...CORE.map(k => stamp[k] || 0));

/* ---------- vocabulary ---------- */
const TYPES = {
  'cosmosdb':{label:'Azure Cosmos DB',short:'Cosmos DB',logo:'l-cosmos',abbr:'CDB'},
  'azure-blob':{label:'Azure Blob Storage',short:'Blob Storage',logo:'l-blob',abbr:'BLB'},
  'sharepoint':{label:'SharePoint Online',short:'SharePoint',logo:'l-sp',abbr:'SP'},
  'postgresql':{label:'PostgreSQL',short:'PostgreSQL',logo:'l-pg',abbr:'PG'},
  'mssql':{label:'Azure SQL / SQL Server',short:'SQL Server',logo:'l-pg',abbr:'SQL'},
  'databricks':{label:'Azure Databricks',short:'Databricks',logo:'l-onelake',abbr:'DBX'},
  'onelake-iceberg':{label:'OneLake · Apache Iceberg',short:'OneLake',logo:'l-onelake',abbr:'OL'},
  'cosmosdb-vector':{label:'Azure Cosmos DB · Vector',short:'Cosmos DB vector',logo:'l-vec',abbr:'VEC'},
  'pgvector':{label:'PostgreSQL + pgvector',short:'pgvector',logo:'l-pg',abbr:'PGV'},
  'azure-openai':{label:'Azure OpenAI',short:'Azure OpenAI',logo:'l-aoai',abbr:'AI'},
  'openai':{label:'OpenAI',short:'OpenAI',logo:'l-aoai',abbr:'AI'},
  'native':{label:'In-cluster model',short:'In-cluster',logo:'l-aoai',abbr:'ML'},
  'recipe':{label:'Processing recipe',short:'Recipe',logo:'l-aoai',abbr:'RC'},
};
const T = t => TYPES[t] || { label:t||'Unknown', short:t||'Unknown', logo:'l-vec', abbr:(t||'?').slice(0,3).toUpperCase() };
const logo = (t, c='') => `<span class="logo ${T(t).logo} ${c}">${T(t).abbr}</span>`;
const QW_SLOW = 12000;

/* ---------- derived model ---------- */
const M = { sources:[], dests:[], models:[], pipelines:[], SRC:{}, DST:{}, MDL:{}, PIPE:{}, issues:[], trig:{}, workers:null };
const byId = a => Object.fromEntries((a||[]).filter(Boolean).map(x => [x.id, x]));
function derive() {
  const sources = D.sources || [], dests = D.dests || [], models = D.models || [];
  const SRC = byId(sources), DST = byId(dests), MDL = byId(models);
  const H = D.health || {};
  const hp = byId(H.pipelines), hs = byId(H.sources), hd = byId(H.destinations), hm = byId(H.models);
  const ins = Object.fromEntries(((D.insights||{}).pipelines||[]).map(x => [x.pipeline_id, x]));
  const livep = (D.live||{}).pipelines || {};
  const trig = {}; const tr = D.triggers || {};
  [...(tr.blob_sources||[]), ...(tr.cosmosdb_sources||[]), ...(tr.sources||[])].forEach(t => { if (t && t.id) trig[t.id] = t; });
  const workers = (D.deployments||[]).find(d => /worker/.test(d.name)) || null;
  const PS = byId(D.pstats);

  const pipelines = (D.pipelines || []).map(p0 => {
    const p = p0.stats || !PS[p0.id] ? p0 : { ...p0, stats: PS[p0.id].stats };
    const sref = (p.sources||[])[0] || {};
    const src = SRC[sref.source_id] || { id: sref.source_id, name: sref.source_id || 'Missing source', type: '?', missing: true };
    const dst = DST[p.destination_id] || { id: p.destination_id, name: p.destination_id || 'Missing store', type: '?', missing: true };
    const model = MDL[p.docgrok_pipeline] || null;
    const recipe = model ? null : p.docgrok_pipeline || null;
    const st = p.stats || {}, I = ins[p.id] || null, L = livep[p.id] || null, obs = (I && I.observed) || {};
    const qwait = obs.avg_queue_wait_ms ?? (L && L.avg_queue_wait_ms) ?? null;
    const failed = Math.max(obs.failed||0, (L&&L.failed)||0, (st.jobs&&st.jobs.failed)||0);
    const lastWrite = obs.last_activity_at || (L && (L.last_success_at || L.last_activity_at)) || null;
    const problems = [];
    const h = hp[p.id];
    if (h && /unhealthy|error|fail/.test(h.status)) (h.checks||[]).filter(c => c.status === 'fail').forEach(c =>
      problems.push({ sev:'err', code:'check_'+c.check, title:humanCheck(c.check)+' failed', detail:c.detail }));
    if (src.missing) problems.push({ sev:'err', code:'missing_source', title:'Source no longer exists', detail:`The pipeline references ${sref.source_id||'no source'}.` });
    if (dst.missing) problems.push({ sev:'err', code:'missing_store', title:'Vector store no longer exists', detail:`The pipeline references ${p.destination_id||'no store'}.` });
    if (failed > 0) problems.push({ sev:'err', code:'failures', title:plural(failed,'failed document'), detail:'Some documents could not be embedded or written.' });
    if (qwait != null && qwait > QW_SLOW) problems.push({ sev:'warn', code:'queue_wait', title:`Queue wait ${ms(qwait)}`, detail:`Work waits ${ms(qwait)} on average before a worker picks it up.` });
    const t = trig[src.id];
    if (t && t.status === 'not_configured') problems.push({ sev:'warn', code:'trigger', title:'No Event Grid trigger', detail:'New files are not picked up automatically.' });
    if (src.enabled === false) problems.push({ sev:'warn', code:'src_disabled', title:'Source disabled', detail:'The source is disabled, so no new changes flow.' });
    const paused = p.status === 'paused';
    const health = paused ? 'paused' : problems.some(x => x.sev==='err') ? 'err' : problems.length ? 'warn' : (h ? (h.status==='healthy' ? 'ok' : h.status==='warning' ? 'warn' : 'unknown') : 'unknown');
    const total = st.source_doc_count, done = st.embedded_count;
    return { ...p, src, dst, model, recipe, st, statsLoading: !p.stats && !('pstats' in D), ins:I, live:L, qwait, failed, lastWrite, problems, health, paused,
      progress: total ? Math.min(100, Math.round(100*(done||0)/total)) : null,
      vectors: st.lifetime_embedded_count ?? st.embedded_count ?? null };
  });
  const PIPE = byId(pipelines);
  Object.assign(M, { sources, dests, models, pipelines, SRC, DST, MDL, PIPE, trig, workers, hs, hd, hm, hp });
  M.issues = buildIssues();
}
const humanCheck = c => ({ source_health:'Source health', destination_health:'Vector store health', model_accessible:'Model access', connectivity:'Connectivity',
  read_permission:'Read permission', write_permission:'Write permission', vector_policy:'Vector policy', vector_index:'Vector index', enabled:'Enabled',
  endpoint_accessible:'Model endpoint', docgrok_health:'DocGrok', model_registered:'Model registration', hpa_saturation:'Autoscaler headroom', config_read:'Config read', reachable:'Reachability' }[c] || String(c||'check').replace(/_/g,' '));

function buildIssues() {
  const out = []; const H = D.health || {};
  const slow = M.pipelines.filter(p => p.problems.some(x => x.code==='queue_wait')).sort((a,b) => b.qwait - a.qwait);
  if (slow.length) {
    const recs = slow.filter(p => p.ins && p.ins.recommendation && p.ins.recommendation.severity !== 'healthy');
    out.push({ id:'capacity', sev:'warn', area:'Capacity', icon:'gauge', title:`${plural(slow.length,'pipeline')} ${slow.length===1?'is':'are'} waiting too long for workers`,
      detail:`Average queue wait is above ${QW_SLOW/1000}s (worst ${ms(slow[0].qwait)}). ${slow.some(p=>p.failed) ? 'Some failures were observed too.' : 'No failed documents were observed, so the shared worker pool is saturated, not broken.'}`,
      affected: slow.map(p => p.id), kind:'capacity', recs,
      evidence:[['Worst queue wait', ms(slow[0].qwait)], ['Workers ready', M.workers ? `${M.workers.ready_replicas??'—'} / ${M.workers.replicas??'—'}${M.workers.autoscaling ? ' · max '+M.workers.autoscaling.max_replicas : ''}` : 'Not observed'], ['Throttles · failures', `${slow.reduce((a,p)=>a+((p.ins&&p.ins.observed&&p.ins.observed.throttles)||0),0)} · ${slow.reduce((a,p)=>a+p.failed,0)}`]] });
  }
  const failing = M.pipelines.filter(p => p.failed > 0);
  if (failing.length) out.push({ id:'failures', sev:'err', area:'Processing', icon:'alert', title:`${plural(failing.length,'pipeline')} reported failed documents`,
    detail:'Documents failed to embed or write. Open the pipeline to see the failure types, then retry after fixing the cause.', affected: failing.map(p=>p.id), kind:'failures',
    evidence: failing.slice(0,3).map(p => [p.name, plural(p.failed,'failure')]) });
  Object.values(M.trig).filter(t => t.status === 'not_configured').forEach(t => {
    const s = M.SRC[t.id]; const aff = M.pipelines.filter(p => p.src.id === t.id);
    out.push({ id:'trigger-'+t.id, sev:'warn', area:'Source', icon:'bolt', title:`“${(s&&s.name)||t.name}” has no Event Grid subscription`,
      detail:`Existing files were processed, but new uploads to container “${(s&&s.config&&s.config.container)||'—'}” will not start ingestion until an Event Grid subscription delivers BlobCreated events.`,
      affected: aff.map(p=>p.id), kind:'trigger', sourceId:t.id, evidence:[['Trigger', 'Event Grid · not configured'], ['Pipelines affected', String(aff.length)]] });
  });
  const add = (list, area, ic, link) => (list||[]).forEach(x => {
    if (!x || x.status === 'healthy') return;
    const bad = (x.checks||[]).filter(c => c.status === 'fail' || c.status === 'warn');
    if (!bad.length && !/unhealthy|error|warning/.test(x.status)) return;
    const sev = (x.status === 'unhealthy' || bad.some(c => c.status==='fail')) ? 'err' : 'warn';
    out.push({ id:`chk-${area}-${x.id}`, sev, area, icon:ic, title:`${x.name||x.id}: ${bad[0] ? humanCheck(bad[0].check)+(bad[0].status==='fail'?' failed':' needs attention') : x.status}`,
      detail: bad.map(c => c.detail).filter(Boolean).join(' · ') || 'A health check reported a problem.', kind:'check', link: link && link(x),
      checks: x.checks||[], affected: area==='Pipeline' ? [x.id] : M.pipelines.filter(p => p.src.id===x.id || p.dst.id===x.id || (p.model&&p.model.id===x.id)).map(p=>p.id),
      evidence: bad.slice(0,3).map(c => [humanCheck(c.check), c.status]) , checkedAt:x.checked_at });
  });
  add(H.sources, 'Source', 'plug', x => '#/connections/source/'+x.id);
  add(H.destinations, 'Vector store', 'db', x => '#/connections/store/'+x.id);
  add(H.models, 'Model', 'cube', () => '#/models');
  add(H.pipelines, 'Pipeline', 'flow', x => '#/pipelines/'+x.id);
  add(H.services, 'Platform', 'server', () => '#/platform');
  (D.deployments||[]).filter(d => d.replicas > 0 && (d.ready_replicas||0) < d.replicas).forEach(d =>
    out.push({ id:'wl-'+d.name, sev: (d.ready_replicas||0)===0 ? 'err' : 'warn', area:'Platform', icon:'server', title:`${d.name}: ${d.ready_replicas||0} of ${d.replicas} replicas ready`,
      detail:'A workload is not fully ready. Check pod status and recent restarts before scaling or restarting.', kind:'workload', link:'#/platform', affected:[],
      evidence:(d.pods||[]).slice(0,3).map(p => [p.name, `${p.status} · ${p.restarts} restarts`]) }));
  const L = D.live;
  if (L && L.documents_embedded > 0 && L.embedding_latency && !L.embedding_latency.count)
    out.push({ id:'telemetry', sev:'info', area:'Observability', icon:'chart', title:'Embedding latency percentiles are not recorded',
      detail:`${fmt(L.documents_embedded)} documents were embedded since the API started, but no embedding latency samples reached this API replica. Tail latency problems would not be visible here.`,
      kind:'info', affected:[], evidence:[['Documents embedded', fmt(L.documents_embedded)], ['Latency samples', '0']] });
  const rank = { err:0, warn:1, info:2 };
  return out.sort((a,b) => rank[a.sev]-rank[b.sev]);
}
const usedBy = id => M.pipelines.filter(p => p.src.id === id || p.dst.id === id || (p.model && p.model.id === id));

/* ---------- render helpers ---------- */
const dot = h => `<span class="dot ${h==='ok'?'ok':h==='warn'?'warn':h==='err'?'err':h==='info'?'info':''}"></span>`;
const statusBadge = p => p.paused ? `<span class="badge">${icon('pause','sm')}Paused</span>`
  : p.health==='err' ? `<span class="badge err">${dot('err')}${esc(p.problems[0] && p.problems[0].code==='failures' ? 'Failing' : 'Broken')}</span>`
  : p.health==='warn' ? `<span class="badge warn">${dot('warn')}${esc(p.problems[0] ? (p.problems[0].code==='queue_wait' ? 'Slow' : p.problems[0].code==='trigger' ? 'No trigger' : 'Attention') : 'Attention')}</span>`
  : p.health==='ok' ? `<span class="badge ok">${dot('ok')}Healthy</span>` : `<span class="badge out">${dot()}Not checked</span>`;
const chipFlow = p => `<span class="chip-flow">${logo(p.src.type,'sm')}<span class="arrow">→</span>${logo(p.model ? p.model.type : 'recipe','sm')}<span class="arrow">→</span>${logo(p.dst.type,'sm')}<span class="trunc muted" style="max-width:150px">${esc(p.dst.name)}</span></span>`;
const spark = (vals, w=90, h=24, color='var(--accent)') => {
  if (!vals || vals.length < 2) return '';
  const mx = Math.max(...vals, 1), st = w/(vals.length-1);
  const pts = vals.map((v,i)=>`${(i*st).toFixed(1)},${(h-2-(v/mx)*(h-4)).toFixed(1)}`).join(' ');
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/><polygon points="0,${h} ${pts} ${w},${h}" fill="${color}" opacity=".08"/></svg>`;
};
const chart = (labels, series, h=180) => {
  const W=640,H=h,pl=40,pr=10,pt=10,pb=22,iw=W-pl-pr,ih=H-pt-pb,n=Math.max(labels.length,1),bw=iw/n;
  let out=`<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${h}px">`;
  const mxL=Math.max(...series.filter(s=>s.axis!=='r').flatMap(s=>s.vals),1);
  for(let g=0;g<=4;g++){const y=pt+ih*g/4;out+=`<line x1="${pl}" x2="${W-pr}" y1="${y}" y2="${y}" stroke="var(--grid)"/><text x="${pl-6}" y="${y+3}" font-size="9.5" fill="var(--faint)" text-anchor="end">${fmt(Math.round(mxL*(1-g/4)))}</text>`;}
  const every = Math.ceil(n/12);
  labels.forEach((l,i)=>{ if(i%every===0) out+=`<text x="${pl+bw*i+bw/2}" y="${H-6}" font-size="9.5" fill="var(--faint)" text-anchor="middle">${esc(l)}</text>`; });
  series.forEach(s=>{const mx=s.axis==='r'?Math.max(...s.vals,1):mxL;
    if(s.type==='bar') s.vals.forEach((v,i)=>{const bh=ih*(v||0)/mx,rw=Math.min(bw*.6,36);out+=`<rect x="${pl+bw*i+(bw-rw)/2}" y="${pt+ih-bh}" width="${rw}" height="${bh}" rx="3" fill="${s.color}" opacity=".85"><title>${esc(labels[i])}: ${fmt(v)}</title></rect>`;});
    else {const pts=s.vals.map((v,i)=>`${pl+bw*i+bw/2},${pt+ih-ih*(v||0)/mx}`).join(' ');out+=`<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round"/>`+s.vals.map((v,i)=>`<circle cx="${pl+bw*i+bw/2}" cy="${pt+ih-ih*(v||0)/mx}" r="2.6" fill="var(--surface)" stroke="${s.color}" stroke-width="1.6"><title>${esc(labels[i])}: ${s.fmt?s.fmt(v):fmt(v)}</title></circle>`).join('');}
  });
  return out+'</svg>';
};
const empty = (ic, t, d, act='') => `<div class="empty">${ic?`<div class="ic">${icon(ic,'lg')}</div>`:''}<h4>${t}</h4><div>${d}</div>${act?`<div style="margin-top:14px">${act}</div>`:''}</div>`;
const codebox = (txt, label='Copy') => `<div class="codebox"><div class="code">${esc(txt)}</div><button class="btn sm" data-copy="${esc(txt)}">${icon('copy','sm')}${label}</button></div>`;
const loadErr = keys => { const e = keys.filter(k => errs[k]); return e.length ? `<div class="callout warn err-banner">${icon('alert','ic')}<div class="grow"><b>Some data could not be loaded</b>${e.map(k => `${esc(k)}: ${esc(errs[k])}`).join(' · ')}</div><button class="btn sm" data-act="reload">${icon('refresh','sm')}Retry</button></div>` : ''; };
const spinner = (t='Working…') => `<span class="row" style="gap:8px">${icon('loader','sm spin')}${esc(t)}</span>`;

/* ---------- overlays ---------- */
let ov;
const escClose = e => { if (e.key === 'Escape' && !$('.menu')) closeOverlay(); };
const closeOverlay = () => { if (ov) ov.innerHTML = ''; document.removeEventListener('keydown', escClose); };
const openDrawer = (html, wide=false) => { ov.innerHTML = `<div class="scrim" data-close></div><aside class="drawer ${wide?'w':''}" role="dialog">${html}</aside>`; document.addEventListener('keydown', escClose); return $('.drawer', ov); };
const openModal = (html, cls='') => { ov.innerHTML = `<div class="scrim" data-close></div><div class="modal ${cls}" role="dialog">${html}</div>`; document.addEventListener('keydown', escClose); return $('.modal', ov); };
const toast = (msg, kind='ok') => { const t = document.createElement('div'); t.className = 'toast';
  t.innerHTML = icon(kind==='err' ? 'alert' : kind==='info' ? 'info' : 'check') + `<span>${esc(msg)}</span>`;
  if (kind==='err') t.style.background = 'var(--err)'; $('#toasts').append(t); setTimeout(() => t.remove(), kind==='err' ? 6500 : 3400); };
function confirmAction({ title, body, confirm='Confirm', danger=false, typed=null, onOk }) {
  const m = openModal(`<div class="modal-h"><h3>${esc(title)}</h3></div><div class="modal-b t2 stack s12"><div>${body}</div>
    ${typed ? `<div class="fld"><label>Type <b class="mono">${esc(typed)}</b> to confirm</label><input class="inp" id="mtyped" autocomplete="off"></div>` : ''}<div class="inline-err" id="merr"></div></div>
    <div class="modal-f"><button class="btn" data-close>Cancel</button><button class="btn ${danger?'danger':'pri'}" id="mok" ${typed?'disabled':''}>${esc(confirm)}</button></div>`);
  const ok = $('#mok', m), inp = $('#mtyped', m);
  if (inp) { inp.oninput = () => { ok.disabled = inp.value.trim() !== typed; }; inp.focus(); }
  ok.onclick = async () => { ok.disabled = true; ok.innerHTML = spinner('Working…');
    try { await (onOk && onOk()); closeOverlay(); } catch (e) { $('#merr', m).textContent = e.message; ok.disabled = false; ok.textContent = confirm; } };
}
function menu(anchor, items) {
  closeMenu(); const r = anchor.getBoundingClientRect(); const el = document.createElement('div'); el.className = 'menu';
  el.innerHTML = items.map((it, i) => it === '-' ? '<div class="sepr"></div>' : `<button data-mi="${i}" class="${it.danger?'danger':''}">${icon(it.icon||'chev','sm')}${esc(it.label)}</button>`).join('');
  document.body.append(el);
  const w = el.offsetWidth; el.style.top = Math.min(r.bottom + 4, innerHeight - el.offsetHeight - 8) + 'px'; el.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + 'px';
  el.onclick = e => { const b = e.target.closest('[data-mi]'); if (!b) return; closeMenu(); items[+b.dataset.mi].run(); };
  setTimeout(() => document.addEventListener('click', closeMenu, { once:true }), 0);
}
const closeMenu = () => $$('.menu').forEach(m => m.remove());
async function copy(txt) { try { await navigator.clipboard.writeText(txt); toast('Copied to clipboard'); } catch { toast('Copy failed. Select the text manually.', 'err'); } }

/* ---------- theme ---------- */
const setThemeIcon = () => { const b = $('#themeBtn'); if (b) b.innerHTML = icon(document.documentElement.dataset.theme==='dark' ? 'sun' : 'moon', 'sm'); };
const toggleTheme = () => { const d = document.documentElement; const n = d.dataset.theme==='dark' ? 'light' : 'dark'; d.dataset.theme = n; localStorage.setItem('ov-theme', n); setThemeIcon(); };

/* ---------- navigation + router ---------- */
const navModel = () => {
  const open = M.issues.filter(i => i.sev !== 'info'); const errN = open.filter(i => i.sev==='err').length;
  return [
    { g:null, items:[['home','Home','home'],['pipelines','Pipelines','flow',M.pipelines.length],['connections','Connections','plug',M.sources.length+M.dests.length],['models','Models & recipes','cube',M.models.length||null],['search','Search','search'],['publish','Publish to AI apps','send']] },
    { g:'Operate', items:[['issues','Issues','alert',open.length||null, errN ? 'err' : 'warn'],['metrics','Metrics','chart'],['capacity','Capacity','gauge'],['platform','Platform','server']] },
    { g:'Workspace', items:[['settings','Settings','gear']] },
  ];
};
const ALIAS = { new:'pipelines' };
const renderNav = cur => { cur = ALIAS[cur] || cur; const n = $('#nav'); if (!n) return;
  const ab = $('.topbar [data-act="agent"]'); if (ab) { const miss = M.models.length > 0 && !M.models.some(m => m.model_category==='chat'); ab.classList.toggle('needs-setup', miss); ab.title = miss ? 'Agent needs a chat model. Click to set up.' : ''; }
  n.innerHTML = navModel().map(s => (s.g?`<div class="nav-g">${s.g}</div>`:'') + s.items.map(([k,l,ic,ct,cls]) =>
    `<a href="#/${k}" title="${l}" class="${cur===k?'on':''}">${icon(ic)}<span class="nl">${l}</span>${ct!=null?`<span class="ct ${cls||''}" ${cls==='err'?'style="background:var(--err-soft);color:var(--err)"':''}>${ct}</span>`:''}</a>`).join('')).join(''); };

const routes = {};
const route = (name, def) => { routes[name] = def; };
let current = { name:null, parts:[] }, renderSeq = 0, query = new URLSearchParams();
const go = h => { if (location.hash === h) render(); else location.hash = h; };
async function render(opts = {}) {
  if (!getToken()) return showSignin();
  if (!$('#view')) buildShell();
  const [path, qs] = location.hash.replace(/^#\/?/, '').split('?');
  const parts = (path || 'home').split('/').map(decodeURIComponent);
  query = new URLSearchParams(qs || '');
  const name = routes[parts[0]] ? parts[0] : 'home'; const def = routes[name];
  const seq = ++renderSeq; const view = $('#view');
  const same = current.name === name && current.parts.join('/') === parts.join('/');
  if (!opts.silent && !same) { view.innerHTML = `<div class="loading-page"><div class="skel" style="height:34px;width:260px"></div><div class="skel" style="height:120px"></div><div class="skel" style="height:280px"></div></div>`; closeOverlay(); }
  try { await need(def.needs || CORE, !!opts.force); }
  catch (e) { if (e.status === 401) return; }
  if (seq !== renderSeq) return;
  current = { name, parts };
  renderNav(name);
  let res;
  try { res = await def.render(parts.slice(1)); }
  catch (e) { console.error(e); res = { crumbs:[['Error']], html:`<div class="page">${empty('alert','This page could not be displayed', esc(e.message), '<button class="btn" data-act="reload">Retry</button>')}</div>` }; }
  if (seq !== renderSeq) return;
  $('#crumbs').innerHTML = (res.crumbs||[]).map((c,i,a) => i < a.length-1 ? `<a href="${c[1]}">${esc(c[0])}</a><span class="sep">/</span>` : `<span class="cur">${esc(c[0])}</span>`).join('');
  document.title = ((res.crumbs||[]).slice(-1)[0]||['OmniVec'])[0] + ' · OmniVec';
  const top = view.scrollTop;
  view.innerHTML = `<div class="fade-in">${res.html}</div>`;
  if (opts.silent) view.scrollTop = top; else view.scrollTop = 0;
  res.mount && res.mount(view);
  pageTables(view);
  updateFreshness();
  if (!inflight.pstats && (!('pstats' in D) || Date.now() - (stamp.pstats||0) > 45000)) {
    need(['pstats']).then(() => { if (seq !== renderSeq || def.live === false || (ov && ov.innerHTML) || $('.menu')) return;
      const a = document.activeElement; if (a && /INPUT|TEXTAREA|SELECT/.test(a.tagName)) return; render({ silent:true }); }).catch(() => {});
  }
}
/* Searchable single-select dropdown that scales to hundreds of options.
   items: [{ v, l, s?, lg? }]  (value, label, sub-label, logo type) */
function combo(host, { items, value = '', all, placeholder = 'Search', width = 200, onPick }) {
  if (!host) return;
  const cur = () => items.find(i => i.v === value);
  const label = () => { const c = cur(); return c ? `${c.lg ? logo(c.lg, 'sm') : ''}<span class="trunc">${esc(c.l)}</span>` : `<span class="trunc ${all ? '' : 'muted'}">${esc(all || placeholder)}</span>`; };
  host.classList.add('msel'); host.innerHTML = `<button class="sel msel-btn" type="button" style="min-width:0;width:${width}px">${label()}</button>`;
  const btn = host.firstElementChild;
  btn.onclick = e => { e.stopPropagation();
    const open = $('.msel-pop', host); if (open) return open.remove();
    $$('.msel-pop').forEach(p => p.remove());
    const pop = document.createElement('div'); pop.className = 'msel-pop'; pop.style.width = Math.max(320, width) + 'px';
    if (host.getBoundingClientRect().left + Math.max(320, width) > innerWidth - 16) { pop.style.left = 'auto'; pop.style.right = '0'; }
    pop.innerHTML = `${items.length > 6 ? `<div style="padding:8px"><div class="search-inp">${icon('search','sm')}<input class="inp" placeholder="${esc(placeholder)} (${items.length})"></div></div>` : ''}<div class="msel-list"></div>`;
    host.append(pop);
    const inp = $('input', pop); let hi = 0;
    const paint = () => { const q = (inp ? inp.value : '').toLowerCase();
      const list = (all && !q ? [{ v:'', l:all }] : []).concat(items.filter(i => !q || (i.l + ' ' + (i.s||'') + ' ' + i.v).toLowerCase().includes(q))).slice(0, 300);
      hi = Math.min(hi, Math.max(0, list.length - 1));
      $('.msel-list', pop).innerHTML = list.map((i, n) => `<div class="msel-it ${i.v===value?'on':''} ${n===hi?'hi':''}" data-v="${esc(i.v)}">${i.lg ? logo(i.lg,'sm') : ''}<div class="grow" style="min-width:0"><div class="trunc" style="font-weight:${i.v===value?600:450}">${esc(i.l)}</div>${i.s ? `<div class="muted small trunc">${esc(i.s)}</div>` : ''}</div>${i.v===value ? icon('check','sm') : ''}</div>`).join('') || '<div class="muted small" style="padding:12px">No matches</div>';
      $$('.msel-it', pop).forEach(el => el.onclick = () => pick(el.dataset.v)); return list; };
    const pick = v => { pop.remove(); if (v === value) return; value = v; btn.innerHTML = label(); onPick && onPick(v); };
    let list = paint();
    if (inp) { inp.oninput = () => { hi = 0; list = paint(); }; inp.focus(); } else { pop.tabIndex = -1; pop.style.outline = 'none'; pop.focus(); }
    pop.onkeydown = e => { if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); hi = Math.max(0, Math.min(list.length - 1, hi + (e.key === 'ArrowDown' ? 1 : -1))); list = paint(); const h = $('.msel-it.hi', pop); h && h.scrollIntoView({ block:'nearest' }); }
      else if (e.key === 'Enter' && list[hi]) { e.preventDefault(); pick(list[hi].v); } else if (e.key === 'Escape') { pop.remove(); btn.focus(); } };
    pop.onclick = e => e.stopPropagation(); setTimeout(() => document.addEventListener('click', () => pop.remove(), { once:true }), 0);
  };
}

/* Pagination for any long .tbl instead of inner scrolling. Page is remembered per route + table index. */
const PAGE = 10, pageMem = {};
function pageTables(root) {
  $$('table.tbl', root).forEach((t, ti) => {
    const body = t.tBodies[0]; if (!body) return;
    const rows = [...body.rows].filter(r => !r.classList.contains('detail-row'));
    const sig = rows.length + ':' + (rows[0] ? rows[0].textContent.length : 0);
    if (t.dataset.pg === sig) return; t.dataset.pg = sig;
    let pg = t.nextElementSibling && t.nextElementSibling.classList.contains('pager') ? t.nextElementSibling : null;
    if (rows.length <= PAGE) { pg && pg.remove(); rows.forEach(r => r.hidden = false); return; }
    if (!pg) { pg = document.createElement('div'); pg.className = 'pager'; t.after(pg); }
    const key = location.hash.split('?')[0] + '#' + ti, pages = Math.ceil(rows.length / PAGE);
    const show = p => { p = Math.max(0, Math.min(pages - 1, p)); pageMem[key] = p;
      rows.forEach((r, i) => r.hidden = Math.floor(i / PAGE) !== p);
      const nums = []; for (let i = 0; i < pages; i++) if (i < 1 || i >= pages - 1 || Math.abs(i - p) <= 1) nums.push(i); else if (nums[nums.length - 1] !== -1) nums.push(-1);
      pg.innerHTML = `<span class="muted small">${p * PAGE + 1}–${Math.min(rows.length, (p + 1) * PAGE)} of ${rows.length}</span><div class="row" style="gap:4px">
        <button class="pn" data-p="${p - 1}" ${p === 0 ? 'disabled' : ''} aria-label="Previous page">‹</button>
        ${nums.map(i => i < 0 ? '<span class="gap">…</span>' : `<button class="pn ${i === p ? 'on' : ''}" data-p="${i}">${i + 1}</button>`).join('')}
        <button class="pn" data-p="${p + 1}" ${p === pages - 1 ? 'disabled' : ''} aria-label="Next page">›</button></div>`;
      $$('[data-p]', pg).forEach(b => b.onclick = e => { e.stopPropagation(); show(+b.dataset.p); }); };
    show(pageMem[key] || 0);
  });
}
let pgTimer; new MutationObserver(ms => { if (ms.every(m => m.target.closest && m.target.closest('.pager'))) return;
  clearTimeout(pgTimer); pgTimer = setTimeout(() => { const v = $('#view'); v && pageTables(v); }, 60); })
  .observe(document.body, { childList: true, subtree: true });
function updateFreshness() { const el = $('#fresh'); if (!el) return; const t = lastUpdated(); el.textContent = t ? 'Updated ' + ago(t) : ''; }
setInterval(() => {
  updateFreshness();
  if (!getToken() || document.hidden || !current.name) return;
  const def = routes[current.name]; if (!def || def.live === false) return;
  if ((ov && ov.innerHTML) || $('.menu')) return;
  const a = document.activeElement; if (a && /INPUT|TEXTAREA|SELECT/.test(a.tagName)) return;
  if (Date.now() - lastUpdated() > 60000) render({ force:true, silent:true });
}, 15000);

/* ---------- shell ---------- */
function buildShell() {
  const u = session.user || {};
  document.body.innerHTML = `<a class="skip" href="#view" style="position:absolute;left:-999px">Skip to content</a>
  <div class="app">
  <aside class="side">
    <div class="brand"><span class="brand-mark"></span><b>OmniVec</b></div>
    <div class="ws" data-act="palette" title="Search or jump (Ctrl K)"><span class="av">OV</span>
      <div class="grow" style="min-width:0"><div style="font-weight:600;font-size:12.5px" class="trunc">${esc(location.hostname)}</div><small>Vector ingestion workspace</small></div>
      ${icon('search','sm')}</div>
    <nav class="nav" id="nav"></nav>
    <div class="side-foot"><span class="av-u">${esc((u.name||'U').slice(0,2).toUpperCase())}</span>
      <div class="who" style="min-width:0"><div style="font-weight:550" class="trunc">${esc(u.name||'Signed in')}</div><small>${esc(u.role ? u.role[0].toUpperCase()+u.role.slice(1) : 'Token')} access</small></div>
      <button class="btn ghost sm icon" id="themeBtn" data-act="theme" title="Toggle theme"></button>
      <button class="btn ghost sm icon" data-act="signout" title="Sign out">${icon('logout','sm')}</button></div>
  </aside>
  <div class="mainwrap">
    <header class="topbar"><div class="crumbs" id="crumbs"></div>
      <span class="muted small" id="fresh" style="margin-left:auto;white-space:nowrap"></span>
      <button class="btn ghost sm icon" data-act="reload" title="Refresh data">${icon('refresh','sm')}</button>
      <div class="cmdk" data-act="palette" style="margin-left:0">${icon('search','sm')}<span>Search or jump to…</span><kbd>Ctrl K</kbd></div>
      <button class="btn" data-act="agent"><span class="agent-orb" style="width:18px;height:18px;border-radius:6px"></span>Ask agent <kbd>Ctrl J</kbd></button>
      <a class="btn pri" href="#/new">${icon('plus')}New pipeline</a></header>
    <main class="view" id="view" tabindex="-1"></main>
  </div></div><div id="overlay"></div><div class="toasts" id="toasts"></div>`;
  ov = $('#overlay'); ov.addEventListener('click', e => { if (e.target.closest('[data-close]')) closeOverlay(); });
  setThemeIcon();
}

/* ---------- sign in ---------- */
function showSignin(msg) {
  current = { name:null, parts:[] };
  document.body.innerHTML = `<div class="signin-wrap">
   <div class="signin-art"><div class="row"><span class="brand-mark"></span><b style="font-size:15px">OmniVec</b></div>
    <div><h2>Any data, any source, any model, any vector store. Always search-ready, always fresh.</h2>
     <p>Databases, documents, SharePoint, lakes. Text, PDFs, Office files, images, video. Embed with the model you choose, write to the vector store you run, and OmniVec keeps it in sync as data changes. It also shows what's slow or broken and proposes fixes you approve.</p>
     <div class="signin-flow"><div class="fnode"><div class="k">${icon('plug','sm')}Any source</div></div><div class="conn live" style="width:40px"></div>
      <div class="fnode"><div class="k">${icon('cube','sm')}Any model</div></div><div class="conn live" style="width:40px"></div>
      <div class="fnode"><div class="k">${icon('db','sm')}Any vector store</div></div><div class="conn live" style="width:40px"></div>
      <div class="fnode"><div class="k">${icon('send','sm')}AI apps</div></div></div></div>
    <div class="muted small">Your token stays in this browser and is sent only to this OmniVec API.</div></div>
   <div class="signin-form"><form class="signin-card" id="signin" autocomplete="off">
    <span class="brand-mark" style="width:34px;height:34px;border-radius:10px"></span>
    <h1>Sign in to OmniVec</h1><div class="muted" style="margin-bottom:20px">Paste an access token issued by your administrator.</div>
    ${msg ? `<div class="callout warn" style="margin-bottom:14px">${icon('info','ic')}<div>${esc(msg)}</div></div>` : ''}
    <div class="fld"><label for="tok">Access token</label><input class="inp mono" id="tok" type="password" placeholder="ovk_…" required autofocus></div>
    <div class="inline-err" id="serr"></div>
    <button class="btn pri" style="width:100%;height:38px;justify-content:center;margin-top:14px" id="sbtn">Sign in</button>
    <details class="muted small" style="margin-top:18px"><summary style="cursor:pointer">Where do I find a token?</summary>
     <div style="margin-top:8px;line-height:1.6">Admins can run <span class="mono">omnivec login</span> and read <span class="mono">~/.omnivec/config.yaml</span>, or create a scoped token in <b>Settings → Access tokens</b>. Search-only tokens can query vector stores but cannot change pipelines.</div></details>
   </form></div></div><div id="overlay"></div><div class="toasts" id="toasts"></div>`;
  ov = $('#overlay');
  $('#signin').onsubmit = async e => {
    e.preventDefault(); const tok = $('#tok').value.trim(); const b = $('#sbtn'); if (!tok) return;
    b.disabled = true; b.innerHTML = spinner('Verifying…'); $('#serr').textContent = '';
    try { const r = await api('/api/auth/login', { method:'POST', body:{ token: tok }, anon:true });
      if (!r || r.authenticated === false) throw new Error('This token was not accepted.');
      localStorage.setItem(TOKEN_KEY, tok); session.user = r; buildShell(); if (!location.hash || location.hash === '#/signin') location.hash = '#/home'; render(); }
    catch (err) { $('#serr').textContent = err.status === 401 || err.status === 403 ? 'This token was not accepted. Check it and try again.' : err.message; b.disabled = false; b.textContent = 'Sign in'; }
  };
}
function signOut(msg) { localStorage.removeItem(TOKEN_KEY); session.user = null; Object.keys(D).forEach(k => delete D[k]); Object.keys(stamp).forEach(k => delete stamp[k]); showSignin(msg); }

/* ---------- command palette ---------- */
function palette() {
  const items = [
    ...[['New pipeline','#/new','plus'],['Add source','#/connections/new/source','plug'],['Add vector store','#/connections/new/store','db'],['Add embedding model','#/models/new','cube'],
        ['Test retrieval','#/search','search'],['Open issues','#/issues','alert'],['Run health checks','#/issues/run','refresh'],['Ask the agent','agent','sparkle'],['Toggle theme','theme','moon'],['Classic console','/classic.html','ext']]
      .map(x => ({ g:'Actions', l:x[0], h:x[1], ic:x[2] })),
    ...M.pipelines.map(p => ({ g:'Pipelines', l:p.name, h:'#/pipelines/'+p.id, ic:'flow', s:T(p.src.type).short+' → '+T(p.dst.type).short })),
    ...M.sources.map(s => ({ g:'Sources', l:s.name, h:'#/connections/source/'+s.id, ic:'plug', s:T(s.type).short })),
    ...M.dests.map(d => ({ g:'Vector stores', l:d.name, h:'#/connections/store/'+d.id, ic:'db', s:T(d.type).short })),
    ...M.issues.map(i => ({ g:'Issues', l:i.title, h:'#/issues/'+i.id, ic:'alert', s:i.area })),
  ];
  const m = openModal(`<input id="pq" placeholder="Search pipelines, connections, issues, actions…" autocomplete="off">${icon('search','pi')}<div class="res" id="pr"></div>`, 'palette');
  const q = $('#pq', m), r = $('#pr', m); let sel = 0, list = [];
  const draw = () => { const t = q.value.toLowerCase().trim(); list = items.filter(i => !t || (i.l+' '+(i.s||'')+' '+i.g).toLowerCase().includes(t)).slice(0, 50); sel = Math.max(0, Math.min(sel, list.length-1));
    let g = ''; r.innerHTML = list.map((i, ix) => (i.g!==g ? `<div class="grp">${g=i.g}</div>` : '') + `<div class="it ${ix===sel?'on':''}" data-ix="${ix}">${icon(i.ic)}<span class="trunc">${esc(i.l)}</span><small>${esc(i.s||'')}</small></div>`).join('') || '<div class="empty" style="padding:24px">No matches</div>';
    const on = $('.it.on', r); on && on.scrollIntoView({ block:'nearest' }); };
  const pick = i => { closeOverlay(); if (i.h==='agent') openAgent(); else if (i.h==='theme') toggleTheme(); else if (i.h.startsWith('/')) location.href = i.h; else go(i.h); };
  q.oninput = () => { sel = 0; draw(); };
  q.onkeydown = e => { if (e.key==='ArrowDown') { sel = Math.min(sel+1, list.length-1); draw(); e.preventDefault(); } else if (e.key==='ArrowUp') { sel = Math.max(sel-1, 0); draw(); e.preventDefault(); } else if (e.key==='Enter' && list[sel]) pick(list[sel]); };
  r.onclick = e => { const it = e.target.closest('.it'); if (it) pick(list[+it.dataset.ix]); };
  draw(); q.focus();
}

/* ---------- agent entry (implementation in views-ops.js) ---------- */
function openAgent(ctx, prompt) {
  if (!ctx) {
    if (current.name === 'pipelines' && current.parts[1] && M.PIPE[current.parts[1]]) ctx = { pipeline: M.PIPE[current.parts[1]] };
    else if (current.name === 'issues' && current.parts[1]) ctx = { issue: M.issues.find(i => i.id === current.parts[1]) };
    else ctx = {};
  }
  window.OVX.agentPanel(ctx, prompt);
}

document.addEventListener('keydown', e => {
  if (!getToken()) return;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); palette(); }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j') { e.preventDefault(); openAgent(); }
});
document.addEventListener('click', e => {
  const c = e.target.closest('[data-copy]'); if (c) { copy(c.dataset.copy); return; }
  const a = e.target.closest('[data-act]'); if (!a) return;
  const act = a.dataset.act;
  if (act==='palette') palette(); else if (act==='agent') openAgent(null, a.dataset.prompt); else if (act==='theme') toggleTheme();
  else if (act==='signout') confirmAction({ title:'Sign out?', body:'Your token will be removed from this browser.', confirm:'Sign out', onOk:() => signOut() });
  else if (act==='reload') { render({ force:true, silent:true }).then(() => toast('Data refreshed', 'info')); }
});
window.addEventListener('hashchange', () => render());

async function boot() {
  if (!getToken()) return showSignin();
  try { session.user = await api('/api/auth/login', { method:'POST', body:{ token:getToken() }, anon:true }); }
  catch (e) { if (e.status === 401 || e.status === 403) return signOut('Your saved token is no longer valid. Sign in again.'); }
  buildShell(); render();
}

window.OVX = { icon, esc, fmt, ms, ago, when, plural, toDate, $, $$, api, need, invalidate, D, M, errs, T, logo, TYPES, usedBy, QW_SLOW, humanCheck,
  dot, statusBadge, chipFlow, spark, chart, empty, codebox, loadErr, spinner, openDrawer, openModal, closeOverlay, toast, confirmAction, menu, copy,
  route, go, render, palette, openAgent, session, combo, current: () => current, query: () => query, boot, CORE };
})();
