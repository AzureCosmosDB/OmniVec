# OmniVec Agent (Phase 1 — read-only diagnostics)

The OmniVec Agent is an **in-cluster** FastAPI service that exposes a small,
read-only AI-ops surface for OmniVec operators. It runs as a sidecar Kubernetes
deployment (`omnivec-agent`) alongside the main `omnivec-api` service and is
reached *only* through the API proxy at `/api/agent/*` — it has **no Ingress**.

## Architecture

```
   Caller (browser / CLI / external)
         │ HTTPS · AAD JWT or scope-bearer
         ▼
   omnivec-api (auth, RBAC, audit)
         │ HTTP (in-cluster) · INTERNAL_API_TOKEN bearer + X-Caller-Id/Role
         ▼
   omnivec-agent (FastAPI)
         │
         ├── LLM (Azure OpenAI · managed identity)
         ├── Tool registry (≈ 30 read-only tools)
         │     ├── omnivec_api: GET endpoints on omnivec-api (uses Host header
         │     │   `omnivec-api` so the api's in-cluster allowlist skips auth)
         │     ├── k8s:        list pods / get pod logs / get events
         │     ├── cosmos:     count / get / SELECT-only query on allow-listed
         │     │               containers
         │     ├── servicebus: queue depth · DLQ count · list topics
         │     └── metrics:    recent errors · p99 latency · throughput
         └── Cosmos (omnivec.metadata)
               ├── agent_sessions   PK /user_id     TTL 30d
               └── agent_audit      PK /session_id  TTL 365d
```

Every tool call is recorded in `agent_audit` (tool, args, caller, timestamp,
latency, success/error). Sessions are scoped to `user_id` so a caller can never
see another user's history.

## Endpoints

| Method | Path                              | Description                                       |
|--------|-----------------------------------|---------------------------------------------------|
| GET    | `/v1/health`                      | Liveness (no auth)                                |
| GET    | `/v1/ready`                       | Readiness (no auth)                               |
| GET    | `/v1/tools`                       | List tools the calling role may invoke            |
| POST   | `/v1/chat`                        | SSE stream: `session`, `token`, `tool_call`, `tool_result`, `final`, `error`, `done` |
| GET    | `/v1/sessions`                    | List the caller's sessions                        |
| GET    | `/v1/sessions/{id}`               | Get one session with full message history        |
| DELETE | `/v1/sessions/{id}`               | Delete a session                                  |

External callers reach the same surface through `/api/agent/*` on
`omnivec-api`, which forwards to the agent service with the internal token.

## Security model

* **No public Ingress.** The agent service is `ClusterIP` only.
* **Auth between api ↔ agent:** the api proxy attaches a static
  `INTERNAL_API_TOKEN` (from `omnivec-agent-internal` secret) plus
  `X-Caller-Id` and `X-Caller-Role` headers populated from the validated
  caller JWT.
* **Tool wrappers** for `omnivec_api` set `Host: omnivec-api` so the api's
  internal-allowlist path is used — no token needed pod-to-pod, and audit
  records the *upstream* caller, not the agent.
* **K8s RBAC:** the `omnivec-agent` ServiceAccount holds a namespaced Role
  with `get/list/watch` on `pods`, `pods/log`, `events` — nothing else.
* **Cosmos:** the agent uses the same UAMI (workload identity) as the api;
  Cosmos data-plane RBAC restricts it to the `omnivec.metadata` database.
* **Read-only by design:** every tool in Phase 1 is declared `role="reader"`
  and contains no mutating verbs. Phase 2 introduces `role="admin"` tools
  behind explicit role checks.

## CLI

```bash
omnivec agent chat "list failing pipelines and show their last error"
omnivec agent sessions list
omnivec agent sessions show <id>
omnivec agent sessions delete <id>
omnivec agent tools
```

`omnivec agent chat` opens an SSE stream and prints tokens to stdout, tool
calls to stderr.

## Web UI

Navigate to the **Agent** tab in the OmniVec web UI for a chat panel with
session sidebar and model override.

## Configuration

| Env var                                  | Purpose                                         |
|------------------------------------------|-------------------------------------------------|
| `COSMOS_ENDPOINT`                        | Cosmos account                                  |
| `COSMOS_METADATA_DB`                     | Database (default `omnivec`)                    |
| `INTERNAL_API_TOKEN`                     | Shared secret with omnivec-api                  |
| `AOAI_ENDPOINT` / `AOAI_DEPLOYMENT`      | Azure OpenAI deployment                         |
| `AGENT_DEFAULT_MODEL_ID`                 | Optional override                               |
| `OMNIVEC_API_URL`                        | In-cluster api service URL                      |
| `DOCGROK_URL`                            | DocGrok router                                  |
| `OMNIVEC_NAMESPACE`                      | K8s namespace the agent inspects                |
| `SERVICEBUS_FQNS`                        | Service Bus FQDNs                               |
| `LOG_LEVEL`                              | `INFO` / `DEBUG`                                |
| `OMNIVEC_DEBUG`                          | If set, expose `/docs` (off in prod)            |

## Phase 1 limitations

* Read-only. No mutations of any OmniVec object.
* Both `reader` and `admin` see the same toolset (all tools are readonly).
* Cosmos client in the agent uses a facade — real `azure.cosmos.aio` wiring
  is left as TODO; tests substitute via module-level singletons.
