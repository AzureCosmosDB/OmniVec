/* Shared permission diagnosis for setup and existing resources. No grants run in the browser. */
function mountPermissionCheck(target, getRequest) {
    target.replaceChildren();
    target.classList.add('permission-card');
    const add = (tag, text, parent = target) => {
        const element = document.createElement(tag);
        element.textContent = text;
        parent.appendChild(element);
        return element;
    };
    add('h4', 'Understand and fix access');
    add('p', 'Check access without changing data or permissions. If access is denied, copy a scoped command for your administrator.');
    const request = getRequest();
    if (request.connector_type === 'sharepoint') {
        add('p', 'Same-tenant SharePoint uses the existing workload identity; no separate app registration is required. Ask the administrator for Microsoft Graph Sites.Selected application permission and read access to the selected site. Broad Files.Read.All or Sites.Read.All grants are not site-restricted. Exact Graph grant commands are not yet supported here.');
    } else if (request.connector_type === 'onelake-iceberg') {
        add('p', 'Ask the Fabric administrator for access to the selected workspace and lakehouse. Destinations additionally need the staging path and Spark Job Definition. Exact Fabric grant commands are not yet supported here.');
    } else if (['cosmosdb', 'cosmosdb-vector'].includes(request.connector_type)) {
        add('p', `Expected access: Cosmos DB Built-in Data ${request.kind === 'source' ? 'Reader' : 'Contributor'} at /dbs/${request.config.database || ''}/colls/${request.config.container || ''}. This is not evidence that a role is missing.`);
    }
    const details = add('details', '');
    add('summary', 'Resource and identity details (only if automatic discovery needs help)', details);
    const field = (label, name) => {
        const wrapper = add('label', label, details);
        wrapper.style.display = 'block';
        const input = add('input', '', wrapper);
        input.className = 'form-input';
        input.name = name;
        input.autocomplete = 'off';
        return input;
    };
    const resource = field('Full account resource ID (Azure Portal > JSON View)', 'permission_resource_id');
    const principal = field('Workload identity Object / principal ID (not client ID)', 'permission_principal_id');
    const button = add('button', 'Check access');
    button.type = 'button';
    button.className = 'btn btn-secondary';
    const output = add('div', '');
    output.setAttribute('aria-live', 'polite');
    let version = 0;
    const invalidate = () => {
        version++;
        output.replaceChildren();
        button.disabled = false;
        button.textContent = 'Check access';
    };
    resource.addEventListener('input', invalidate);
    principal.addEventListener('input', invalidate);
    const form = target.closest('form');
    if (form) form.addEventListener('input', invalidate, {signal: (() => {
        if (target.permissionController) target.permissionController.abort();
        target.permissionController = new AbortController();
        return target.permissionController.signal;
    })()});
    const copy = (label, value) => {
        const copyButton = add('button', label, output);
        copyButton.type = 'button';
        copyButton.className = 'btn btn-secondary';
        copyButton.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(value);
                showNotification('Copied. Review the scope before running as an authorized administrator.', 'success');
            } catch {
                showNotification('Clipboard is unavailable. Select and copy the displayed text manually.', 'error');
            }
        });
    };
    button.addEventListener('click', async () => {
        const current = ++version;
        button.disabled = true;
        output.replaceChildren();
        add('p', 'Checking access. No role assignments or data writes will be performed.', output);
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 30000);
        try {
            const response = await apiFetch('/api/permissions/check', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({...getRequest(), resource_id: resource.value.trim(), principal_id: principal.value.trim()}),
                signal: controller.signal,
            });
            if (!response.ok) throw new Error(`Access check failed (HTTP ${response.status}).`);
            const data = await response.json();
            if (typeof data.summary !== 'string' || !Array.isArray(data.missing) || !Array.isArray(data.limitations)) {
                throw new Error('Access check returned an invalid response.');
            }
            if (current !== version || !target.isConnected) return;
            output.replaceChildren();
            add('h4', data.summary, output);
            for (const check of data.checks || []) add('p', `${check.name}: ${check.status}`, output);
            for (const [key, label] of Object.entries({
                principal_id: 'Workload principal', identity_source: 'Identity evidence', resource_id: 'Account resource',
                scope: 'Access scope', role: 'Required role', reason: 'Why', administrator: 'Who can run this', checked_at: 'Checked at',
            })) {
                if (data[key]) {
                    const text = add('p', `${label}: ${data[key]}`, output);
                    text.style.overflowWrap = 'anywhere';
                }
            }
            for (const message of data.missing) add('p', message, output);
            if (data.missing.length) details.open = true;
            for (const shell of ['powershell', 'bash']) {
                const command = data.commands?.[shell];
                if (!command) continue;
                add('strong', shell === 'bash' ? 'Bash / Azure Cloud Shell' : 'PowerShell', output);
                const pre = add('pre', command, output);
                pre.style.whiteSpace = 'pre-wrap';
                pre.style.overflowWrap = 'anywhere';
                copy(`Copy ${shell === 'bash' ? 'Bash' : 'PowerShell'}`, command);
            }
            add('p', data.verification, output);
            for (const limitation of data.limitations) add('p', limitation, output);
            copy('Copy administrator instructions', output.innerText);
        } catch (error) {
            if (current !== version || !target.isConnected) return;
            output.replaceChildren();
            add('p', error.name === 'AbortError'
                ? 'The check timed out. No permission conclusion is available. Check service connectivity and try again.'
                : error.message, output);
        } finally {
            clearTimeout(timeout);
            if (current === version) {
                button.disabled = false;
                button.textContent = 'Check again';
            }
        }
    });
}

function renderPermissionPreflight(kind) {
    const form = document.getElementById(`${kind}-form`);
    mountPermissionCheck(document.getElementById(`${kind}-permission-preflight`), () => {
        const values = new FormData(form);
        const config = {};
        for (const key of ['endpoint', 'account_url', 'database', 'container', 'site_id', 'drive_id']) {
            if (values.get(key)) config[key] = values.get(key);
        }
        config.auth_type = values.get(kind === 'source' ? 'auth_type' : 'dest_auth_type') || 'managed-identity';
        return {kind, connector_type: values.get('type'), config};
    });
}

function showResourcePermissions(kind, id) {
    let dialog = document.getElementById('resource-permissions-dialog');
    if (!dialog) {
        dialog = document.createElement('dialog');
        dialog.id = 'resource-permissions-dialog';
        dialog.style.cssText = 'width:min(760px,90vw);max-height:85vh;overflow:auto;background:var(--bg-primary);color:var(--text-primary);border:1px solid var(--border-primary);border-radius:12px';
        dialog.setAttribute('aria-label', 'Resource access and permissions');
        document.body.appendChild(dialog);
    }
    dialog.replaceChildren();
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn btn-secondary';
    close.textContent = 'Close';
    close.onclick = () => dialog.close();
    dialog.appendChild(close);
    const body = document.createElement('div');
    dialog.appendChild(body);
    mountPermissionCheck(body, () => ({kind, resource_ref: id}));
    dialog.showModal();
}
