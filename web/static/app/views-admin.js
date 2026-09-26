/* OmniVec console: Agent panel, Models & recipes, Publish to AI apps, Settings */
(() => {
const X = window.OVX;
const { icon, esc, fmt, ms, ago, when, plural, api, need, invalidate, D, M, T, logo, dot, empty, codebox, loadErr, spinner, route, go, toast, confirmAction, openModal, openDrawer, closeOverlay, $, $$ } = X;

/* ---------- agent ---------- */
const A = { session:null, msgs:[], busy:false, model:localStorage.getItem('ov-agent-model') || '' };
const md = t => esc(t).replace(/```([\s\S]*?)```/g, (_, c) => `<pre class="code">${c}</pre>`).replace(/`([^`]+)`/g, '<span class="mono">$1</span>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/\n{2,}/g, '</p><p>').replace(/\n/g, '<br>');
X.agentPanel = (ctx = {}, prompt) => {
  const scope = ctx.pipeline ? `Pipeline · ${ctx.pipeline.name}` : ctx.issue ? `Issue · ${ctx.issue.title}` : 'Whole workspace';
  const sug = ctx.pipeline ? [`Why is ${ctx.pipeline.name} slow?`, `Are there failed documents in ${ctx.pipeline.name}?`, 'Check permissions for this pipeline']
    : ['What should I fix first?', 'Which pipelines are slow and why?', 'Explain the worker capacity', 'Is anything failing right now?'];
  const chats = M.models.filter(m => m.model_category==='chat');
  const dr = openDrawer(`<div class="drawer-h"><span class="agent-orb"></span><div class="grow" style="min-width:0"><h3>OmniVec agent</h3><div class="muted small trunc">Scope: ${esc(scope)}</div></div>
     ${chats.length > 1 ? `<select class="sel" id="amodel" style="width:150px;height:28px">${chats.map(m => `<option value="${m.id}" ${A.model===m.id?'selected':''}>${esc(m.deployment||m.name)}</option>`).join('')}</select>` : ''}
     <button class="btn ghost sm icon" id="anew" title="New conversation">${icon('plus','sm')}</button><button class="btn ghost sm icon" data-close>${icon('x','sm')}</button></div>
   <div class="drawer-b"><div class="chat" id="achat"></div></div>
   <div class="drawer-f" style="flex-direction:column;align-items:stretch"><div class="suggest" id="asug">${sug.map(s => `<button>${esc(s)}</button>`).join('')}</div>
    <div class="chat-input"><textarea id="ain" placeholder="Ask about pipelines, failures, permissions or capacity…"></textarea><div class="row sp"><span class="muted small">Changes always need your approval</span><button class="btn pri sm" id="asend">${icon('send','sm')}Send</button></div></div></div>`, true);
  if (!chats.length) {
    const pr = ((D.publish||{}).capabilities||{}).principal_id;
    dr.classList.add('locked'); if (dr.previousElementSibling) dr.previousElementSibling.classList.add('blur');
    dr.insertAdjacentHTML('beforeend', `<div class="agent-lock" role="alertdialog" aria-labelledby="alk-h"><div class="agent-lock-card">
      <div class="big">${icon('alert','lg')}</div>
      <h3 id="alk-h">The agent can't run yet</h3>
      <div class="t2">No chat model is registered, so the agent can't answer questions, run diagnostics from chat or propose fixes. Everything else in OmniVec keeps working.</div>
      <div class="eyebrow" style="margin:16px 0 8px">Set it up in 3 steps</div>
      <div class="perm-step"><span class="n">1</span><div class="small">Deploy a chat model such as <b>gpt-4o</b> or <b>gpt-4.1</b> in your Azure OpenAI resource.</div></div>
      <div class="perm-step"><span class="n">2</span><div class="small">Grant the OmniVec identity <b>Cognitive Services OpenAI User</b> on that resource.${pr ? `<div class="row" style="gap:6px;margin-top:6px"><span class="mono small trunc">${esc(pr)}</span><button class="btn sm" data-copy="${esc(pr)}">${icon('copy','sm')}Copy ID</button></div>` : ''}</div></div>
      <div class="perm-step"><span class="n">3</span><div class="small">Register it in OmniVec as a <b>Chat</b> model.</div></div>
      <div class="row" style="margin-top:18px;gap:8px"><a class="btn pri" href="#/models/new?cat=chat">${icon('plus','sm')}Register a chat model</a><button class="btn" data-close>Not now</button></div></div></div>`);
    $('.agent-lock', dr).style.top = $('.drawer-h', dr).offsetHeight + 'px';
    $('#ain', dr).disabled = true; $('#asend', dr).disabled = true;
  }
  const chat = $('#achat', dr), inp = $('#ain', dr);
  const paint = () => { if (!A.msgs.length) { chat.innerHTML = `<div class="empty"><div class="ic">${icon('sparkle','lg')}</div><h4>Diagnose, explain, fix</h4><div>The agent reads live health, metrics and logs. It proposes changes and waits for your approval before running anything.</div></div>`; return; }
    chat.innerHTML = A.msgs.map((m, i) => m.role==='user' ? `<div class="msg u">${esc(m.text)}</div>`
      : `<div class="msg a"><div class="who"><span class="agent-orb" style="width:18px;height:18px;border-radius:6px"></span>Agent</div>
        ${(m.tools||[]).map(t => `<div class="tool">${icon(t.done ? 'check' : 'loader', 'sm'+(t.done?'':' spin'))}<span class="mono">${esc(t.name)}</span><span class="muted trunc">${esc(t.args ? JSON.stringify(t.args).slice(0,80) : '')}</span></div>`).join('')}
        ${m.text ? `<div class="body"><p>${md(m.text)}</p></div>` : (A.busy && i===A.msgs.length-1 && !(m.tools||[]).length ? `<div class="muted">${spinner('Thinking…')}</div>` : '')}
        ${(m.approvals||[]).map(ap => `<div class="approve"><div class="h">${icon('shield','sm')}Approval needed · ${esc(ap.tool)}${ap.danger_level ? ` <span class="badge ${/high|danger/.test(ap.danger_level)?'err':'warn'}" style="margin-left:auto">${esc(ap.danger_level)}</span>` : ''}</div>
          <div class="b stack s8"><div>${esc(ap.summary || 'The agent wants to run this action.')}</div>${ap.args ? `<div class="code small" style="white-space:pre-wrap">${esc(JSON.stringify(ap.args, null, 2))}</div>` : ''}
          ${ap.decision ? `<span class="badge ${ap.decision==='approve'?'ok':''}">${ap.decision==='approve'?'Approved':'Denied'}</span>` : `<div class="row"><button class="btn pri sm" data-ap="${esc(ap.call_id)}" data-d="approve">${icon('check','sm')}Approve and run</button><button class="btn sm" data-ap="${esc(ap.call_id)}" data-d="deny">Deny</button></div>`}</div></div>`).join('')}
        ${m.error ? `<div class="inline-err">${esc(m.error)}</div>` : ''}</div>`).join('');
    $$('[data-ap]', chat).forEach(b => b.onclick = () => decide(b.dataset.ap, b.dataset.d));
    const body = chat.parentElement; body.scrollTop = body.scrollHeight; };
  async function stream(url, body) {
    const cur = { role:'assistant', text:'', tools:[], approvals:[] }; A.msgs.push(cur); A.busy = true; paint();
    try { const r = await api(url, { method:'POST', body, raw:true, headers:{ Accept:'text/event-stream' } });
      if (!r.ok) { let t = ''; try { const j = await r.json(); t = j.detail || JSON.stringify(j); } catch { t = 'HTTP '+r.status; } throw new Error(t); }
      const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
      for (;;) { const { value, done } = await rd.read(); if (done) break; buf += dec.decode(value, { stream:true });
        let ix; while ((ix = buf.indexOf('\n\n')) >= 0) { const blk = buf.slice(0, ix); buf = buf.slice(ix+2); let ev = 'message', data = '';
          blk.split('\n').forEach(l => { if (l.startsWith('event:')) ev = l.slice(6).trim(); else if (l.startsWith('data:')) data += l.slice(5).trim(); });
          let d = {}; try { d = data ? JSON.parse(data) : {}; } catch { d = { text:data }; }
          if (ev==='message' && d.type) ev = d.type;
          if (ev==='session') A.session = d.session_id || A.session;
          else if (ev==='token') cur.text += d.text || '';
          else if (ev==='tool_call') cur.tools.push({ name:d.tool || d.name, args:d.args });
          else if (ev==='tool_result') { const t = cur.tools.find(x => !x.done); if (t) t.done = true; }
          else if (ev==='approval_required') cur.approvals.push(d);
          else if (ev==='approval_decision') { const a = cur.approvals.find(x => x.call_id===d.call_id); if (a) a.decision = d.decision; }
          else if (ev==='final') { if (d.text) cur.text = d.text; }
          else if (ev==='error') cur.error = d.detail || d.message || 'The agent hit an error.';
          paint(); } }
    } catch (e) { cur.error = e.message; }
    cur.tools.forEach(t => t.done = true); A.busy = false; paint();
  }
  async function decide(callId, decision) { A.msgs.forEach(m => (m.approvals||[]).forEach(a => { if (a.call_id===callId) a.decision = decision; })); paint();
    await stream('/api/agent/chat/approve', { session_id:A.session, call_id:callId, decision, comment:'' }); invalidate(...X.CORE); }
  const send = async text => { text = (text || inp.value).trim(); if (!text || A.busy || !chats.length) return; inp.value = '';
    const pre = !A.msgs.length && (ctx.pipeline || ctx.issue) ? `[Context: ${ctx.pipeline ? 'pipeline '+ctx.pipeline.name+' ('+ctx.pipeline.id+')' : 'issue: '+ctx.issue.title+'. '+ctx.issue.detail}]\n` : '';
    A.msgs.push({ role:'user', text }); $('#asug', dr).style.display = 'none';
    await stream('/api/agent/chat', { messages:[{ role:'user', content:pre+text }], ...(A.session ? { session_id:A.session } : {}), ...(A.model ? { model_id:A.model } : {}) }); };
  $('#asend', dr).onclick = () => send(); inp.onkeydown = e => { if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
  $$('#asug button', dr).forEach(b => b.onclick = () => send(b.textContent));
  $('#anew', dr).onclick = () => { A.session = null; A.msgs = []; $('#asug', dr).style.display = ''; paint(); };
  const ms_ = $('#amodel', dr); if (ms_) ms_.onchange = () => { A.model = ms_.value; localStorage.setItem('ov-agent-model', A.model); };
  paint(); if (A.msgs.length) $('#asug', dr).style.display = 'none';
  if (prompt) send(prompt); else inp.focus();
};

/* ---------- models ---------- */
const pid = () => ({ principal:((D.publish||{}).capabilities||{}).principal_id || null });
route('models', { needs:[...X.CORE, 'transforms', 'publish'], render(parts) {
  if (parts[0]==='new') return newModel();
  if (parts[0]==='recipes' && parts[1]) return X.recipeDetail(parts.slice(1).join('/'));
  if (parts[0] && parts[0]!=='recipes') return X.modelDetail(parts[0]);
  const tab = parts[0]==='recipes' ? 'recipes' : 'models';
  const emb = M.models.filter(m => m.model_category!=='chat'), chat = M.models.filter(m => m.model_category==='chat'), tr = D.transforms || [];
  const auth = m => m.auth_type==='managed-identity' || (m.endpoint && !m.api_key) ? 'Managed identity' : m.api_key ? 'API key' : m.endpoint ? '—' : 'In-cluster';
  const mtbl = (list, isChat) => `<table class="tbl"><thead><tr><th>Model</th><th>Provider</th>${isChat ? '' : '<th class="r">Dims</th>'}<th>Endpoint</th><th>Auth</th><th>Health</th><th class="r">Used by</th><th></th></tr></thead><tbody>
    ${list.map(m => { const h = M.hm[m.id], used = X.usedBy(m.id).length;
      return `<tr class="clickrow" data-href="#/models/${m.id}"><td><div class="row" style="gap:10px">${logo(m.type||'native')}<div style="min-width:0"><div class="nm">${esc(m.deployment||m.name)}</div><div class="muted small mono">${esc(m.id)}</div></div></div></td>
       <td class="small">${m.type ? esc(T(m.type).short) : 'OmniVec native'}</td>
       ${isChat ? '' : `<td class="r num">${m.embedding_dim ? esc(m.embedding_dim) : '<span class="muted small">Not reported</span>'}</td>`}
       <td class="mono small muted" style="max-width:260px">${esc((m.endpoint||'in-cluster').replace(/^https?:\/\//,''))}</td><td class="small">${auth(m)}</td>
       <td>${h ? `<span class="badge ${h.status==='healthy'?'ok':'warn'}">${dot(h.status==='healthy'?'ok':'warn')}${h.status==='healthy'?'Healthy':esc(h.status)}</span>` : '<span class="badge out">Not checked</span>'}<div class="small" id="mt-${m.id}"></div></td>
       <td class="r num">${used ? plural(used,'pipeline') : '<span class="muted">Unused</span>'}</td><td class="r"><button class="btn ghost sm icon" data-mm="${m.id}">${icon('more','sm')}</button></td></tr>`; }).join('')}</tbody></table>`;
  const exts = t => { const a = t.applies_to; const l = Array.isArray(a) ? a : a ? [...(a.extensions||[]), ...(a.content_types||[])] : [];
    return l.length ? l.slice(0,8).map(x => `<span class="badge out mono" title="${esc(typeof x==='string' ? x : JSON.stringify(x))}">${esc(typeof x==='string' ? x : JSON.stringify(x))}</span>`).join(' ') + (l.length > 8 ? ` <span class="muted small">+${l.length-8}</span>` : '') : '<span class="muted">All files</span>'; };
  const stages = t => (t.stages||[]).map(s => esc(typeof s==='string' ? s : s.name || s.type)).join(' <span class="muted">→</span> ');
  return { crumbs: tab==='recipes' ? [['Models & recipes','#/models'],['Recipes']] : [['Models & recipes']], html:`<div class="page"><div class="ph"><div><h1>Models & recipes</h1><p>Embedding models turn content into vectors. Recipes combine extraction, chunking and embedding for specific file types. Chat models power the agent.</p></div>
    <div class="acts">${tab==='models' ? `<a class="btn pri" href="#/models/new">${icon('plus','sm')}Register model</a>` : ''}</div></div>
   <div class="tabs"><a href="#/models" class="${tab==='models'?'on':''}">${icon('cube','sm')}Models <span class="ct">${M.models.length}</span>${chat.length ? '' : '<span class="dot warn" title="No chat model: the agent can\'t answer" style="margin-left:4px"></span>'}</a><a href="#/models/recipes" class="${tab==='recipes'?'on':''}">${icon('layers','sm')}Recipes <span class="ct">${tr.length}</span></a></div>
   ${tab==='models' ? `
   <div class="eyebrow">Embedding models <span class="ct">${emb.length}</span></div><div class="card" style="margin:8px 0 22px">${emb.length ? mtbl(emb) : empty('cube','No embedding models','Register an Azure OpenAI embedding deployment to build pipelines.', '<a class="btn sm pri" href="#/models/new">Register embedding model</a>')}</div>
   <div class="eyebrow">Chat models (agent) <span class="ct">${chat.length}</span></div><div class="card" style="margin:8px 0 22px">${chat.length ? mtbl(chat, true) : empty('bot','No chat model yet','The agent can\'t answer until an Azure OpenAI chat deployment is registered.', '<a class="btn sm pri" href="#/models/new?cat=chat">Register chat model</a>')}</div>
   ` : `<p class="muted small" style="margin:0 0 12px">Recipes are processing templates (extract, chunk, embed) chosen per file type when you build a pipeline.</p><div class="card">${tr.length ? `<table class="tbl"><thead><tr><th>Recipe</th><th>Applies to</th><th>Stages</th><th>Description</th></tr></thead><tbody>${tr.map(t => `<tr class="clickrow" data-href="#/models/recipes/${encodeURIComponent(t.name)}"><td style="white-space:nowrap"><div class="nm">${esc(t.name)}</div><span class="muted small">v${esc(t.version||'1')}</span></td><td class="small tags" style="max-width:280px">${exts(t)}</td><td class="small">${stages(t)}</td><td class="small muted">${esc(t.description||'')}</td></tr>`).join('')}</tbody></table>` : empty('layers','No recipes','DocGrok recipes appear here when the service is running.')}</div>`}</div>`,
   mount(v) { $$('tr[data-href]', v).forEach(r => r.onclick = e => { if (!e.target.closest('button,a')) go(r.dataset.href); });
     $$('[data-mm]', v).forEach(b => b.onclick = () => { const m = M.MDL[b.dataset.mm];
     X.menu(b, [{ label:'Open details', icon:'chev', run:() => go('#/models/'+m.id) }, { label:'Test model', icon:'play', run: async () => { const e = $('#mt-'+m.id, v); e.style.color = 'var(--err)'; e.innerHTML = spinner('Testing…');
         try { const r = await api(`/api/models/${m.id}/test`, { method:'POST' }); e.style.color = r.success===false ? 'var(--err)' : 'var(--ok)'; e.textContent = r.success===false ? (r.error||'Test failed') : 'Model responded' + (r.dimensions||r.embedding_dim ? ` · ${r.dimensions||r.embedding_dim} dims` : '') + (r.latency_ms ? ` · ${ms(r.latency_ms)}` : ''); }
         catch (er) { e.textContent = er.message; } } },
       { label:'Access guidance', icon:'shield', run:() => { openModal(`<div class="modal-h"><h3>Grant access to ${esc(m.deployment||m.name)}</h3></div><div class="modal-b">${X.perm.guideHtml('open','azure-openai', m, pid())}</div><div class="modal-f"><button class="btn" data-close>Close</button></div>`, 'wide'); } },
       '-', { label:'Delete model', icon:'trash', danger:true, run:() => { const u = X.usedBy(m.id).length; confirmAction({ title:`Delete ${m.deployment||m.name}?`, danger:true, typed: u ? null : m.name,
         body: u ? `This model is used by ${plural(u,'pipeline')}. Change those pipelines first.` : 'The registration is removed. Vectors already written are not affected.', confirm:'Delete',
         onOk: async () => { if (u) return; await api('/api/models/'+m.id, { method:'DELETE' }); toast('Model deleted'); invalidate('models'); X.render({ force:true }); } }); } }]); }); } };
}});
function newModel() {
  const qp = X.query(); const cat = qp.get('cat') || 'embedding';
  return { crumbs:[['Models & recipes','#/models'],['Register model']], html:`<div class="page"><div class="ph"><div><h1>Register an Azure OpenAI model</h1><p>OmniVec calls the deployment with its managed identity, so no keys are stored unless you choose to.</p></div></div>
    <div class="grid g-main" style="align-items:start"><div class="card"><div class="card-b stack s12">
     <div class="fld"><label>Purpose</label><div class="seg" id="mcat"><button data-c="embedding" class="${cat==='embedding'?'on':''}">Embedding</button><button data-c="chat" class="${cat==='chat'?'on':''}">Chat (agent)</button></div></div>
     <div class="grid g2"><div class="fld"><label>Display name</label><input class="inp" id="mn" placeholder="${cat==='chat'?'gpt-4o':'text-embedding-3-small'}"></div><div class="fld"><label>Deployment name</label><input class="inp mono" id="md" placeholder="${cat==='chat'?'gpt-4o':'text-embedding-3-small'}"></div></div>
     <div class="fld"><label>Endpoint</label><input class="inp mono" id="me" placeholder="https://<resource>.openai.azure.com/"></div>
     <div class="grid g2">${cat==='embedding' ? `<div class="fld"><label>Dimensions</label><input class="inp" type="number" id="mdim" value="1536"><span class="hint">Must match the vector store's index.</span></div>` : ''}<div class="fld"><label>API version</label><input class="inp mono" id="mv" value="2024-06-01"></div></div>
     <div class="fld"><label>Authentication</label><div class="seg" id="mauth"><button data-a="managed-identity" class="on">Managed identity</button><button data-a="key">API key</button></div></div>
     <div id="mkey" style="display:none" class="fld"><label>API key</label><input class="inp mono" type="password" id="mk"><span class="hint">Stored in Key Vault when configured.</span></div>
     <div class="inline-err" id="merr"></div>
     <div class="row sp" style="border-top:1px solid var(--border);padding-top:14px"><a class="btn" href="#/models">Cancel</a><button class="btn pri" id="msave">${icon('check','sm')}Register</button></div></div></div>
    <div class="card" style="position:sticky;top:12px"><div class="card-h"><h3>${icon('shield','sm')} Grant access</h3></div><div class="card-b">${X.perm.guideHtml('open','azure-openai', {}, pid())}</div></div></div></div>`,
    mount(v) { let auth = 'managed-identity';
      $$('[data-c]', v).forEach(b => b.onclick = () => go('#/models/new?cat='+b.dataset.c+(qp.get('then') ? '&then='+qp.get('then') : '')));
      $$('[data-a]', v).forEach(b => b.onclick = () => { auth = b.dataset.a; $$('[data-a]', v).forEach(x => x.classList.toggle('on', x===b)); $('#mkey', v).style.display = auth==='key' ? '' : 'none'; });
      $('#md', v).oninput = e => { if (!$('#mn', v).dataset.t) $('#mn', v).value = e.target.value; }; $('#mn', v).oninput = e => e.target.dataset.t = 1;
      $('#msave', v).onclick = async e => { const b = e.currentTarget, g = id => ($('#'+id, v)||{}).value;
        const body = { name:(g('mn')||g('md')||'').trim(), deployment:(g('md')||'').trim(), endpoint:(g('me')||'').trim(), provider_type:'azure-openai', model_category:cat, auth_type:auth, api_version:g('mv'), ...(cat==='embedding' ? { embedding_dim:+g('mdim') } : {}), ...(auth==='key' ? { api_key:g('mk') } : {}) };
        if (!body.deployment || !/^https:\/\//.test(body.endpoint)) { $('#merr', v).textContent = 'Enter the deployment name and an https endpoint.'; return; }
        b.disabled = true; b.innerHTML = spinner('Registering…');
        try { await api('/api/models', { method:'POST', body }); toast('Model registered'); invalidate('models','health'); go(qp.get('then')==='new' ? '#/new' : '#/models'); }
        catch (er) { $('#merr', v).textContent = er.message; b.disabled = false; b.innerHTML = icon('check','sm')+'Register'; } }; } };
}

/* ---------- publish ---------- */
const jobBadge = s => `<span class="badge ${s==='succeeded'?'ok':/fail|interrupt/.test(s)?'err':/await/.test(s)?'warn':'acc'}">${esc(String(s||'').replace(/_/g,' '))}</span>`;
route('publish', { needs:[...X.CORE, 'publish'], render() {
  const P = D.publish || {}, cap = P.capabilities || {}, jobs = P.jobs || [];
  const kinds = [['mcp','MCP server','Expose vector search as an MCP tool for Copilot, Claude or any MCP client.','terminal'],['foundry','Azure AI Foundry agent','Create a Foundry agent grounded on your vector store through the MCP server.','bot'],['cosmos_container','Cosmos DB vector container','Provision a new container with a vector policy that matches a model.','db']];
  return { crumbs:[['Publish to AI apps']], html:`<div class="page"><div class="ph"><div><h1>Publish to AI apps</h1><p>Deploy a search endpoint your AI apps can call. Every deployment is planned first; nothing is created in Azure until you approve the plan, its cost and its permissions.</p></div></div>
   ${loadErr(['publish'])}
   ${cap.enabled===false ? `<div class="callout warn">${icon('lock','ic')}<div class="grow"><b>Cloud deployment is not enabled</b><div>${esc(cap.message || 'An administrator must grant the OmniVec identity rights on a resource group.')}</div>${cap.principal_id ? `<div style="margin-top:8px">Grant <b>Contributor</b> on the target resource group to principal:</div>${codebox(cap.principal_id)}` : ''}</div></div>` : ''}
   <div class="grid g3" style="margin-top:14px">${kinds.map(([k,l,d,ic]) => `<div class="opt" data-kind="${k}"><span class="logo" style="background:var(--accent-soft);color:var(--accent-text)">${icon(ic,'sm')}</span><div><div class="t">${l}</div><div class="d">${d}</div></div></div>`).join('')}</div>
   <div id="pform"></div>
   <div class="card" style="margin-top:18px"><div class="card-h"><h3>Deployments</h3></div>${jobs.length ? `<table class="tbl"><thead><tr><th>Kind</th><th>Status</th><th>Target</th><th>Created</th><th></th></tr></thead><tbody>${jobs.map(j => `<tr><td><b>${esc(j.kind)}</b></td><td>${jobBadge(j.status)}</td><td class="small mono trunc" style="max-width:320px">${esc((j.plan||{}).resource_group_id || (j.request||{}).resource_group_id || '')}</td><td class="small muted">${ago(j.created_at)}</td><td class="r"><button class="btn sm" data-job="${esc(j.id)}">Open</button></td></tr>`).join('')}</tbody></table>` : empty('send','Nothing published yet','Choose what to publish above.')}</div></div>`,
   mount(v) { $$('[data-kind]', v).forEach(o => o.onclick = () => { $$('[data-kind]', v).forEach(x => x.classList.toggle('on', x===o)); planForm($('#pform', v), o.dataset.kind, cap); });
     $$('[data-job]', v).forEach(b => b.onclick = () => showJob(jobs.find(j => j.id===b.dataset.job))); } };
}});
function planForm(el, kind, cap) {
  const scopes = cap.allowed_scopes || [];
  const pub = (D.publish && D.publish.jobs || []).filter(j => j.kind==='mcp' && j.status==='succeeded');
  el.innerHTML = `<div class="card" style="margin-top:14px"><div class="card-b stack s12"><div class="grid g2">
    <div class="fld"><label>Resource group</label>${scopes.length ? `<select class="sel mono" id="prg">${scopes.map(s => `<option>${esc(s)}</option>`).join('')}</select>` : `<input class="inp mono" id="prg" placeholder="/subscriptions/…/resourceGroups/…">`}</div>
    <div class="fld"><label>Region</label><input class="inp mono" id="ploc" value="eastus2"></div>
    ${kind!=='foundry' ? `<div class="fld"><label>Vector store</label><select class="sel" id="pdst">${M.dests.filter(d => d.type==='cosmosdb-vector').map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('')}</select></div>
     <div class="fld"><label>Embedding model</label><select class="sel" id="pmdl">${M.models.filter(m => m.model_category!=='chat').map(m => `<option value="${m.id}">${esc(m.deployment||m.name)} · ${m.embedding_dim}d</option>`).join('')}</select></div>` : ''}
    ${kind==='mcp' ? `<div class="fld"><label>Fields returned</label><input class="inp mono" id="pfields" value="id,title,text,source_ref"></div><div class="fld"><label>Vector field</label><input class="inp mono" id="pvf" value="embedding"></div>` : ''}
    ${kind==='foundry' ? `<div class="fld"><label>MCP deployment</label><select class="sel" id="pmcp">${pub.map(j => `<option value="${esc(j.id)}">${esc(j.id)}</option>`).join('')}</select>${pub.length ? '' : '<span class="hint">Publish an MCP server first.</span>'}</div><div class="fld"><label>Foundry project resource ID</label><input class="inp mono" id="pproj"></div><div class="fld"><label>Chat deployment</label><input class="inp mono" id="pchat" value="gpt-4o"></div>` : ''}
    ${kind==='cosmos_container' ? `<div class="fld"><label>Database</label><input class="inp mono" id="pdb"></div><div class="fld"><label>Container</label><input class="inp mono" id="pct"></div>` : ''}</div>
    <div class="inline-err" id="perr"></div><div class="row" style="justify-content:flex-end"><button class="btn pri" id="pplan">${icon('book','sm')}Prepare plan</button></div></div></div>`;
  $('#pplan', el).onclick = async e => { const b = e.currentTarget, g = id => { const x = $('#'+id, el); return x ? x.value.trim() : undefined; };
    const body = { kind, resource_group_id:g('prg'), location:g('ploc') };
    [['destination_id','pdst'],['embedding_model_id','pmdl'],['fields','pfields'],['vector_field','pvf'],['mcp_deployment_id','pmcp'],['project_resource_id','pproj'],['chat_deployment','pchat'],['database','pdb'],['container','pct']].forEach(([k,id]) => { const val = g(id); if (val) body[k] = val; });
    b.disabled = true; b.innerHTML = spinner('Planning…');
    try { const j = await api('/api/cloud-deployments/plan', { method:'POST', body }); invalidate('publish'); showJob(j); } catch (er) { $('#perr', el).textContent = er.message; }
    b.disabled = false; b.innerHTML = icon('book','sm')+'Prepare plan'; };
}
function showJob(j) { if (!j) return; const pl = j.plan || {};
  const dr = openDrawer(`<div class="drawer-h"><h3>${esc(j.kind)} deployment</h3>${jobBadge(j.status)}<button class="btn ghost sm icon" data-close style="margin-left:auto">${icon('x','sm')}</button></div>
   <div class="drawer-b stack s12">${pl.summary ? `<div class="t2">${esc(pl.summary)}</div>` : ''}
    ${pl.estimated_monthly_cost || pl.cost ? `<div class="callout info">${icon('info','ic')}<div><b>Estimated cost</b><div>${esc(pl.estimated_monthly_cost || JSON.stringify(pl.cost))}</div></div></div>` : ''}
    ${(pl.role_assignments||pl.permissions||[]).length ? `<div class="eyebrow">Permissions granted</div>${(pl.role_assignments||pl.permissions).map(r => `<div class="chk-row">${icon('shield','sm')}<span class="small">${esc(typeof r==='string' ? r : [r.role, r.scope].filter(Boolean).join(' on '))}</span></div>`).join('')}` : ''}
    ${j.error ? `<div class="inline-err">${esc(j.error)}</div>` : ''}${j.result ? `<div class="eyebrow">Result</div><div class="code small" style="white-space:pre-wrap">${esc(JSON.stringify(j.result, null, 2))}</div>` : ''}
    <details><summary class="link small">Full plan</summary><div class="code small" style="white-space:pre-wrap;margin-top:8px">${esc(JSON.stringify(pl, null, 2))}</div></details></div>
   ${/await|failed|interrupt/.test(j.status) ? `<div class="drawer-f"><label class="check grow"><input type="checkbox" id="jok"> I approve the cost and permissions in this plan</label><button class="btn pri" id="japp" disabled>Approve and deploy</button></div>` : ''}`, true);
  const ok = $('#jok', dr), ap = $('#japp', dr);
  if (ok) { ok.onchange = () => ap.disabled = !ok.checked;
    ap.onclick = async () => { ap.disabled = true; ap.innerHTML = spinner('Starting…');
      try { const r = await api(`/api/cloud-deployments/${j.id}/approve`, { method:'POST', body:{ plan_hash:j.plan_hash, approve_cost_and_permissions:true } }); toast('Deployment started'); invalidate('publish'); closeOverlay(); X.render({ force:true }); }
      catch (e) { ap.disabled = false; ap.textContent = 'Approve and deploy'; toast(e.message, 'err'); } }; }
}

/* ---------- settings ---------- */
route('settings', { needs:['sources'], live:false, render() {
  const u = X.session.user || {};
  return { crumbs:[['Settings']], html:`<div class="page"><div class="ph"><div><h1>Settings</h1><p>Access, configuration backup and preferences for this workspace.</p></div></div>
   <div class="grid g-main" style="align-items:start"><div class="stack">
    <div class="card"><div class="card-h"><h3>${icon('key','sm')} Access tokens</h3><button class="btn sm" id="tnew" style="margin-left:auto">${icon('plus','sm')}New token</button></div><div id="tlist">${`<div class="card-b">${spinner('Loading…')}</div>`}</div></div>
    <div class="card"><div class="card-h"><h3>${icon('box','sm')} Backup and move configuration</h3></div><div class="card-b stack s12"><div class="t2">Export sources, vector stores, models and pipelines as a JSON bundle. Secrets are not exported. Import into another OmniVec workspace to recreate them.</div>
     <div class="row"><button class="btn" id="exp">${icon('download','sm')}Export bundle</button><label class="btn">${icon('upload','sm')}Import bundle<input type="file" id="imp" accept=".json,application/json" hidden></label>
      <select class="sel" id="impc" style="width:190px"><option value="skip">On conflict: skip</option><option value="rename">On conflict: rename</option><option value="overwrite">On conflict: overwrite</option></select></div><div id="impout"></div></div></div></div>
   <div class="stack"><div class="card"><div class="card-h"><h3>You</h3></div><div class="card-b stack s12"><dl class="kv"><dt>Name</dt><dd>${esc(u.name||'—')}</dd><dt>Role</dt><dd>${esc(u.role||'—')}</dd><dt>API</dt><dd class="mono small">${esc(location.origin)}</dd></dl>
     <div class="row"><button class="btn" data-act="theme">${icon('moon','sm')}Toggle theme</button><button class="btn" data-act="signout">${icon('logout','sm')}Sign out</button></div></div></div>
    <div class="card"><div class="card-h"><h3>Classic console</h3></div><div class="card-b stack s8"><div class="muted small">The previous console remains available while the new experience rolls out.</div><a class="btn" href="/classic.html">${icon('ext','sm')}Open classic console</a></div></div></div></div></div>`,
   mount(v) {
     const load = async () => { const el = $('#tlist', v);
       try { const r = await api('/api/auth/tokens'); const ts = (r.tokens||[]).sort((a,b) => (a.revoked-b.revoked) || String(b.created_at).localeCompare(a.created_at));
         el.innerHTML = ts.length ? `<table class="tbl"><thead><tr><th>Name</th><th>Access</th><th>Last used</th><th>Expires</th><th></th></tr></thead><tbody>${ts.map(t => `<tr ${t.revoked?'style="opacity:.5"':''}><td><b>${esc(t.name)}</b><div class="muted small mono">${esc(t.id)}</div></td><td><span class="badge ${t.scope==='search'?'acc':''}">${t.scope==='search'?'Search only':esc(t.role)}</span></td><td class="small muted">${t.last_used_at ? ago(t.last_used_at)+' · '+fmt(t.use_count)+' uses' : 'Never'}</td><td class="small muted">${t.expires_at ? when(t.expires_at) : 'Never'}</td>
           <td class="r">${t.revoked ? '<span class="badge">Revoked</span>' : `<button class="btn sm" data-rev="${esc(t.id)}" data-n="${esc(t.name)}">Revoke</button>`}</td></tr>`).join('')}</tbody></table>` : `<div class="card-b">${empty('key','No extra tokens','Create a search-only token for AI apps, or an admin token for teammates.')}</div>`;
         $$('[data-rev]', el).forEach(b => b.onclick = () => confirmAction({ title:`Revoke “${b.dataset.n}”?`, body:'Apps using this token stop working immediately.', confirm:'Revoke', danger:true, typed:b.dataset.n,
           onOk: async () => { await api('/api/auth/tokens/'+b.dataset.rev, { method:'DELETE' }); toast('Token revoked'); load(); } }));
       } catch (e) { el.innerHTML = `<div class="card-b muted">${e.status===403 ? 'Only administrators can manage tokens.' : esc(e.message)}</div>`; } };
     load();
     $('#tnew', v).onclick = () => { const m = openModal(`<div class="modal-h"><h3>New access token</h3></div><div class="modal-b stack s12"><div class="fld"><label>Name</label><input class="inp" id="tn" placeholder="e.g. support-bot"></div>
        <div class="fld"><label>Access</label><select class="sel" id="ts"><option value="search">Search only (for AI apps)</option><option value="user">Admin API · user role</option><option value="admin">Admin API · admin role</option></select></div>
        <div class="fld"><label>Expires</label><select class="sel" id="te"><option value="90">In 90 days</option><option value="30">In 30 days</option><option value="365">In 1 year</option><option value="">Never</option></select></div><div class="inline-err" id="terr"></div><div id="tout"></div></div>
        <div class="modal-f"><button class="btn" data-close>Close</button><button class="btn pri" id="tok2">Create</button></div>`);
       $('#tok2', m).onclick = async e => { const s = $('#ts', m).value, n = $('#tn', m).value.trim(); if (!n) { $('#terr', m).textContent = 'Name the token.'; return; }
         try { const r = await api('/api/auth/tokens', { method:'POST', body:{ name:n, scope:s==='search'?'search':'admin', role:s==='admin'?'admin':'user', expires_days:$('#te', m).value ? +$('#te', m).value : null } });
           $('#tout', m).innerHTML = `<div class="callout warn">${icon('lock','ic')}<div class="grow"><b>Copy it now. It won't be shown again.</b>${codebox(r.token)}</div></div>`; e.currentTarget.remove(); load(); }
         catch (er) { $('#terr', m).textContent = er.message; } }; };
     $('#exp', v).onclick = async () => { try { const r = await api('/api/admin/export'); const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(r, null, 2)], { type:'application/json' }));
       a.download = `omnivec-bundle-${new Date().toISOString().slice(0,10)}.json`; a.click(); toast('Bundle exported'); } catch (e) { toast(e.message, 'err'); } };
     $('#imp', v).onchange = async e => { const f = e.target.files[0]; if (!f) return; const out = $('#impout', v); let bundle;
       try { bundle = JSON.parse(await f.text()); } catch { out.innerHTML = '<div class="inline-err">This file is not valid JSON.</div>'; return; }
       const oc = $('#impc', v).value; out.innerHTML = spinner('Checking what would change…');
       try { const dry = await api(`/api/admin/import?on_conflict=${oc}&dry_run=true`, { method:'POST', body:bundle });
         out.innerHTML = `<div class="code small" style="white-space:pre-wrap;max-height:220px;overflow:auto">${esc(JSON.stringify(dry.summary || dry, null, 2))}</div><button class="btn pri" id="impgo" style="margin-top:8px">Import now</button>`;
         $('#impgo', v).onclick = async () => { try { await api(`/api/admin/import?on_conflict=${oc}`, { method:'POST', body:bundle }); toast('Bundle imported'); invalidate(...X.CORE); out.innerHTML = ''; } catch (er) { toast(er.message, 'err'); } }; }
       catch (er) { out.innerHTML = `<div class="inline-err">${esc(er.message)}</div>`; } e.target.value = ''; };
   } };
}});
})();
