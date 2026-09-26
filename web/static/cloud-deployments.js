/* Approved deployment jobs persist on the server; navigation never starts work. */
const CloudDeployments = (() => {
    let section = '';
    let generation = 0;
    let timer;
    let jobs = [];
    let capabilities = {};
    let selectedId = '';
    const jobSection = job => ['mcp', 'cosmos_container'].includes(job.kind) ? 'mcp' : 'foundry';
    const el = id => document.getElementById(id);
    const value = name => el(`${section}-${name}`).value.trim();

    async function request(path, body) {
        const controller = new AbortController();
        const deadline = setTimeout(() => controller.abort(), 30000);
        try {
            const response = await fetch('/api/cloud-deployments' + path, {
                method: body === undefined ? 'GET' : 'POST',
                headers: { ...authHeaders(), 'Content-Type': 'application/json' },
                ...(body === undefined ? {} : { body: JSON.stringify(body) }),
                signal: controller.signal,
            });
            const data = await response.json();
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (HTTP ${response.status}). Check the form values.`);
            return data;
        } catch (error) {
            if (error.name === 'AbortError') throw new Error('Request timed out. Refresh saved jobs before retrying; the server may have accepted the request.');
            throw error;
        } finally {
            clearTimeout(deadline);
        }
    }

    function field(kind, name, label, type = 'input', hint = '') {
        const id = `${kind}-${name}`;
        const control = type === 'select'
            ? `<select id="${id}" required></select>`
            : `<input id="${id}" required autocomplete="off">`;
        return `<label class="cloud-field" for="${id}">${label}${control}<small>${hint}</small></label>`;
    }

    function mount(kind) {
        const root = el(`${kind}-workspace`);
        if (root.dataset.mounted) return;
        root.dataset.mounted = 'true';
        const fields = kind === 'mcp' ? [
            field(kind, 'destination', 'Cosmos vector destination', 'select'),
            field(kind, 'model', 'Matching Azure OpenAI embedding model', 'select', 'Use the same deployment that produced these vectors.'),
            field(kind, 'subscription', 'Target Azure subscription ID'),
            field(kind, 'group', 'Existing target resource group'),
            field(kind, 'location', 'Azure region', 'input', 'Choose a region supporting Flex Consumption, for example eastus2.'),
            field(kind, 'cosmos', 'Cosmos account resource ID'),
            field(kind, 'openai', 'Embedding account resource ID'),
            field(kind, 'vector', 'Vector field'),
            field(kind, 'fields', 'Returned document fields', 'input', 'Include the stored text field, e.g. text for chunks; only top-level fields are supported.'),
        ].join('') : [
            field(kind, 'mcp', 'Deployed MCP server', 'select'),
            field(kind, 'project', 'Existing Foundry project resource ID', 'input', 'Full Microsoft.CognitiveServices/accounts/.../projects/... resource ID.'),
            field(kind, 'chat', 'Chat model deployment name', 'input', 'An existing chat deployment in this Foundry account. Creation does not run inference.'),
        ].join('');
        root.innerHTML = `
            <p>${kind === 'mcp'
                ? 'Deploy the same read-only Cosmos MCP code to a destination-specific Azure Function. Review actual resource IDs and permissions before approval.'
                : 'Create a Foundry prompt agent connected to a deployed MCP server. Credentials move directly into a secure project connection, never into the browser.'}</p>
            <p id="${kind}-capability" role="status">Loading deployment settings...</p>
            <p id="${kind}-error" role="alert" class="cloud-error"></p>
            <form id="${kind}-form" class="cloud-form">${fields}
                <button class="btn btn-primary" type="submit">Prepare plan (no provisioning)</button>
            </form>
            ${kind === 'mcp' ? `<details><summary>Need a new isolated Cosmos vector container?</summary>
                <p>Use an existing database. This creates a new container only after separate approval; then register it in Destinations.</p>
                <form id="mcp-container-form" class="cloud-form">
                    ${field(kind, 'container-account', 'Existing Cosmos account resource ID')}
                    ${field(kind, 'container-database', 'Existing database')}
                    ${field(kind, 'container-name', 'New isolated container name')}
                    ${field(kind, 'container-model', 'Embedding model for vector dimensions', 'select')}
                    <button class="btn btn-primary" type="submit">Prepare container plan</button>
                </form>
            </details>` : `<details><summary>Test a deployed Foundry agent</summary>
                <p>Separately approve one billable inference/retrieval request. Its answer and source references are saved in the job.</p>
                <form id="foundry-test-form" class="cloud-form">
                    ${field(kind, 'test-agent', 'Created Foundry agent', 'select')}
                    ${field(kind, 'test-question', 'Question')}
                    <button class="btn btn-primary" type="submit">Prepare agent test plan</button>
                </form>
            </details>`}
            <h3>Saved deployment jobs (latest 100)</h3>
            <button class="btn btn-secondary" id="${kind}-refresh">Refresh status</button>
            <div id="${kind}-jobs" class="cloud-jobs" aria-live="polite"></div>
            <div id="${kind}-review" class="cloud-review" hidden>
                <h3>Review deployment</h3>
                <p id="${kind}-notice"></p>
                <h4>Resources</h4><ul id="${kind}-resources"></ul>
                <h4>New Function identity permissions</h4><ul id="${kind}-permissions"></ul>
                <details><summary>Configuration, results and progress history</summary><pre id="${kind}-plan" tabindex="0"></pre></details>
                <details><summary>API identity permissions and administrator commands</summary>
                    <p>These commands grant deployment privileges, not just data access. An Azure administrator must review their scope. OmniVec never executes them automatically.</p>
                    <button class="btn btn-secondary" id="${kind}-copy-ps">Copy PowerShell commands</button>
                    <button class="btn btn-secondary" id="${kind}-copy-sh">Copy Bash commands</button>
                    <pre id="${kind}-commands" tabindex="0"></pre>
                </details>
                <label><input id="${kind}-consent" type="checkbox"> I approve the listed resources, costs and identity permissions.</label>
                <button class="btn btn-primary" id="${kind}-approve" disabled>Approve deployment</button>
                <p id="${kind}-approval-note"></p>
            </div>`;
        el(`${kind}-form`).addEventListener('submit', prepare);
        el(kind === 'mcp' ? 'mcp-container-form' : 'foundry-test-form').addEventListener('submit', prepareExtra);
        el(`${kind}-refresh`).addEventListener('click', () => refresh().catch(showError));
        el(`${kind}-approve`).addEventListener('click', approve);
        el(`${kind}-consent`).addEventListener('change', updateApproval);
        for (const [suffix, shell] of [['ps', 'powershell'], ['sh', 'bash']]) {
            el(`${kind}-copy-${suffix}`).addEventListener('click', async () => {
                const job = jobs.find(j => j.id === selectedId);
                const commands = (job?.plan.deployer_permissions || []).map(p => p.commands?.[shell]).filter(Boolean).join('\n');
                try {
                    if (!commands) throw new Error('The workload principal ID is not configured; commands cannot be generated without an actual identity.');
                    await navigator.clipboard.writeText(commands);
                    showNotification('Administrator commands copied.', 'success');
                } catch (error) { showError(error); }
            });
        }
        if (kind === 'mcp') {
            el('mcp-vector').value = 'embedding';
            el('mcp-fields').value = 'id,title,text,source_ref';
        }
    }

    function showError(error) {
        if (section) el(`${section}-error`).textContent = error.message || 'Deployment request failed.';
    }

    function options(select, items, label, previous = select.value) {
        select.replaceChildren(new Option('Select...', ''));
        items.forEach(item => select.add(new Option(label(item), item.id)));
        if (items.some(item => item.id === previous)) select.value = previous;
    }

    async function enter(kind) {
        section = kind;
        const current = ++generation;
        mount(kind);
        try {
            await refresh();
            if (kind === 'mcp') {
                const responses = await Promise.all(['/api/destinations', '/api/models'].map(url =>
                    fetch(url, { headers: authHeaders(), signal: AbortSignal.timeout(30000) })));
                if (responses.some(r => !r.ok)) throw new Error('Unable to load destinations or models. Check access and refresh the section.');
                const [destinations, models] = await Promise.all(responses.map(r => r.json()));
                if (current !== generation) return;
                options(el('mcp-destination'), (destinations.destinations || []).filter(d => d.type === 'cosmosdb-vector' && d.enabled !== false), d => d.name || d.id);
                options(el('mcp-model'), (models.models || []).filter(m => m.type === 'azure-openai' && (m.model_category || 'embedding') === 'embedding' && m.enabled !== false), m => m.name || m.id);
                options(el('mcp-container-model'), (models.models || []).filter(m => (m.model_category || 'embedding') === 'embedding' && m.enabled !== false), m => m.name || m.id);
            }
        } catch (error) {
            if (current === generation) showError(error);
        } finally {
            if (current === generation) schedule();
        }
    }

    function schedule() {
        clearTimeout(timer);
        const current = generation;
        timer = setTimeout(async () => {
            try { await refresh(); } catch (error) { if (current === generation) showError(error); }
            if (current === generation) schedule();
        }, 5000);
    }

    async function refresh() {
        const current = generation;
        const result = await request('');
        if (current !== generation || !section) return;
        if (!Array.isArray(result.jobs) || !result.capabilities) throw new Error('Invalid deployment status response.');
        jobs = result.jobs;
        capabilities = result.capabilities;
        el(`${section}-capability`).textContent = capabilities.enabled
            ? 'Deployment is enabled for administrator-approved scopes. Preparing a plan creates no Azure resources.'
            : 'Live provisioning is disabled. An administrator must explicitly enable it and allow the target scopes; preparing a plan is still safe.';
        const list = el(`${section}-jobs`);
        list.replaceChildren();
        const relevant = jobs.filter(j => jobSection(j) === section);
        if (!relevant.length) list.textContent = 'No saved deployment jobs.';
        relevant.forEach(job => {
            const row = document.createElement('div');
            row.className = 'cloud-job';
            const text = document.createElement('span');
            text.textContent = `${job.id} — ${job.status} — ${job.stage} (attempt ${job.attempts}/3)`;
            const button = document.createElement('button');
            button.className = 'btn btn-secondary';
            button.textContent = 'Review';
            button.addEventListener('click', () => {
                selectedId = job.id;
                el(`${section}-consent`).checked = false;
                review();
            });
            row.append(text, button);
            if (job.error) {
                const error = document.createElement('p');
                error.className = 'cloud-error';
                error.textContent = `${job.error.code}: ${job.error.message}${job.error.resource_id ? '\n' + job.error.resource_id : ''}`;
                row.append(error);
            }
            if (job.result?.verification) {
                const verified = document.createElement('p');
                verified.textContent = job.result.verification;
                row.append(verified);
            }
            if (job.result?.answer) {
                const answer = document.createElement('pre');
                answer.className = 'cloud-answer';
                answer.textContent = job.result.answer;
                const references = document.createElement('pre');
                references.className = 'cloud-answer';
                references.textContent = 'Retrieved source references:\n' + JSON.stringify(job.result.source_references, null, 2);
                row.append(answer, references);
            }
            list.append(row);
        });
        if (section === 'foundry') {
            options(el('foundry-mcp'), jobs.filter(j => j.kind === 'mcp' && j.status === 'succeeded'), j => j.result.server_url);
            options(el('foundry-test-agent'), jobs.filter(j => j.kind === 'foundry' && j.status === 'succeeded'), j => j.result.agent_name);
        }
        review();
    }

    function review() {
        const job = jobs.find(j => j.id === selectedId && jobSection(j) === section);
        el(`${section}-review`).hidden = !job;
        if (!job) return;
        el(`${section}-notice`).textContent = job.plan.notice;
        const resourceList = el(`${section}-resources`);
        resourceList.replaceChildren();
        for (const resource of job.plan.resources) {
            const item = document.createElement('li');
            if (resource.startsWith('/subscriptions/')) {
                const link = document.createElement('a');
                link.href = 'https://portal.azure.com/#resource' + resource;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.textContent = resource;
                item.append(link);
            } else item.textContent = resource;
            resourceList.append(item);
        }
        const permissions = el(`${section}-permissions`);
        permissions.replaceChildren();
        if (!job.plan.permissions.length) permissions.textContent = 'No new data roles. Uses the existing MCP Function identity.';
        for (const permission of job.plan.permissions) {
            const item = document.createElement('li');
            item.textContent = `${permission.role}: ${permission.scope}`;
            permissions.append(item);
        }
        el(`${section}-plan`).textContent = JSON.stringify({ resources: job.plan.resources, service_permissions: job.plan.permissions, settings: job.plan, result: job.result, history: job.history }, null, 2);
        const commands = (job.plan.deployer_permissions || []).map(p => p.commands?.powershell || `${p.role} at ${p.scope}: workload principal ID is not configured.`).join('\n\n');
        el(`${section}-commands`).textContent = commands;
        updateApproval();
    }

    function updateApproval() {
        const job = jobs.find(j => j.id === selectedId);
        if (!job || !section) return;
        const retry = ['failed', 'interrupted'].includes(job.status);
        const allowed = capabilities.enabled && ['awaiting_approval', 'failed', 'interrupted'].includes(job.status) && job.attempts < 3 && !(job.kind === 'verification' && job.inference_started);
        el(`${section}-approve`).disabled = !allowed || !el(`${section}-consent`).checked;
        el(`${section}-approve`).textContent = retry ? 'Approve retry of the same deployment' : 'Approve deployment';
        el(`${section}-approval-note`).textContent = retry
            ? 'Partial resources are retained. Inspect them first. Retry reuses the same resource names and stops after three approved attempts. Ambiguous agent creation is reconciled, not blindly replayed.'
            : 'This is the only action that queues Azure changes. Closing the browser does not cancel an approved job.';
    }

    async function prepare(event) {
        event.preventDefault();
        const kind = section, current = generation;
        const button = event.target.querySelector('button[type=submit]');
        button.disabled = true;
        el(`${kind}-error`).textContent = '';
        try {
            const body = kind === 'mcp' ? {
                kind, resource_group_id: `/subscriptions/${value('subscription')}/resourceGroups/${value('group')}`,
                location: value('location'), destination_id: value('destination'), embedding_model_id: value('model'),
                cosmos_account_id: value('cosmos'), embedding_account_id: value('openai'),
                vector_field: value('vector'), fields: value('fields'),
            } : {
                kind, resource_group_id: value('project').split('/providers/')[0],
                location: 'eastus', project_resource_id: value('project'),
                mcp_deployment_id: value('mcp'), chat_deployment: value('chat'),
            };
            const job = await request('/plan', body);
            if (current !== generation) return;
            selectedId = job.id;
            el(`${kind}-consent`).checked = false;
            await refresh();
        } catch (error) { if (current === generation) showError(error); }
        finally { button.disabled = false; }
    }

    async function approve() {
        const current = generation;
        const job = jobs.find(j => j.id === selectedId);
        if (!job || !el(`${section}-consent`).checked) return;
        el(`${section}-approve`).disabled = true;
        el(`${section}-consent`).checked = false;
        try {
            await request(`/${encodeURIComponent(job.id)}/approve`, { plan_hash: job.plan_hash, approve_cost_and_permissions: true });
            if (current === generation) await refresh();
        } catch (error) { if (current === generation) showError(error); }
        finally { if (current === generation) updateApproval(); }
    }

    async function prepareExtra(event) {
        event.preventDefault();
        const current = generation;
        const kind = section;
        const button = event.target.querySelector('button[type=submit]');
        button.disabled = true;
        try {
            let body;
            if (kind === 'mcp') {
                const account = value('container-account');
                body = {
                    kind: 'cosmos_container', resource_group_id: account.split('/providers/')[0],
                    location: 'eastus', cosmos_account_id: account, database: value('container-database'),
                    container: value('container-name'), embedding_model_id: value('container-model'),
                };
            } else {
                const parent = jobs.find(j => j.id === value('test-agent'));
                if (!parent) throw new Error('Select a successfully created Foundry agent.');
                body = {
                    kind: 'verification', resource_group_id: parent.plan.resource_group_id,
                    location: parent.plan.location, foundry_deployment_id: parent.id, question: value('test-question'),
                };
            }
            const job = await request('/plan', body);
            if (current !== generation) return;
            selectedId = job.id;
            el(`${kind}-consent`).checked = false;
            await refresh();
        } catch (error) { if (current === generation) showError(error); }
        finally { button.disabled = false; }
    }

    function leave() {
        generation++;
        section = '';
        clearTimeout(timer);
    }
    return { enter, leave };
})();
