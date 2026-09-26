/* OmniVec console: permissions assistant. Explains identity, role and scope, checks access, and hands admins exact commands. No grants run in the browser. */
(() => {
const X = window.OVX;
const { icon, esc, api, D, M, T, codebox, spinner, toast, $, $$ } = X;

/* principal (object) ID of the OmniVec workload identity, discovered from API responses */
let principalCache = null, clientCache = null;
async function identity() {
  if (!clientCache) { const withClient = [...(D.dests||[]), ...(D.sources||[])].find(x => x.config && x.config.client_id); if (withClient) clientCache = withClient.config.client_id; }
  if (principalCache) return { principal: principalCache, client: clientCache };
  try { const d = D.publish || await api('/api/cloud-deployments'); D.publish = d; principalCache = (d && d.capabilities && d.capabilities.principal_id) || null; } catch { /* discovered later from a check */ }
  return { principal: principalCache, client: clientCache };
}
const acct = (url) => { try { return new URL(url).hostname.split('.')[0]; } catch { return '<account-name>'; } };

/* per-connector guidance: who needs what, where, and why */
const GUIDE = {
  'azure-blob': c => ({
    role:'Storage Blob Data Reader', scope:`Container “${c.container||'<container>'}” in storage account ${acct(c.account_url)}`,
    why:'OmniVec lists and reads blobs to extract text. It never writes to or deletes from your storage account.',
    portal:[`Open the storage account <b>${esc(acct(c.account_url))}</b> in the Azure portal, then <b>Containers → ${esc(c.container||'<container>')}</b>.`,
      'Select <b>Access control (IAM) → Add → Add role assignment</b>.', 'Choose <b>Storage Blob Data Reader</b>, then <b>Managed identity</b> as the member type.',
      'Select the OmniVec workload identity (the principal ID shown above) and save.', 'Optional, for automatic ingestion: create an Event Grid subscription for BlobCreated events. OmniVec can do this for you from the source page.'],
    cli: p => `az role assignment create \\\n  --assignee-object-id ${p} --assignee-principal-type ServicePrincipal \\\n  --role "Storage Blob Data Reader" \\\n  --scope "$(az storage account show -n ${acct(c.account_url)} --query id -o tsv)/blobServices/default/containers/${c.container||'<container>'}"`,
    checkable:true }),
  'cosmosdb': c => ({
    role:'Cosmos DB Built-in Data Reader', scope:`/dbs/${c.database||'<database>'}/colls/${c.container||'<container>'} in account ${acct(c.endpoint)}`,
    why:'OmniVec reads documents and the change feed so updates flow automatically. Cosmos DB data roles are separate from Azure RBAC roles like Reader or Contributor.',
    portal:['Cosmos DB data-plane roles cannot be assigned in the portal IAM blade. Use Azure CLI or PowerShell (Cloud Shell works).', 'Run the command below as someone with <b>Cosmos DB Operator</b> or <b>Owner</b> on the account.', 'Wait 2 to 5 minutes for the assignment to propagate, then select <b>Check again</b>.'],
    cli: p => `az cosmosdb sql role assignment create \\\n  --account-name ${acct(c.endpoint)} \\\n  --resource-group "$(az cosmosdb list --query \"[?name=='${acct(c.endpoint)}'].resourceGroup\" -o tsv)" \\\n  --role-definition-id 00000000-0000-0000-0000-000000000001 \\\n  --principal-id ${p} \\\n  --scope "/dbs/${c.database||'<database>'}/colls/${c.container||'<container>'}"`,
    checkable:true }),
  'cosmosdb-vector': c => ({
    role:'Cosmos DB Built-in Data Contributor', scope:`/dbs/${c.database||'<database>'}/colls/${c.container||'<container>'} in account ${acct(c.endpoint)}`,
    why:'OmniVec writes, updates and deletes vector documents. The container also needs a vector embedding policy and a vector index whose dimensions match your embedding model.',
    portal:['Create the container with a <b>vector embedding policy</b> (path such as <span class="mono">/embedding</span>, float32, dimensions matching your model, e.g. 1536) and a <b>diskANN</b> or <b>quantizedFlat</b> vector index.', 'Assign the data role with the command below (data-plane roles are not in the portal IAM blade).', 'Wait 2 to 5 minutes, then select <b>Check again</b>.'],
    cli: p => `az cosmosdb sql role assignment create \\\n  --account-name ${acct(c.endpoint)} \\\n  --resource-group "$(az cosmosdb list --query \"[?name=='${acct(c.endpoint)}'].resourceGroup\" -o tsv)" \\\n  --role-definition-id 00000000-0000-0000-0000-000000000002 \\\n  --principal-id ${p} \\\n  --scope "/dbs/${c.database||'<database>'}/colls/${c.container||'<container>'}"`,
    checkable:true }),
  'sharepoint': (c, id) => ({
    role:'Microsoft Graph · Sites.Selected (application) + read on this site', scope:`Site ${c.site_id ? String(c.site_id).split(',')[0] : '<site>'}${c.folder_path ? ' · folder '+c.folder_path : ''}`,
    why:'Sites.Selected limits OmniVec to the sites you explicitly grant. Avoid Sites.Read.All or Files.Read.All: they expose every site in the tenant. Same-tenant setups reuse the OmniVec workload identity, so no app registration is needed.',
    portal:['A <b>Global Administrator</b> or <b>Privileged Role Administrator</b> grants the Graph application permission <b>Sites.Selected</b> to the OmniVec identity (step 1 below).', 'A <b>SharePoint administrator</b> or site owner grants <b>read</b> on the specific site (step 2).', 'For a site in another tenant, add a federated credential for <span class="mono">system:serviceaccount:omnivec:omnivec-api</span> to an app in that tenant and set its tenant and client IDs on the pipeline.', 'Select <b>Check again</b>. Graph grants usually apply within a few minutes.'],
    cli: p => `# Microsoft Graph PowerShell (Install-Module Microsoft.Graph)\nConnect-MgGraph -Scopes "AppRoleAssignment.ReadWrite.All","Sites.FullControl.All"\n\n# 1. Grant the Sites.Selected application permission\n$graph = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"\n$role  = $graph.AppRoles | Where-Object { $_.Value -eq 'Sites.Selected' -and $_.AllowedMemberTypes -contains 'Application' }\nNew-MgServicePrincipalAppRoleAssignment -ServicePrincipalId ${p} -PrincipalId ${p} -ResourceId $graph.Id -AppRoleId $role.Id\n\n# 2. Grant read on this site only\nNew-MgSitePermission -SiteId "${c.site_id||'<site-id>'}" -BodyParameter @{\n  roles = @('read')\n  grantedToIdentities = @(@{ application = @{ id = '${id.client||'<workload-client-id>'}'; displayName = 'OmniVec' } })\n}`,
    shell:'PowerShell (Microsoft Graph)', checkable:true }),
  'onelake-iceberg': (c, _id, kind) => ({
    role: kind==='destination' ? 'Fabric workspace Contributor, plus Spark Job Definition execute' : 'Fabric workspace Viewer (read) on the lakehouse',
    scope:`Workspace ${c.workspace_id || (c.warehouse||'').split('/')[0] || '<workspace>'}${c.lakehouse_item_id ? ' · lakehouse '+c.lakehouse_item_id : ''}`,
    why: kind==='destination' ? 'OmniVec stages vectors in OneLake, runs your Spark Job Definition to merge them into the Iceberg table, and optionally mirrors to Garnet.' : 'OmniVec reads Iceberg table snapshots from OneLake and keeps a checkpoint so only new rows are processed.',
    portal:['In the Fabric admin portal, enable <b>Service principals can use Fabric APIs</b> (tenant setting) for a group that contains the OmniVec identity.', `Open the workspace, choose <b>Manage access → Add people or groups</b>, and add the OmniVec identity as <b>${kind==='destination'?'Contributor':'Viewer'}</b>.`, kind==='destination' ? 'Confirm the Spark Job Definition exists and the staging path is inside the lakehouse <span class="mono">Files/</span> area.' : 'Confirm the table is an Iceberg table and the content and ID fields exist.', 'For a Garnet mirror, store the password in Key Vault and grant the identity <b>Key Vault Secrets User</b>.'],
    cli: null, checkable:false }),
  'postgresql': c => ({
    role:'Database role with SELECT on the table', scope:`${c.host||'<host>'} / ${c.database||'<database>'} / ${c.table||'<table>'}`,
    why:'OmniVec polls the table using the ID and timestamp columns to find new or changed rows. It never writes to the source.',
    portal:['Connect as a database owner (psql, Azure Data Studio, or the portal query editor).', 'Run the SQL below. Replace <span class="mono">omnivec</span> with the user you enter in this form.', 'For Entra authentication on Azure Database for PostgreSQL, create the role with <span class="mono">pgaadauth_create_principal</span> first.'],
    cli: () => `GRANT CONNECT ON DATABASE "${c.database||'<database>'}" TO omnivec;\nGRANT USAGE ON SCHEMA public TO omnivec;\nGRANT SELECT ON TABLE ${c.table||'<table>'} TO omnivec;`, shell:'SQL', checkable:false }),
  'pgvector': c => ({
    role:'Database role with read and write on the vector table', scope:`${c.host||'<host>'} / ${c.database||'<database>'} / ${c.table||'<table>'}`,
    why:'OmniVec inserts, updates and deletes rows that hold the vector, text and metadata. The pgvector extension must be enabled.',
    portal:['Allow-list the <b>vector</b> extension (Azure Database for PostgreSQL: Server parameters → azure.extensions).', 'Run the SQL below as a database owner.', `Make sure the vector column has ${c.vector_dimensions||'<dimensions>'} dimensions to match your embedding model.`],
    cli: () => `CREATE EXTENSION IF NOT EXISTS vector;\nGRANT USAGE, CREATE ON SCHEMA public TO omnivec;\nGRANT SELECT, INSERT, UPDATE, DELETE ON TABLE ${c.table||'<table>'} TO omnivec;`, shell:'SQL', checkable:false }),
  'azure-openai': c => ({
    role:'Cognitive Services OpenAI User', scope:`Azure OpenAI account ${acct(c.endpoint)}`,
    why:'Lets OmniVec call the embedding deployment with its managed identity, so no API key is stored.',
    portal:[`Open the Azure OpenAI resource <b>${esc(acct(c.endpoint))}</b> → <b>Access control (IAM) → Add role assignment</b>.`, 'Choose <b>Cognitive Services OpenAI User</b>, member type <b>Managed identity</b>, and select the OmniVec identity.', 'Wait a few minutes, then use <b>Test</b> on the model.'],
    cli: p => `az role assignment create \\\n  --assignee-object-id ${p} --assignee-principal-type ServicePrincipal \\\n  --role "Cognitive Services OpenAI User" \\\n  --scope "$(az cognitiveservices account list --query \"[?name=='${acct(c.endpoint)}'].id\" -o tsv)"`, checkable:false }),
};
const STATUS = {
  read_verified:['ok','check','Access verified'], access_denied:['err','lock','Access denied'], authentication_failed:['err','key','Authentication failed'],
  network_unreachable:['err','alert','Network unreachable'], network_restricted:['warn','shield','Network restricted'], resource_missing:['warn','alert','Resource not found'],
  provisioning_required:['warn','wand','Provisioning required'], needs_input:['warn','info','More details needed'], unsupported:['info','info','Automatic check not available'], unknown:['info','info','Could not determine'],
};

/* guide block (no network): identity + role + scope + steps + command */
const guideFor = type => (typeof type === 'string' && Object.prototype.hasOwnProperty.call(GUIDE, type) && typeof GUIDE[type] === 'function') ? GUIDE[type] : null;
function guideHtml(kind, type, cfg, id) {
  const gf = guideFor(type); const g = gf ? gf(cfg||{}, id||{}, kind) : null;
  if (!g) return `<div class="muted">No permission guidance is available for ${esc(T(type).label)}.</div>`;
  const p = (id && id.principal) || '<omnivec-principal-id>';
  return `<div class="stack s12">
   <div class="ident"><span class="logo l-aoai">${icon('user','sm')}</span><div class="grow" style="min-width:0"><div style="font-weight:600">OmniVec workload identity</div>
     <div class="muted small">Principal (object) ID <span class="mono">${esc(id && id.principal || 'discovered when you check access')}</span>${id && id.client ? ` · client ID <span class="mono">${esc(id.client)}</span>` : ''}</div></div>
     ${id && id.principal ? `<button class="btn sm" data-copy="${esc(id.principal)}">${icon('copy','sm')}Copy ID</button>` : ''}</div>
   <dl class="kv"><dt>Needs</dt><dd><b>${esc(g.role)}</b></dd><dt>On</dt><dd class="mono small">${esc(g.scope)}</dd><dt>Why</dt><dd>${esc(g.why)}</dd></dl>
   <details ${kind==='open'?'open':''}><summary class="link small" style="list-style:none">${icon('book','sm')} Step-by-step for your administrator</summary>
    <div style="margin-top:8px">${g.portal.map((s,i) => `<div class="perm-step"><span class="n">${i+1}</span><div class="small">${s}</div></div>`).join('')}
    ${g.cli ? `<div class="eyebrow" style="margin:10px 0 6px">${esc(g.shell || 'Azure CLI (Bash or Cloud Shell)')}</div>${codebox(g.cli(p))}` : ''}</div></details></div>`;
}

/* full assistant: guide + live check + generated commands */
function permAssistant(el, opts) {
  // opts: { kind:'source'|'destination', type, getConfig:()=>cfg, resourceRef, compact }
  let ver = 0;
  const draw = async (result) => {
    const id = await identity(); if (result && result.principal_id) { principalCache = principalCache || result.principal_id; id.principal = id.principal || result.principal_id; }
    const type = opts.type || (result && result.connector_type); const cfg = opts.getConfig ? opts.getConfig() : {};
    const gf = guideFor(type); const g = gf ? gf(cfg, id, opts.kind) : null;
    el.innerHTML = `<div class="stack s12">
      ${opts.noGuide ? '' : guideHtml(opts.kind, type, cfg, id)}
      <div class="row" style="flex-wrap:wrap"><button class="btn ${result ? '' : 'pri'} sm" id="pchk">${icon('shield','sm')}${result ? 'Check again' : 'Check access'}</button>
        <span class="muted small">${g && !g.checkable ? 'Automatic verification is limited for this type; the test connection result is the best evidence.' : 'Read-only check. No roles are assigned and no data is written.'}</span></div>
      <details class="small"><summary class="muted" style="cursor:pointer">Advanced: override resource or identity</summary>
        <div class="grid g2" style="margin-top:8px"><div class="fld"><label>Account resource ID</label><input class="inp mono" id="prid" placeholder="/subscriptions/…/providers/…"></div>
        <div class="fld"><label>Principal (object) ID, not client ID</label><input class="inp mono" id="ppid" placeholder="${esc(id.principal||'')}"></div></div></details>
      <div id="pout" aria-live="polite">${result ? resultHtml(result) : ''}</div></div>`;
    $('#pchk', el).onclick = run;
  };
  const run = async () => {
    const v = ++ver; const out = $('#pout', el); const btn = $('#pchk', el); btn.disabled = true; btn.innerHTML = spinner('Checking…');
    out.innerHTML = `<div class="muted small">${spinner('Checking access from the OmniVec API workload…')}</div>`;
    const body = opts.resourceRef ? { kind:opts.kind, resource_ref:opts.resourceRef } : { kind:opts.kind, connector_type:opts.type, config:opts.getConfig() };
    const rid = ($('#prid', el)||{}).value, pid = ($('#ppid', el)||{}).value; if (rid) body.resource_id = rid.trim(); if (pid) body.principal_id = pid.trim();
    try { const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 35000);
      const r = await api('/api/permissions/check', { method:'POST', body, signal:ctl.signal }); clearTimeout(tm);
      if (v !== ver || !el.isConnected) return; opts.onResult && opts.onResult(r); draw(r); }
    catch (e) { if (v !== ver) return; out.innerHTML = `<div class="callout warn">${icon('alert','ic')}<div><b>No conclusion</b>${esc(e.name==='AbortError' ? 'The check timed out. Check network connectivity from the cluster and try again.' : e.message)}</div></div>`; btn.disabled = false; btn.textContent = 'Check again'; }
  };
  draw(null);
  return { run };
}

function resultHtml(r) {
  const [cls, ic, label] = STATUS[r.status] || STATUS.unknown;
  const cmds = r.commands || {}; const shells = Object.keys(cmds).filter(k => cmds[k]);
  const admin = [`OmniVec access request (${label})`, r.summary, r.role && `Role: ${r.role}`, r.scope && `Scope: ${r.scope}`, r.resource_id && `Resource: ${r.resource_id}`, r.principal_id && `Principal (object) ID: ${r.principal_id}`, r.reason && `Why: ${r.reason}`, ...(r.missing||[]), ...shells.map(s => `\n# ${s}\n${cmds[s]}`), r.verification].filter(Boolean).join('\n');
  return `<div class="stack s12">
   <div class="perm-status ${cls==='info'?'':cls}"><span class="sev ${cls}">${icon(ic,'sm')}</span><div class="grow"><div style="font-weight:650">${esc(label)}</div><div class="t2 small">${esc(r.summary||'')}</div>
     ${(r.checks||[]).length ? `<div style="margin-top:8px">${r.checks.map(c => `<div class="chk-row">${X.chkIcon(c.status==='passed'||c.status==='pass'?'pass':c.status==='failed'||c.status==='fail'?'fail':'warn')}<span>${esc(c.name)} <span class="muted">· ${esc(c.status)}${c.detail ? ' · '+esc(c.detail) : ''}</span></span></div>`).join('')}</div>` : ''}</div>
     <span class="muted small" style="flex:none">${X.ago(r.checked_at)}</span></div>
   ${(r.missing||[]).length ? `<div class="callout ${cls==='err'?'err':'warn'}">${icon('info','ic')}<div class="grow"><b>What's missing</b>${r.missing.map(m => `<div>${esc(m)}</div>`).join('')}</div></div>` : ''}
   ${r.role || r.scope || r.administrator ? `<dl class="kv">${r.role?`<dt>Required role</dt><dd><b>${esc(r.role)}</b></dd>`:''}${r.scope?`<dt>Scope</dt><dd class="mono small">${esc(r.scope)}</dd>`:''}${r.administrator?`<dt>Who can grant it</dt><dd>${esc(r.administrator)}</dd>`:''}${r.identity_source?`<dt>Identity evidence</dt><dd>${esc(r.identity_source)}</dd>`:''}</dl>` : ''}
   ${shells.length ? `<div><div class="seg" data-shells>${shells.map((s,i) => `<button class="${i?'':'on'}" data-sh="${s}">${s==='bash'?'Bash / Cloud Shell':'PowerShell'}</button>`).join('')}</div>
     ${shells.map((s,i) => `<div data-shbody="${s}" style="margin-top:8px;${i?'display:none':''}">${codebox(cmds[s])}</div>`).join('')}</div>` : ''}
   ${r.verification ? `<div class="muted small">${icon('clock','sm')} ${esc(r.verification)}</div>` : ''}
   ${(r.limitations||[]).length ? `<details class="small"><summary class="muted" style="cursor:pointer">What this check does not cover (${r.limitations.length})</summary><ul style="margin:6px 0 0;padding-left:18px">${r.limitations.map(l => `<li class="muted">${esc(l)}</li>`).join('')}</ul></details>` : ''}
   ${cls !== 'ok' ? `<div><button class="btn sm" data-copy="${esc(admin)}">${icon('copy','sm')}Copy request for administrator</button></div>` : ''}</div>`;
}
document.addEventListener('click', e => { const b = e.target.closest('[data-sh]'); if (!b) return; const wrap = b.closest('[data-shells]').parentElement;
  $$('[data-sh]', wrap).forEach(x => x.classList.toggle('on', x===b)); $$('[data-shbody]', wrap).forEach(x => x.style.display = x.dataset.shbody===b.dataset.sh ? '' : 'none'); });

X.perm = { permAssistant, guideHtml, identity, GUIDE, STATUS, resultHtml };
})();
