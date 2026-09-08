# DocGrok Operations Guide

The standard installation entrypoint is **`azd up`** from the repository root.
DocGrok is the `helm\docgrok` subchart of `helm\omnivec`, deployed in the
**`omnivec` release and namespace**. It is not a separate `docgrok` release
in an azd installation. Follow the repository `README.md` for prerequisites,
image builds, installation settings, and recovery after interrupted deployment.

## Select the environment

Deployment hooks keep an environment-specific kubeconfig instead of replacing
your default context. Use the path printed by deployment. PowerShell example:

```powershell
$Kubeconfig = Join-Path $HOME ".kube\omnivec-my-omnivec"
helm --kubeconfig $Kubeconfig status omnivec -n omnivec
kubectl --kubeconfig $Kubeconfig get services -n omnivec
kubectl --kubeconfig $Kubeconfig get deployments -n omnivec
```

Replace `my-omnivec` with the azd environment name. Standard services are:

| Service | In-cluster port | Deployment |
|---|---|---|
| `docgrok` | 80 | `docgrok` |
| `docgrok-controller` | 8081 | `docgrok-controller` |
| `pipeline-worker-svc` | 8080 | `docgrok-pipeline-worker` |

Cross-namespace callers should use names such as
`docgrok.omnivec.svc.cluster.local`. Model services depend on enabled models;
discover them rather than assuming a GPU backend has been deployed.

## Monitor and diagnose

```powershell
kubectl --kubeconfig $Kubeconfig get pods -n omnivec
kubectl --kubeconfig $Kubeconfig get events -n omnivec --sort-by=.lastTimestamp
kubectl --kubeconfig $Kubeconfig logs deployment/docgrok -n omnivec --tail=100
kubectl --kubeconfig $Kubeconfig logs deployment/docgrok-controller -n omnivec --tail=100
kubectl --kubeconfig $Kubeconfig logs deployment/docgrok-pipeline-worker -n omnivec --tail=100
kubectl --kubeconfig $Kubeconfig top pods -n omnivec
```

Check node readiness, scheduling events, image pulls, workload-identity
availability, model availability, and memory pressure before restarting
services. A deployment rollout or successful HTTP health response does not
prove that an ingestion pipeline is making progress.

### Processing that stops making progress

The document processor sends chunks through `/embed/batch` rather than
issuing one model request per chunk. `DOCGROK_EMBED_BATCH_SIZE` defaults to
`16` and accepts values from `1` to `128`. Lower it for models with smaller
batch/token budgets or limited GPU memory. Each batch retains the existing
120-second HTTP timeout; this is not a whole-document execution deadline.
Incomplete batch responses fail rather than silently dropping chunks.
Backend client errors, throttling, and server errors retain their HTTP status
so the queue worker can distinguish permanent failures from retryable ones.

Chunk sizes must be positive; overlap must be nonnegative and smaller than
the configured chunk size. Invalid settings fail explicitly instead of
allowing a non-progressing chunking loop. The legacy `chunk_size` override
reduces the built-in overlap when necessary. Failed or cancelled PDF
extraction removes its scratch file to avoid filling disk on repeated retries.

CLIP batching ignores cancelled request futures when publishing results, so
a client disconnect cannot terminate the shared scheduler and strand later
requests. Model inference, OCR, memory consumption, queue lock renewal, and
cluster health still need to be monitored separately.

## Model and routing-pipeline persistence

With `COSMOS_ENDPOINT`, `COSMOS_DATABASE`, and `COSMOS_CONTAINER` configured,
registry mutations must reach Cosmos DB before they change the local cache or
return success. Storage failures return HTTP 503 rather than acknowledging an
in-memory-only registration. A partial Cosmos configuration is a startup error.
Standalone runs with none of these settings still use an in-memory registry;
those registrations do not survive restarts.

Startup loads every Cosmos query page and retries failed loads before exiting
unsuccessfully. It does not start serving a successful empty registry after a
storage failure. Router and controller processes refresh their registry caches
every 15 seconds, retaining the last complete snapshot if a refresh fails.
Registry-list failures are surfaced to callers instead of returning an empty
list. Cache misses reload persisted registrations so a request routed to another
replica can resolve a newly registered model or routing pipeline.

File and transform requests resolve the model from their routing pipeline before
forwarding to the document processor. An explicit request `model_id` takes
precedence; model-free image/video transforms remain supported. If a request was
already dead-lettered because its model was missing, fixing the registry does
not replay it: selectively redrive the affected Service Bus messages after
confirming the model can generate an embedding.

## Exercise the router

Forward the router service in a separate terminal:

```powershell
kubectl --kubeconfig $Kubeconfig port-forward service/docgrok -n omnivec 8080:80
```

Then use the API with an existing registered embedding model:

```powershell
Invoke-RestMethod http://localhost:8080/health
Invoke-RestMethod -Method Post -Uri http://localhost:8080/embed/batch `
  -ContentType "application/json" `
  -Body '{"model_id":"mdl-ext-your-model","texts":["first document","second document"]}'
```

The batch endpoint takes `texts` plus a `model_id` or `pipeline`, and returns
one nested vector output per input. It does not accept the old `requests`
array of blob URLs. The deployed router does not expose
`/embed/batch/async` or a batch-status polling API.

Document extraction uses `/process` or `/process/blob`, which the router
forwards to the pipeline-worker. Do not send PDFs as text embedding batches.
Use registered, healthy models rather than interpreting mock embeddings as
an end-to-end ingestion test.

## Updates and recovery

For a source update, set `OMNIVEC_BUILD=true` in the azd environment and
rerun `azd up`; this rebuilds images even if their tags already exist.
Keep coupled watcher, worker, router, and pipeline-worker changes together.
See `docs\sharepoint-source.md` for SharePoint protocol migration requirements.

Manual parent-chart overrides require the `docgrok.` prefix and the existing
environment values. Prefer the deployment hooks to a bare Helm upgrade that
can drop settings. One-off scaling or value changes may be overwritten by
the next azd run.

For a targeted restart after diagnosing the failure:

```powershell
kubectl --kubeconfig $Kubeconfig rollout restart deployment/docgrok -n omnivec
kubectl --kubeconfig $Kubeconfig rollout status deployment/docgrok -n omnivec --timeout=180s
```

Do not uninstall a release or adopt namespace resources to clear a pending
Helm operation automatically. Inspect release history and establish whether
another deployment is active first. Uninstalling `omnivec` removes the entire
application release, not just DocGrok.
