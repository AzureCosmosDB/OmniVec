# OmniVec Metrics — Azure Monitor + Application Insights Plan

## Goal

Add Azure-native observability to OmniVec using Application Insights + Azure Monitor.
All metrics should be viewable in the OmniVec web UI dashboard AND in Azure Portal.

## What gets added

### 1. Infrastructure (Bicep)

Add to `infra/modules/`:
- **Log Analytics Workspace** — stores all logs and metrics
- **Application Insights** — connected to the workspace

Output: `APPINSIGHTS_CONNECTION_STRING` passed to Helm

Cost: ~$2-5/month for small deployments

### 2. API (Python) — Application Insights SDK

Add `opencensus-ext-azure` or `azure-monitor-opentelemetry` to `api/requirements.txt`.

Auto-instrumented:
- HTTP request latency (every API call)
- HTTP error rates (4xx, 5xx)
- Dependency tracking (CosmosDB, DocGrok HTTP calls)
- Exception telemetry

Custom metrics to emit:
| Metric | When | What |
|--------|------|------|
| `omnivec.documents.embedded` | After successful embedding | Counter per pipeline |
| `omnivec.embedding.latency_ms` | After each embedding call | Histogram |
| `omnivec.pipeline.jobs_created` | When controller creates jobs | Counter per pipeline |
| `omnivec.pipeline.jobs_failed` | When job fails | Counter per pipeline |
| `omnivec.pipeline.completion_pct` | On stats refresh | Gauge per pipeline |
| `omnivec.search.latency_ms` | After search query | Histogram |
| `omnivec.search.results_count` | After search query | Histogram |
| `omnivec.changefeed.events_processed` | Reported by .NET CFP | Counter |
| `omnivec.changefeed.lag` | Reported by .NET CFP | Gauge |

Implementation: ~50 lines in `api/api.py` + `api/telemetry.py`

### 3. .NET Services (Optional, Phase 2)

Add `Microsoft.ApplicationInsights.WorkerService` NuGet to:
- `connectors/ingestion/dotnet/` (changefeed processor)
- `connectors/worker/dotnet/` (embedding worker)

This gives: embedding latency from the worker perspective, Service Bus message processing time, PostgreSQL connection health.

For Phase 1, the .NET services report metrics via `/api/metrics/changefeed` which the Python API can forward to App Insights.

### 4. Helm Charts

Add to `helm/omnivec/values.yaml`:
```yaml
azure:
  appInsights:
    connectionString: ""
```

Pass as env var `APPLICATIONINSIGHTS_CONNECTION_STRING` to:
- `omnivec-api` deployment
- (Phase 2) `omnivec-changefeed` and `omnivec-worker` deployments

### 5. Web UI — Metrics Dashboard

The UI dashboard already reads from `/api/metrics` and `/api/metrics/timeseries`.
These endpoints already return:
- events_processed, events_failed
- avg_processing_time_ms
- daily breakdown
- per-pipeline stats
- timeseries data (throughput per bucket)

**No UI changes needed for Phase 1** — the existing dashboard already works.
The benefit of App Insights is backend observability + alerting, not UI changes.

**Phase 2 UI addition:** Add an "Azure Monitor" link in the dashboard that opens
the Application Insights overview in Azure Portal (deep link with resource ID).

## Implementation Steps

### Phase 1 (do now)

1. Add Bicep module: `infra/modules/appinsights.bicep`
   - Log Analytics Workspace (PerGB2018 pricing)
   - Application Insights (connected to workspace)
   - Output: connection string

2. Wire in `infra/main.bicep`:
   - Add module reference
   - Pass connection string to postprovision as Bicep output

3. Add Python SDK to `api/requirements.txt`:
   - `azure-monitor-opentelemetry` (recommended over opencensus)

4. Add `api/telemetry.py`:
   - Initialize Azure Monitor exporter
   - Custom metric functions
   - Request middleware for auto-instrumentation

5. Update `api/api.py`:
   - Import and initialize telemetry on startup
   - Emit custom metrics at key points

6. Update Helm chart:
   - Add `appInsights.connectionString` value
   - Set env var on API deployment

7. Update postprovision hooks:
   - Pass App Insights connection string from Bicep output to Helm

### Phase 2 (later)

8. Add App Insights to .NET services
9. Add Azure Monitor deep link to web UI
10. Configure alerts (error rate, pipeline stall, pod crashes)

## Files to modify

| File | Change |
|------|--------|
| `infra/modules/appinsights.bicep` | NEW — Log Analytics + App Insights |
| `infra/main.bicep` | Add appinsights module, output connection string |
| `infra/main.parameters.json` | No change (connection string from Bicep output) |
| `api/requirements.txt` | Add `azure-monitor-opentelemetry` |
| `api/telemetry.py` | NEW — telemetry init + custom metrics |
| `api/api.py` | Import telemetry, emit metrics at key points |
| `helm/omnivec/values.yaml` | Add `appInsights.connectionString` |
| `helm/omnivec/templates/api-deployment.yaml` | Add env var |
| `hooks/postprovision.ps1` | Pass connection string to Helm |
| `hooks/postprovision.sh` | Pass connection string to Helm |

## What the user sees

After `azd up`:
- Application Insights resource appears in their resource group
- API requests are automatically tracked
- Custom metrics (embedding throughput, pipeline health) flow to Azure Monitor
- They can open Azure Portal → Application Insights → Live Metrics for real-time view
- Alerts can be configured for error spikes, pipeline stalls

In the OmniVec UI:
- Dashboard continues to work as before (reads from API)
- (Phase 2) "View in Azure Monitor" link on dashboard

## Cost

| Resource | Monthly cost |
|----------|-------------|
| Log Analytics Workspace | ~$2/GB (first 5GB/month free) |
| Application Insights | Uses Log Analytics pricing |
| Data ingestion estimate | ~1-3 GB/month for small deployment |
| **Total** | **~$2-5/month** |
