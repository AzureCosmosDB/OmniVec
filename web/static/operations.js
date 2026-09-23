/* Operational views use observed values only; missing measurements stay unknown. */
const OmniConsole = (() => {
    let snapshot = null;
    let diagnostic = null;
    let pending = null;
    let requestVersion = 0;

    function number(value) {
        return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
    }

    function total(values) {
        return values.length && values.every(value => number(value) !== null)
            ? values.reduce((sum, value) => sum + value, 0) : null;
    }

    function display(value, suffix = '') {
        return number(value) === null ? 'Not observed' : value.toLocaleString(undefined, { maximumFractionDigits: 1 }) + suffix;
    }

    function timestamp(value) {
        if (typeof value !== 'string' || !value.trim()) return null;
        const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : value + 'Z';
        const parsed = Date.parse(normalized);
        return Number.isFinite(parsed) ? parsed : null;
    }

    function healthSignal(health, now = Date.now()) {
        const checked = timestamp(health?.checked_at);
        if (checked === null || now - checked < 0 || now - checked > 600000) {
            return { label: 'Health evidence unavailable or stale', tone: 'warning' };
        }
        if (health.overall === 'healthy') return { label: 'Dependencies ready', tone: 'good' };
        if (['unhealthy', 'error'].includes(health.overall)) return { label: 'Dependencies need attention', tone: 'bad' };
        return { label: 'Dependency evidence incomplete', tone: 'warning' };
    }

    function diagnosticMetrics(data) {
        const evidence = data?.evidence || {};
        const queueRows = Object.values(evidence.queues?.queues || {});
        const queuesReadable = queueRows.length && queueRows.every(row => row.ok === true);
        const workloads = Array.isArray(evidence.deployments) ? evidence.deployments : [];
        const readable = workloads.length && workloads.every(row =>
            ['desired', 'ready', 'generation', 'observed_generation'].every(key => number(row[key]) !== null));
        return {
            activeMessages: queuesReadable ? total(queueRows.map(row => row.value?.active_message_count)) : null,
            deadLetters: queuesReadable ? total(queueRows.map(row => row.value?.dead_letter_message_count)) : null,
            workloads: readable ? workloads.filter(row => row.desired > 0 && row.ready >= row.desired &&
                row.observed_generation === row.generation).length : null,
            workloadCount: workloads.length || null,
            destinationCount: total((data?.pipelines || []).map(row => row.progress?.embedded_after)),
        };
    }

    function summarizeBuckets(buckets, granularity) {
        const processed = total(buckets.map(bucket => bucket.processed));
        const failed = total(buckets.map(bucket => bucket.failed));
        const measured = buckets.filter(bucket => number(bucket.processed) > 0);
        const completeLatency = measured.length && measured.every(bucket => number(bucket.avg_latency_ms) !== null);
        const averageLatency = completeLatency
            ? measured.reduce((sum, bucket) => sum + bucket.avg_latency_ms * bucket.processed, 0) /
                measured.reduce((sum, bucket) => sum + bucket.processed, 0) : null;
        const first = timestamp(buckets[0]?.t);
        const last = timestamp(buckets[buckets.length - 1]?.t);
        const interval = { minute: 60, hour: 3600, day: 86400 }[granularity];
        const throughput = processed !== null && first !== null && last !== null && last >= first && interval
            ? processed / ((last - first) / 1000 + interval) : null;
        return { processed, failed, averageLatency, throughput };
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function cell(label, value, note) {
        const node = element('div', 'pulse-cell');
        node.append(element('small', '', label), element('strong', '', value));
        if (note) node.append(element('small', '', note));
        return node;
    }

    function badge(label, tone) {
        return element('span', 'signal-badge ' + (tone || ''), label);
    }

    function updateScopes() {
        const select = document.getElementById('diagnostic-scope');
        if (!select || !snapshot) return;
        const selected = select.value;
        const options = [new Option('System overview (up to 6 pipelines)', '')];
        for (const pipeline of snapshot.pipelines) {
            options.push(new Option((pipeline.name || pipeline.id) + ' / ' + (pipeline.status || 'unknown'), pipeline.id));
        }
        select.replaceChildren(...options);
        select.value = options.some(option => option.value === selected) ? selected : '';
        if (selected && select.value !== selected) changeScope();
    }

    function renderPulse() {
        if (!snapshot) return;
        const metrics = snapshot.metrics || {};
        const health = healthSignal(snapshot.health);
        const target = document.getElementById('workspace-pulse');
        if (target) {
            const summary = element('div', 'pulse-summary');
            summary.append(badge(health.label, health.tone), element('span', 'muted', 'Cached dependency checks'));
            const grid = element('div', 'pulse-grid');
            grid.append(
                cell('Pipelines active', snapshot.pipelines.filter(p => p.status === 'active').length + ' / ' + snapshot.pipelines.length),
                cell('Pending jobs', display(total(snapshot.pipelines.map(p => p.stats?.jobs?.pending)))),
                cell('Embedding latency p95', display(metrics.latency?.embedding?.p95, ' ms')),
                cell('Tokens observed', display(metrics.tokens?.total)),
            );
            target.replaceChildren(summary, grid, element('p', 'pulse-note',
                'Active is configuration, not processing proof. Diagnostics explain missing evidence and next steps.'));
        }
        const runtime = document.getElementById('runtime-metrics');
        if (runtime) {
            runtime.replaceChildren(
                cell('Live throughput', display(metrics.throughput_docs_per_sec, ' /s')),
                cell('Embedding latency p50', display(metrics.latency?.embedding?.p50, ' ms')),
                cell('Embedding latency p95', display(metrics.latency?.embedding?.p95, ' ms')),
                cell('Embedding latency p99', display(metrics.latency?.embedding?.p99, ' ms')),
                cell('Jobs pending', display(total(snapshot.pipelines.map(p => p.stats?.jobs?.pending)))),
                cell('Jobs processing', display(total(snapshot.pipelines.map(p => p.stats?.jobs?.processing)))),
                cell('Runtime failed events', display(metrics.events_failed)),
                cell('Runtime skipped events', display(metrics.skipped?.total)),
                element('p', 'pulse-note', snapshot.metrics
                    ? 'Runtime snapshot received ' + new Date(snapshot.receivedAt).toLocaleTimeString() + '. Missing measurements are not zero; runtime counters may reset after service restarts.'
                    : 'Runtime metrics are unavailable. Job counters come from pipeline observations; no runtime values have been substituted.'),
            );
        }
    }

    function updateData(data) {
        snapshot = { ...data, receivedAt: Date.now() };
        for (const key of ['sources', 'pipelines', 'destinations']) {
            const target = document.getElementById('flow-' + key);
            if (target) target.textContent = data[key].length.toLocaleString();
        }
        const states = document.getElementById('stat-pipeline-states');
        if (states) states.textContent = data.pipelines.filter(p => p.status === 'active').length + ' active / ' + data.pipelines.length + ' configured';
        const freshness = document.getElementById('workspace-freshness');
        if (freshness) {
            freshness.className = 'workspace-freshness';
            freshness.textContent = 'Workspace refreshed ' + new Date(snapshot.receivedAt).toLocaleTimeString() +
                ' / refreshes every 30 seconds' + (data.metrics ? '' : ' / runtime metrics unavailable');
        }
        updateScopes();
        renderPulse();
    }

    function reportDataError() {
        const freshness = document.getElementById('workspace-freshness');
        if (freshness) {
            freshness.className = 'workspace-freshness error';
            freshness.textContent = snapshot
                ? 'Refresh failed. Showing the last workspace snapshot from ' + new Date(snapshot.receivedAt).toLocaleTimeString() + '.'
                : 'Workspace data could not be loaded. Check API connectivity or sign in again.';
        }
    }

    function details(title, values) {
        const node = element('details', 'diagnostic-details');
        node.append(element('summary', '', title + ' (' + values.length + ')'));
        const list = element('ul');
        values.forEach(value => list.append(element('li', '', value)));
        node.append(list);
        return node;
    }

    function renderDiagnostics(data) {
        const target = document.getElementById('diagnostic-results');
        const values = diagnosticMetrics(data);
        const states = {
            HEALTHY: ['Observed processing is healthy', 'good'],
            READY_IDLE: ['Ready, but processing is not verified', ''],
            BLOCKED: ['A concrete blocker needs attention', 'bad'],
            UNHEALTHY: ['Failures need investigation', 'bad'],
            UNKNOWN: ['More evidence is needed', 'warning'],
        };
        const state = states[data.status] || states.UNKNOWN;
        const banner = element('div', 'diagnostic-banner');
        const explanation = element('div');
        const observed = timestamp(data.observed_at);
        explanation.append(
            badge(data.status, state[1]), element('h3', '', state[0]),
            element('p', '', 'Scope: ' + data.scope + ' / ' +
                (observed === null ? 'Observation time unavailable' : 'Observed ' + new Date(observed).toLocaleString())),
            element('p', '', data.processing_verified === true
                ? 'Destination progress was observed. This does not prove every source document has been indexed.'
                : 'A successful request, ready workload, or empty queue is not proof of processing recovery.'),
        );
        const investigateButton = element('button', 'btn btn-secondary', 'Investigate with agent');
        investigateButton.type = 'button';
        investigateButton.addEventListener('click', () => investigate(data.scope === 'system' ? '' : data.scope));
        banner.append(explanation, investigateButton);
        const grid = element('div', 'diagnostic-grid');
        grid.append(
            cell('Observed workloads ready', values.workloads === null ? 'Not observed' : values.workloads + ' / ' + values.workloadCount),
            cell('Shared active messages', display(values.activeMessages), 'Not pipeline-specific'),
            cell('Shared dead-letter messages', display(values.deadLetters), 'Inspect before any replay'),
            cell('Destination count in scope', display(values.destinationCount), 'Not a throughput measurement'),
        );
        target.replaceChildren(banner, grid);
        const plans = Array.isArray(data.repair_plan) ? data.repair_plan : (data.findings || []).map(finding => ({
            code: finding.code, instructions: finding.next_action,
            execution: finding.approved_tool_candidate ? 'approval_required' : 'operator_investigation',
        }));
        const plan = element('div', 'card');
        const heading = element('div', 'card-header');
        heading.append(element('h3', 'card-title', 'Prioritized next steps'), badge('Approval gates stay on'));
        const body = element('div', 'card-body diagnostic-plan');
        if (!plans.length) body.append(element('p', 'muted', 'No repair is supported by this snapshot. Verify an authorized workload before claiming recovery.'));
        plans.forEach((step, index) => {
            const item = element('div', 'diagnostic-step');
            item.append(
                element('h4', '', (index + 1) + '. ' + String(step.code || 'Investigate').replaceAll('_', ' ')),
                badge(step.execution === 'approval_required' ? 'Approval required' : 'Investigate / observe'),
                element('p', '', step.instructions || 'Inspect the available evidence before taking action.'),
            );
            if (step.precondition) item.append(element('p', '', 'Before acting: ' + step.precondition));
            if (step.verification) item.append(element('p', '', 'Verify: ' + step.verification));
            body.append(item);
        });
        plan.append(heading, body);
        target.append(plan);
        const findings = (data.findings || []).map(finding => (finding.code || 'Finding') + ': ' + (finding.evidence || 'Evidence unavailable'));
        if (findings.length) target.append(details('Supporting evidence', findings));
        if (data.unknown?.length) {
            const gaps = details('Missing or uncertain evidence', data.unknown);
            gaps.open = true;
            target.append(gaps);
        }
        if (data.limitations?.length) target.append(details('Coverage limitations', data.limitations));
        const workloads = data.evidence?.deployments || [];
        if (workloads.length) target.append(details('Observed workload readiness', workloads.map(row =>
            row.name + ': ' + display(row.ready) + ' ready / ' + display(row.desired) + ' desired; generation ' +
            display(row.observed_generation) + ' observed / ' + display(row.generation) + ' requested')));
    }

    async function runDiagnostics() {
        if (pending) return;
        const select = document.getElementById('diagnostic-scope');
        const button = document.getElementById('diagnostic-run');
        const target = document.getElementById('diagnostic-results');
        const scope = select.value;
        const version = ++requestVersion;
        const controller = new AbortController();
        pending = controller;
        let timedOut = false;
        const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 55000);
        button.disabled = select.disabled = true;
        button.textContent = 'Collecting evidence...';
        target.setAttribute('aria-busy', 'true');
        target.replaceChildren(element('div', 'diagnostic-empty', 'Observing dependencies, workloads, queues, and pipeline counters. No changes are being made.'));
        try {
            const response = await apiFetch('/api/agent/diagnostics/' + (scope ? 'pipeline' : 'system'), {
                method: scope ? 'POST' : 'GET',
                ...(scope ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pipeline_id: scope }) } : {}),
                signal: controller.signal,
            });
            if (!response.ok) throw new Error('Diagnostics unavailable (HTTP ' + response.status + '). Verify the API and agent deployment, then retry.');
            const data = await response.json();
            if (data.scope !== (scope || 'system') || !Array.isArray(data.pipelines) ||
                !Array.isArray(data.findings) || !Array.isArray(data.unknown)) {
                throw new Error('Diagnostic evidence was incomplete or returned the wrong scope.');
            }
            if (version !== requestVersion) return;
            diagnostic = data;
            renderDiagnostics(data);
        } catch (error) {
            if (version !== requestVersion) return;
            diagnostic = null;
            const message = timedOut ? 'The diagnostic snapshot timed out. Retry a single pipeline. No health conclusion can be made.' : error.message;
            target.replaceChildren(element('div', 'diagnostic-empty diagnostic-error', message));
        } finally {
            clearTimeout(timeout);
            if (version === requestVersion) {
                pending = null;
                button.disabled = select.disabled = false;
                button.textContent = 'Run diagnostics';
                target.setAttribute('aria-busy', 'false');
            }
        }
    }

    function changeScope() {
        diagnostic = null;
        document.getElementById('diagnostic-results').replaceChildren(
            element('div', 'diagnostic-empty', 'Scope changed. Run a new diagnostic snapshot; previous evidence does not apply to this selection.'));
    }

    function openDiagnostics() {
        showSection('health');
        updateScopes();
        runDiagnostics();
    }

    function prefillAgent(message) {
        const input = document.getElementById('agent-input');
        input.value = message;
        input.focus();
    }

    function renderApproval({ callId, tool, args, danger, summary }) {
        const transcript = document.getElementById('agent-transcript');
        const card = element('div', 'agent-approval');
        card.setAttribute('data-call-id', callId);
        const heading = element('div', 'pulse-summary');
        heading.append(element('strong', '', 'Approval required'), badge(
            ['low', 'medium', 'high'].includes(danger) ? danger + ' risk' : 'Review scope',
            danger === 'high' ? 'bad' : 'warning'));
        const comment = element('textarea', 'form-input');
        comment.id = 'approve-comment-' + callId;
        comment.placeholder = 'Optional comment about this decision';
        comment.setAttribute('aria-label', 'Approval comment');
        comment.rows = 2;
        const buttons = element('div', 'hero-actions');
        for (const [decision, label] of [['deny', 'Deny'], ['approve', 'Approve and run once']]) {
            const button = element('button', decision === 'approve' ? 'btn btn-primary' : 'btn btn-secondary', label);
            button.type = 'button';
            button.addEventListener('click', async () => {
                buttons.querySelectorAll('button').forEach(control => { control.disabled = true; });
                try {
                    await agentDecide(callId, tool, decision);
                } catch (error) {
                    showNotification('Approval response interrupted. Inspect the session before retrying.', 'error');
                }
            });
            buttons.append(button);
        }
        card.append(heading, element('p', '', tool + ': ' + (summary || 'Review the exact action scope.')),
            element('pre', '', JSON.stringify(args || {}, null, 2)), comment, buttons,
            element('p', 'pulse-note', 'Approval permits one scoped action. The runtime must verify recovery afterward; execution alone is not success.'));
        transcript.append(card);
        transcript.scrollTop = transcript.scrollHeight;
        return card;
    }

    async function investigate(pipelineId = '') {
        showSection('agent');
        await agentInit();
        prefillAgent(pipelineId
            ? 'Diagnose pipeline ' + pipelineId + '. Explain blockers, missing evidence, and the smallest safe repair. Do not execute mutations without approval.'
            : 'Diagnose the system. Explain observed blockers and missing evidence. Propose scoped next steps; do not execute mutations without approval.');
    }

    function clear() {
        requestVersion++;
        if (pending) pending.abort();
        pending = diagnostic = snapshot = null;
        const button = document.getElementById('diagnostic-run');
        const select = document.getElementById('diagnostic-scope');
        if (button) { button.disabled = false; button.textContent = 'Run diagnostics'; }
        if (select) { select.disabled = false; select.replaceChildren(new Option('System overview (up to 6 pipelines)', '')); }
        const result = document.getElementById('diagnostic-results');
        if (result) { result.setAttribute('aria-busy', 'false'); result.replaceChildren(element('div', 'diagnostic-empty', 'Run diagnostics to collect fresh evidence.')); }
        for (const id of ['workspace-pulse', 'runtime-metrics']) {
            const target = document.getElementById(id);
            if (target) target.replaceChildren(element('p', 'muted', 'Waiting for fresh observations.'));
        }
        for (const key of ['sources', 'pipelines', 'destinations']) {
            const target = document.getElementById('flow-' + key);
            if (target) target.textContent = '--';
        }
    }

    function init() {
        document.querySelectorAll('.nav-item').forEach(item => {
            item.setAttribute('role', 'button');
            item.tabIndex = 0;
            if (item.classList.contains('active')) item.setAttribute('aria-current', 'page');
            item.addEventListener('keydown', event => {
                if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); item.click(); }
            });
        });
    }

    return { number, total, display, timestamp, healthSignal, diagnosticMetrics, summarizeBuckets, updateData, reportDataError,
        renderPulse, renderDiagnostics, runDiagnostics, changeScope, openDiagnostics, prefillAgent, renderApproval, investigate, clear, init };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = OmniConsole;
if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', OmniConsole.init);
