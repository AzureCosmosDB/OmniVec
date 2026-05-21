# OmniVec Threat Model

| Field | Value |
|---|---|
| Owner | OmniVec Team |
| Last reviewed | 2026-05-11 |
| Methodology | STRIDE-at-boundaries, manually identified (per DPSS / SQL Security Review Board guidance) |
| Companion artifact | [`threat-model.tm7`](./threat-model.tm7) — open in [Microsoft Threat Modeling Tool](https://aka.ms/threatmodelingtool); **TMT auto-threat-generation is intentionally disabled** (Settings → Disable Threat Generation) |
| Companion (CI/CD) | [`cicd-threat-model.md`](./cicd-threat-model.md) |
| Latest review notes | [`threat-model-review-2026-05.md`](./threat-model-review-2026-05.md) |

---

## 0. Threat Model Information

Documents assumptions that are not visible on the diagrams.

### 0.1 Deployment & operating model

- **Single-tenant.** One customer = one AKS cluster + one set of Azure data-plane resources. There is no shared OmniVec backplane and no cross-tenant traffic.
- **Customer is operator.** The customer (or their operator) deploys via Helm, configures Azure resources, and owns the cluster's day-2 operations (upgrades, patching, scaling).
- **Logical, not physical.** Diagrams in this document are **logical** views of trust relationships. Pod replicas, node pools, AZ topology, and Helm release naming are deployment concerns and are intentionally omitted.

### 0.2 Identity assumptions

- End users authenticate to **Azure AD** (customer's tenant). OmniVec validates JWTs against AAD JWKS; no local user database.
- **Workload Identity Federation (Managed Identity (UAMI))** is the default auth between AKS pods and Azure managed services (CosmosDB, AOAI, Service Bus, Key Vault). Pods present federated tokens; no Azure access keys in pod env.
- A long-lived `OMNIVEC_ADMIN_TOKEN` exists as a **breakglass** path only; production deployments are expected to rely on AAD groups → roles.

### 0.3 Networking assumptions

- All public ingress goes through an **HTTPS-terminating ingress controller** (NGINX or AKS Application Gateway). Plain `LoadBalancer` Services are not used in production (search Service defaults to `ClusterIP`; see T-SRCH-1).
- **TLS protocol floor is a customer responsibility.** OmniVec does **not** enforce a minimum TLS version in application code — TLS termination, cipher suite selection, and protocol-version floor (e.g. TLS ≥ 1.2, disable 1.0/1.1) are configured at the ingress controller and the underlying OS / OpenSSL runtime. Diagrams continue to show `HTTPS` on public arrows because transport encryption is mandatory; **operators must independently configure the ingress controller (or AGIC / Application Gateway listener) to reject TLS < 1.2** as part of bring-up. See §0.6.2 and the operator checklist.
- In-cluster traffic is **plain HTTP** today.The `networkPolicy.enabled=true` toggle activates default-deny + per-component allow rules (see T-NET-1). mTLS / service-mesh is roadmap.
- Customer data-plane endpoints (CosmosDB, Blob) are reached over HTTPS via the public Azure backbone; Private Endpoints are supported but not required.

### 0.4 Customer assumptions

- Customer **owns** their source CosmosDB / Blob and the destination vector store. The customer trusts their own data; what OmniVec must defend against is **content-level risk** — a third party (an end user, an upstream system) can place a malicious document (crafted PDF/Office/image) into the customer's Blob, or set an attachment URL pointing at an attacker-controlled storage account. Our parsers and SSRF guards must therefore treat document bytes and blob URLs as hostile content (see T-PWK-1, T-CON-2 in §5).
- Customer is responsible for hardening *their* CosmosDB / Blob accounts (firewall, RBAC, data classification). OmniVec only assumes it can reach them.
- Customer configures `attachment_blob_account_allowlist` (storage-account hostnames OmniVec is permitted to fetch from). Misconfiguration = open SSRF (see T-CON-2).

### 0.5 What this model does NOT cover

| Out of scope | Why |
|---|---|
| **Azure AD / Microsoft Entra ID** (identity provider) | Microsoft-operated service in the customer's tenant. OmniVec only *consumes* it: validates JWTs against the public JWKS endpoint and reads group claims. We do not run, secure, configure, or rotate keys for AAD — Microsoft does. Shown on diagrams because the API talks to it, but the security of AAD itself (sign-in protection, conditional access, key rotation, tenant configuration) is Microsoft's + the customer tenant admin's responsibility. |
| **Azure AI Foundry / Azure OpenAI** (consumed by DocGrok) | Lives in the **customer's** Azure subscription. OmniVec only consumes it as a service over HTTPS+AAD; we have no control over the model deployment, content filters, network ACLs, or RBAC on the resource. Shown on diagrams (DocGrok calls it) but the security of the resource itself is the customer's responsibility. |
| CI/CD supply chain | Has its own model: [`cicd-threat-model.md`](./cicd-threat-model.md) |
| Helm chart / Bicep infra | Operator concern; tracked under `infra/` review |
| Code-level vulns (SQLi, XSS, deserialization) | Covered by SDL / CodeQL / SAST policy |
| Operational SIEM / alerting design | Handled by AppInsights consumer team |
| Customer's hardening of their CosmosDB / Blob | Customer responsibility (we assume *they* did it) |
| Pod replicas, node-pool / AZ layout | Deployment concern; not a security-design risk in a single-tenant cluster |

### 0.6 Operator (customer) responsibilities & assumptions

OmniVec's threat model holds **only if** the customer operates the deployment along the lines below. These are the assumptions the security design depends on; they double as an onboarding checklist for any customer taking the platform into production.

#### 0.6.1 Deployment assumptions

- **Single-tenant cluster.** One customer deployment = one AKS cluster + one set of Azure data-plane resources (CosmosDB / Blob / Service Bus / Key Vault / AOAI). OmniVec is **not** designed to be shared across tenants; do not co-locate two customers in one cluster.
- **Helm is the supported install path.** Customer deploys via the published `helm/omnivec` chart (or a thin overlay of it). Deviating from the chart (hand-rolled manifests, kustomize forks that drop securityContext / probes / RBAC bindings) is out-of-scope for this model.
- **Linux node pools only in production.** The parser sandbox (`T-RTR-1`) relies on Linux `RLIMIT_*` and `spawn`; Windows nodes silently lose the sandbox guarantee. Dev/CI on macOS/Windows is fine — production must be Linux.
- **Container image source.** Customer pulls signed images from the OmniVec ACR (or a mirror they control). Image signatures are verified at admission when `security.imageSigning.enabled=true` (cosign + sigstore policy-controller; see batch 8). Customers who disable signing are accepting `RES-3`.
- **Cluster day-2 ownership.** Customer owns AKS upgrades, node patching, autoscaling, log retention, and AppInsights / SIEM wiring. OmniVec ships defaults but does not operate the cluster.

#### 0.6.2 Networking assumptions

- **HTTPS-terminating ingress.** Customer fronts `omnivec-web` / `omnivec-api` with an HTTPS-terminating ingress controller (NGINX, Traefik, or Application Gateway/AGIC — all three Helm variants ship). A plain `LoadBalancer` Service for the API or Search is not a supported topology.
- **TLS certificate management.** Customer provisions and renews TLS certs (cert-manager + Let's Encrypt, or a corporate PKI). OmniVec does not issue certs.
- **TLS minimum version is the customer's call.** OmniVec relies on the ingress / OS / runtime for protocol-version enforcement and does **not** pin a minimum TLS version in application code. Customers **must** configure the ingress controller to require **TLS ≥ 1.2** (and ideally TLS 1.3) and to disable SSLv3 / TLS 1.0 / TLS 1.1 — e.g. NGINX `ssl-protocols: "TLSv1.2 TLSv1.3"` annotation, AGIC `appgw-ssl-policy` set to `AppGwSslPolicy20220101S` or stricter, Traefik `tls.options.minVersion=VersionTLS12`. Operators running OmniVec without applying one of these is accepting the residual described in §5 row **T-TLS-1**. This assumption is restated in the Threat Model Information (§0.3) because the diagrams show only `HTTPS` and do not encode a version floor.
- **In-cluster network policy.** Customer enables `networkPolicy.enabled=true` for default-deny + per-component allow (see `T-NET-1`). Leaving it off is a documented residual.
- **Egress reachability.** OmniVec pods must reach (a) `login.microsoftonline.com` (AAD JWKS), (b) the customer's CosmosDB / Blob / Service Bus / Key Vault / AOAI endpoints, and (c) the configured embedding endpoints (AOAI or in-cluster BGE/CLIP). If the cluster has a strict egress firewall, the customer is responsible for allow-listing these FQDNs.
- **Private Endpoints are optional but recommended.** `terraform/private-endpoints.tf` (batch 6/7) provisions PEs for Cosmos / Blob / Service Bus / Key Vault / AOAI. Customers in regulated environments should enable them; OmniVec works either way (`RES-1`).
- **Customer storage account hostnames are allow-listed.** Customer populates `attachment_blob_account_allowlist` with the storage-account hostnames OmniVec is permitted to fetch attachments from. An empty / wildcarded list re-opens `T-CON-2` (SSRF).

#### 0.6.3 Identity & access assumptions

- **AAD-first, breakglass-second.** Customer wires `OMNIVEC_AAD_TENANT_ID`, `_CLIENT_ID`, and the three group IDs (`*_ADMIN_GROUP_ID`, `*_OPERATOR_GROUP_ID`, `*_VIEWER_GROUP_ID`). End users authenticate via AAD; admin token is for breakglass only.
- **Reject-unmapped-principals is enabled in production.** Customer sets `OMNIVEC_AAD_REQUIRE_GROUP=1` so AAD principals that don't match any configured group are rejected (closes `T-AAD-1`). Default `0` is for backwards compat only.
- **Admin token rotated or disabled.** Customer either rotates `OMNIVEC_ADMIN_TOKEN` on a documented cadence (we recommend ≤ 90 days) **or** removes the env entirely in production once AAD is verified working. The audit log surfaces every admin-token use.
- **Workload Identity Federation for Azure resources.** Customer attaches a User-Assigned Managed Identity to the AKS pods via federated credentials. No Azure access keys in pod env, no service-principal secrets in Helm values.
- **Azure RBAC, not Cosmos master keys.** Customer grants the OmniVec UAMI Cosmos / Blob / AOAI data-plane RBAC roles. Cleartext API-key fallbacks exist in code for offline/dev only and should not be populated in production.

#### 0.6.4 Data & content assumptions

- **Customer owns and trusts their data stores.** OmniVec assumes the customer has hardened the source CosmosDB / Blob / SharePoint they point at us (firewall, RBAC, classification, encryption-at-rest with CMK if required). We don't audit or re-configure the source.
- **Document content is hostile.** Even though the customer owns the source, the *content* may have been placed by a third party (end users, upstream systems, partners). OmniVec treats every parsed byte as untrusted; the customer should not relax sandbox / SSRF / size-cap defaults below shipped values without a security review.
- **Customer-supplied embedding endpoints are trusted with content.** When a pipeline points at a customer-provided embedding endpoint (any model registered via `/api/models`), the endpoint sees plaintext document content. Customer is responsible for that endpoint's data-handling, retention, and residency posture.
- **Right-to-erasure is per-source.** Customer uses `DELETE /api/sources/{id}/vectors?cascade=true` to purge a source's vectors. Cascade purge on legacy (pre-batch-4) pipelines is pipeline-wide — customers operating legacy pipelines should run the `backfill_source_id.py` job first (`T-VEC-2`).
- **PII classification is the customer's call.** OmniVec does not classify or redact document content. If the source contains regulated data (PII, PHI, payment), the customer is responsible for source-side redaction + downstream access control.

#### 0.6.5 Operational security assumptions

- **Secrets live in Key Vault.** Customer stores AOAI keys, connector secrets, lease-account keys (if any) in Key Vault and references them via CSI driver or the AAD-RBAC pathway. Helm `values.yaml` should never contain a real secret.
- **Audit-log retention is wired up.** Audit events on state-changing routes (`POST/PATCH/DELETE /api/*`) are emitted as structured logs. Customer ships them to AppInsights / Log Analytics / their SIEM with retention that meets their compliance bar.
- **Rate-limits are accepted defaults or tightened.** Per-token sliding-window rate-limit on the API and `/search` ships with conservative defaults. Customer may raise via the `rate_limit_rpm` field on the token doc but should not disable globally.
- **Ingress security headers are enabled.** Customer keeps the chart's ingress-template defaults (CSP, X-Frame-Options, Permissions-Policy, rate-limit annotations). Stripping them re-opens browser-side risks the in-process CSP only partially mitigates.
- **Cluster autoscaling sized for embed concurrency.** `OMNIVEC_EMBED_CONCURRENCY` (default 4) caps in-flight AOAI calls per replica. Customer scaling the API horizontally multiplies effective concurrency; they should size the AOAI deployment's TPM accordingly to avoid `T-RL-1` (429 amplification).
- **Patching cadence.** Customer applies OmniVec chart upgrades on the published cadence (monthly minor, immediate for security advisories). Running an N-2 chart in production is not a supported security posture.

---

## 1. Scope

OmniVec is a **single-tenant** retrieval-augmented vector platform that ingests customer documents, generates embeddings, and serves search/RAG. See §0 for deployment, identity, networking, and customer assumptions.

**In scope for this threat model:**
- Cluster-internal services (web, api, search, ingestor, dotnet-worker, docgrok router/pipeline-worker, in-cluster embedders)
- Trust boundaries crossed by user, customer data, and Azure managed services
- Bootstrap/admin authentication and AAD integration
- Inter-component data plane

**Out of scope:** see §0.5.

## 2. What we're working on — high-level view

**One diagram, 5 shapes, logical view.** Everything OmniVec owns is collapsed into a single black box. Trust boundaries are red dashed lines. External interactors (outside our security responsibility) are marked `[ext]` with a justification in §0.5. Per-component breakdowns live in §3 scenarios.

```mermaid
flowchart LR
  callers(["Callers<br/>browser · CLI · embedded app<br/>[ext]"])

  subgraph msft["Microsoft-operated — OOS"]
    aad(["Azure AD<br/>[ext, OOS]"])
  end

  subgraph omnivecbox["OmniVec — single-tenant AKS (TB-2)"]
    direction TB
    api["Web / API control plane"]
    docgrok["DocGrok"]
    conn["Ingestion connector"]
    envcfg[("Local configuration<br/>env vars · Helm values")]
  end

  subgraph azuremgd["TB-3 · OmniVec-owned Azure managed services"]
    direction LR
    cmeta[("CosmosDB metadata account")]
    kv[("Key Vault")]
    appi[("Application Insights")]
  end

  subgraph custsub["Customer Azure subscription — OOS"]
    foundry(["Azure OpenAI / Foundry<br/>[ext, OOS]"])
  end

  customer(["Customer data plane<br/>source / vectors destination<br/>[ext]"])

  callers -- "O1" --> api
  api -.-> |O2| aad
  api -- "O3" --> docgrok
  conn -- "O4" --> docgrok
  docgrok -- "O5" --> foundry
  conn <-- "O6 (read+write)" --> customer

  api -- "O7" --> cmeta
  api -- "O7" --> kv
  api -. "O7" .-> appi
  docgrok -- "O8" --> cmeta
  docgrok -- "O8" --> kv
  docgrok -. "O8" .-> appi
  conn -- "O9" --> cmeta
  conn -- "O9" --> kv
  conn -. "O9" .-> appi

  %% No arrows from `envcfg` — it is documented as the local-config source
  %% per reviewer ask, but env-var / Helm-value reads are pod-internal
  %% (no network, no trust-boundary crossing). Key Vault references resolved
  %% at runtime are already covered by O7/O8/O9 (component -> Key Vault).

  style msft fill:#fff5f5,stroke:#c00,stroke-dasharray: 5 5
  style custsub fill:#fff5f5,stroke:#c00,stroke-dasharray: 5 5
  style omnivecbox fill:#f5faff,stroke:#369
  style azuremgd fill:#f0fff4,stroke:#393,stroke-dasharray: 3 3
```

**Flow details — Overall (§2)**

Per reviewer guidance (2026-05-21): arrows on the diagram carry only flow-IDs to keep the picture readable; the table below carries the security details.

| # | From → To | Purpose | Transport / auth | Mitigation refs (§5) |
|---|---|---|---|---|
| **O1** | Callers → Web/API | Queries · admin · token-mint | HTTPS · TLS ≥ 1.2 (customer-configured) · AAD bearer or `scope=search` bearer | T-API-1, T-TLS-1, T-SRCH-2 |
| **O2** | Web/API → Azure AD | JWT validation (JWKS fetch) | HTTPS · public endpoint · cached 1 h | T-AAD-1/2 |
| **O3** | Web/API → DocGrok | In-cluster embed for search/RAG | HTTP · `X-Admin-Token` · NetworkPolicy `api → docgrok` | T-NET-1 |
| **O4** | Connector → DocGrok | In-cluster embed for ingest batches | HTTP · `X-Admin-Token` · NetworkPolicy `connector → docgrok` | T-NET-1 |
| **O5** | DocGrok → Foundry | Embedding call (consume only) | HTTPS · Managed Identity (UAMI) or API key | T-MET-1, T-RL-1 |
| **O6** | Connector ↔ Customer data plane | **Unified** read source + write vectors (OmniVec-initiated, both directions) | HTTPS · UAMI / SAS · host allowlist · parser sandbox · Cosmos RBAC | T-CON-1/2, T-PWK-1, T-VEC-1 |
| **O7** | Web/API → TB-3 (Cosmos metadata · Key Vault · App Insights) | Config read/write + token hashes · resolve secret refs · export telemetry | HTTPS · UAMI · per-resource RBAC (Cosmos Data Contributor · KV Secret Reader · AppInsights Metrics Publisher) | T-MET-1 |
| **O8** | DocGrok → TB-3 (same 3 resources) | Read model records · resolve AOAI secret refs · export telemetry | HTTPS · UAMI · per-resource RBAC | T-MET-1, T-FUZ-1 |
| **O9** | Connector → TB-3 (same 3 resources) | Read pipeline/source records · resolve connector secret refs · export telemetry | HTTPS · UAMI · per-resource RBAC | T-MET-1 |

> **Note on Local configuration:** the `envcfg` node is shown inside TB-2 to satisfy the reviewer's ask that the configuration source be represented in the diagram. It is **intentionally not connected by arrows** to the three OmniVec components — env vars and mounted Helm values are pod-internal state read at process start (no network, no trust-boundary crossing), and Key Vault references are resolved at runtime via UAMI which is already drawn as the per-component arrows to **Key Vault** in O7 / O8 / O9. Operator responsibility: never put plaintext secrets in `values.yaml`; use Key Vault references resolved by UAMI.

> **What this view shows**: the OmniVec AKS deployment is drawn as **exactly three components** (per 2026-05-21 reviewer guidance, refined to the three roles the team actually operates):
> 1. **Web / API control plane** — `omnivec-web` (static UI) + `omnivec-api` (admin CRUD, token mint, query proxy) + `omnivec-search` (kNN + RAG). Handles all caller traffic.
> 2. **DocGrok** — `docgrok-router` + `docgrok-pipeline-worker` with the parser sandbox; the only component that talks to Azure OpenAI / Foundry for embeddings.
> 3. **Ingestion source/destination connector** — `omnivec-ingestor` (change-feed watcher) + `omnivec-dotnet-worker` (Service Bus consumer); the only component that talks to the customer data plane.
>
> The three **OmniVec-owned Azure managed services** — **CosmosDB metadata**, **Key Vault**, and **Application Insights** — are intentionally grouped inside a **single trust boundary** (green dashed). They live in OmniVec's own Azure subscription, share the same authentication mechanism (UAMI), and share the same blast radius (compromise of one UAMI principal can reach any of them subject to per-resource RBAC). Telemetry export from each of the three OmniVec components flows into this same box (Application Insights). Per-resource RBAC scopes are documented in the §3.2 / §3.3 / §3.4 flow tables.
>
> The **OIDC sign-in flow from Callers → Azure AD** is intentionally **not drawn** — External Interactors are black boxes in this model and we do not represent their internal workings. JWT validation (OmniVec → AAD JWKS) **is** drawn because OmniVec initiates it.
>
> The customer-data-plane arrow is drawn **bidirectional** because OmniVec initiates both the read (change-feed / attachments) and the write (embedding vectors). Per reviewer guidance, same-actor read+write flows are unified rather than split into two directional artifacts.
>
> The `envcfg` node represents the **local configuration source** (environment variables and Helm values mounted into each pod). It is read once at process start; no network traffic. Key Vault references are resolved separately via UAMI (see §3.2 U6).

**Components**

| Component | Responsibility | Credentials | Receives external content from |
|---|---|---|---|
| **Web / API control plane** | omnivec-web (static UI), omnivec-api (admin CRUD, token mint, query proxy), omnivec-search (kNN + RAG). Validates AAD JWTs and `scope=search` bearer tokens. | AAD svc-principal (JWT validation), UAMI (Cosmos / Key Vault data-plane RBAC) | Callers (TB-1) |
| **DocGrok** | docgrok-router + docgrok-pipeline-worker with parser sandbox; only component that calls Azure OpenAI / Foundry for embeddings. | UAMI with AOAI Cognitive Services User; Cosmos read on `omnivec.metadata.docgrok_model` | Customer document content (TB-4, via the connector) |
| **Ingestion source/destination connector** | omnivec-ingestor (change-feed watcher) + omnivec-dotnet-worker (Service Bus consumer); only component that touches the customer data plane (read source / write vectors). | UAMI with Service Bus Send/Receive, Cosmos RBAC read on source / write on vectors, Blob read on attachments | Customer documents (TB-4), customer attachment blobs (TB-4) |
| **OmniVec-owned Azure managed services** (single TB-3 boundary, **three distinct resources**) | Three separate Azure resources sharing one trust boundary because they share the OmniVec subscription, the same UAMI, and the same blast radius — but each is its own shape on the diagram, with its own RBAC role and its own per-flow arrow: (a) **CosmosDB metadata account** — `omnivec.metadata` (pipelines / sources / tokens / models); (b) **Key Vault** — secret references resolved at runtime; (c) **Application Insights** — telemetry sink (traces / metrics / logs). | UAMI with per-resource data-plane RBAC: Cosmos `DocumentDB Data Contributor`, Key Vault `Secret Reader`, AppInsights `Monitoring Metrics Publisher` | n/a |
| **Local configuration (env vars / Helm values)** | Source of truth at process start for AAD tenant/group IDs, Key Vault references, allowlists, rate-limit defaults, feature flags. Customer-owned. | n/a — file/env read only | n/a |

**Trust boundaries**

| Id | Boundary | Threat-model relevance |
|---|---|---|
| TB-1 | Internet ↔ API | Public HTTPS surface to OmniVec (in scope). AAD itself sits *outside* this boundary as a Microsoft-operated external interactor — out of scope; only token validation is in scope. |
| TB-2 | Inter-component within cluster | Plain HTTP today; cross-component compromise = lateral movement (mitigated by NetworkPolicy) |
| TB-3 | AKS ↔ Azure managed services | Workload Identity Federation (HTTPS via Managed Identity), not key-based |
| TB-4 | OmniVec ↔ customer data plane | Customer-supplied document content and attachment URLs may originate from a third party; parser must assume hostile content (T-PWK-1) and SSRF guard the URL host (T-CON-2) |

## 3. Scenario diagrams

Four views, each focused on a distinct audience. Per reviewer guidance: request flows only (responses omitted unless they cross a new boundary), two-line labels (purpose / how secured), authorization noted. Each scenario is followed by a **Flow Details** table that documents per-flow purpose, transport, authentication, authorization, data sensitivity, and applied mitigations.

| # | View | Audience | What it shows | What it hides |
|---|---|---|---|---|
| **3.1** | Overall (5-shape) | Exec / SQL board | OmniVec as a black box vs. its 4 external interactors | All internals |
| **3.2** | User control plane | App-sec / API reviewers | Callers, AAD, Web, API, metadata, Key Vault — admin CRUD, token mint, sign-in, secret resolve | Search, Ingestion, DocGrok, Foundry |
| **3.3** | Search read path | App-sec / API reviewers | Browser-side search via API **and** programmatic search via `searchIngress`; embed hop to DocGrok / Foundry | Ingestion, dotnet-worker, customer source data |
| **3.4** | Ingestion / embedding data plane | Data-plane / SRE reviewers | Customer source → Ingestor → Service Bus → dotnet-worker → DocGrok → Foundry → customer vectors | Callers, AAD, Web, API |

### 3.1 Overall (high-level view)

The 5-shape black-box diagram is in **§2 above** — it is the canonical overall view. The three remaining diagrams below zoom into the OmniVec black box from three different angles.

### 3.2 User control plane (admin CRUD · token mint · sign-in)

> **Audience:** anyone reviewing the user-facing control surface — who can sign in, what they can change, and where config / token records live. Search is *not* on this diagram (see §3.3).
>
> **Authorization:** admin CRUD and token-mint endpoints (`POST/GET/DELETE /api/auth/tokens`, `POST/PUT/DELETE /api/{pipelines,sources,models}`) require `role=admin` — obtainable via AAD JWT in the `Admin` group, an opaque `scope=admin` bearer, or the breakglass `OMNIVEC_ADMIN_TOKEN`. Read-only endpoints require `Reader`.

```mermaid
flowchart LR
  callers(["Callers<br/>browser · CLI · embedded user app<br/>[ext]"])

  subgraph msft["Microsoft-operated — out of scope"]
    aad(["Azure AD<br/>[ext, OOS]"])
  end

  web["Web<br/>(UI static assets)"]
  api["API<br/>(admin CRUD · token mint)"]
  cmeta(["CosmosDB metadata<br/>omnivec.metadata + tokens<br/>[ext, Azure-managed]"])
  kv(["Key Vault<br/>[ext, Azure-managed]"])

  envcfg[("Local configuration<br/>env vars · Helm values<br/>[customer-owned]")]

  callers -->|"U1 · GET / (UI assets)<br/>HTTPS · static, no auth"| web
  callers -->|"U3 · {GET,POST,PUT,DELETE} /api/* (admin CRUD · token mint)<br/>HTTPS (customer-configured TLS ≥ 1.2) · AAD JWT (browser) or scope=admin bearer (CLI/script) — role: Admin/Reader"| api
  api -->|"U4 · JWKS validation<br/>HTTPS to login.microsoftonline.com · cached 1h"| aad
  api -->|"U5 · {read,write} /dbs/omnivec/colls/metadata/docs<br/>HTTPS · Managed Identity (UAMI) · Cosmos data-plane RBAC"| cmeta
  api -->|"U6 · GET /secrets/{name}<br/>HTTPS · Managed Identity (UAMI) · Key Vault Secret Reader"| kv
  envcfg -->|"U7 · process-start read<br/>pod env / mounted Helm values · no network"| api

  style msft fill:#fff5f5,stroke:#c00,stroke-dasharray: 5 5
```

**Flow details — User control plane**

| # | Purpose | Transport | AuthN | AuthZ | Data on the wire | Mitigations (refs §5) |
|---|---|---|---|---|---|---|
| **U1** | Browser fetches static UI assets | HTTPS (TLS 1.2+) terminated at NGINX | None (static) | n/a | HTML/JS/CSS | DefenseInDepth: CSP header set by web image |
| **U3** | Caller invokes admin CRUD / token mint endpoints | HTTPS — **customer-configured TLS ≥ 1.2** at the ingress controller / OS (OmniVec does **not** pin a TLS floor; see §0.3) | AAD JWT (browser) or opaque `scope=admin` bearer (CLI/script); JWT validated against tenant JWKS — signature, `aud`, `iss`, `exp` | AAD group claim `Admin` or `Reader`; token-mint endpoints require `role=admin` | Config payloads (refs to secrets only); new-token plaintext (returned **once** at mint) | T-API-1 (AAD enforced; admin token = breakglass; audit log) |
| **U7** | API / Web / Search read local configuration (env vars + mounted Helm values) at process start — AAD tenant/group IDs, allowlists, feature flags, KV references. **No network.** | Pod env / mounted file | n/a (read-only, in-pod) | Pod ServiceAccount (Kubernetes RBAC for the Secret/ConfigMap mounts) | Configuration values; **never raw secrets** — KV references only | Operator responsibility: never put plaintext secrets in `values.yaml`; use KV CSI driver |
| **U4** | API verifies the user's JWT signature against Microsoft's signing keys | HTTPS to `login.microsoftonline.com` (egress) | JWKS endpoint (public); response cached 1 h with `Cache-Control` honored | n/a | JWKS keys (public) | DefenseInDepth: optional cert pin via env var |
| **U5** | API persists/reads config records (pipelines, sources, model records, hashed tokens) | HTTPS to Azure Cosmos endpoint | Managed Identity (UAMI) | Cosmos data-plane RBAC; **read/write** on `omnivec.metadata`; tokens stored as SHA-256 hashes only | Config records (refs to secrets only), token records (hashed) | T-MET-1 (no key material in records; tokens hashed-at-rest) |
| **U6** | API resolves secret references at runtime (never stores secret values) | HTTPS to Key Vault | Managed Identity (UAMI) | Key Vault RBAC; **get** scope on a named secret | Secret value (in-memory only) | T-MET-1 |

### 3.3 Search read path (browser + programmatic)

> **Audience:** anyone reviewing the read/RAG surface. Shows both query entry points — **browser/CLI via the API** (AAD JWT or `scope=admin` bearer) and **programmatic via the dedicated `searchIngress`** (`scope=search` opaque bearer) — and the shared embed hop into DocGrok / Foundry.
>
> **Authorization:** browser/CLI queries require AAD `Reader`/`Admin` (or `scope=admin` bearer); programmatic queries require an opaque `scope=search` bearer minted by an Admin and validated by SHA-256 lookup in Cosmos. Results are filtered by source-id ACL.

```mermaid
flowchart LR
  callers(["Callers<br/>browser · CLI · embedded user app<br/>[ext]"])

  subgraph msft["Microsoft-operated — out of scope"]
    aad(["Azure AD<br/>[ext, OOS]"])
  end

  api["API<br/>(query proxy)"]
  search["Search<br/>(direct via searchIngress for programmatic callers)"]
  docgrok["DocGrok<br/>(router + pipeline-worker)"]
  cmeta(["CosmosDB metadata<br/>tokens partition (SHA-256)<br/>[ext, Azure-managed]"])
  cvec(["Customer vectors<br/>[ext]"])

  subgraph custsub["Customer Azure subscription — out of scope"]
    foundry(["Azure OpenAI / Foundry<br/>[ext, OOS]"])
  end

  callers -->|"S1 · POST /api/assistant/query (browser/CLI)<br/>HTTPS · AAD JWT (Reader/Admin) or scope=admin bearer"| api
  callers -->|"S2 · POST /api/search (programmatic)<br/>HTTPS via dedicated searchIngress · scope=search bearer (opaque · SHA-256 in Cosmos)"| search
  api -.->|"S3 · JWKS validation<br/>HTTPS to login.microsoftonline.com · cached 1h"| aad
  api -->|"S4 · in-cluster /v1/search (browser query)<br/>HTTP · X-Admin-Token · NetworkPolicy: api → search"| search
  search -->|"S5 · GET /dbs/omnivec/colls/metadata/docs (token verify · SHA-256)<br/>HTTPS · Managed Identity (UAMI) · read-only on tokens partition"| cmeta
  search -->|"S6 · POST /v1/embed (in-cluster)<br/>HTTP · X-Admin-Token · NetworkPolicy: search → docgrok-router"| docgrok
  docgrok -->|"S7 · POST /openai/deployments/{name}/embeddings<br/>HTTPS · UAMI access token (or legacy API key)"| foundry
  search -->|"S8 · POST /dbs/{db}/colls/{c}/docs (vector kNN)<br/>HTTPS · Managed Identity (UAMI) · Cosmos data-plane RBAC + source-id ACL"| cvec

  style msft fill:#fff5f5,stroke:#c00,stroke-dasharray: 5 5
  style custsub fill:#fff5f5,stroke:#c00,stroke-dasharray: 5 5
```

**Flow details — Search read path**

| # | Purpose | Transport | AuthN | AuthZ | Data on the wire | Mitigations (refs §5) |
|---|---|---|---|---|---|---|
| **S1** | Browser/CLI submits a natural-language RAG question via the API | HTTPS (TLS 1.2+) terminated at NGINX ingress | AAD JWT (Bearer) validated against tenant JWKS, **or** opaque `scope=admin` bearer (CLI) | AAD group `Reader`/`Admin`; per-source ACL downstream | Query text (may contain PII) + Bearer token | T-API-1 (AAD enforced), T-SRCH-1 (TLS at ingress, ClusterIP) |
| **S2** | Backend service / embedded app queries Search directly without the API in path | HTTPS (TLS 1.2+) terminated at NGINX `searchIngress` (dedicated host, dedicated cert, dedicated rate-limit policy) | `Authorization: Bearer <search-token>`; server-side SHA-256 compare against `omnivec.metadata.tokens`; admin-scope tokens **rejected** unless `SEARCH_ACCEPT_ADMIN_TOKEN=true` | scope=`search` required; per-source ACL downstream | Query text (may contain PII) + bearer token | T-SRCH-1 (TLS at dedicated ingress), T-SRCH-2 (long-lived static tokens), T-RL-1 (rate limit on searchIngress) |
| **S3** | API verifies the user's JWT signature against Microsoft's signing keys | HTTPS to `login.microsoftonline.com` (egress) | JWKS endpoint (public); response cached 1 h | n/a | JWKS keys (public) | DefenseInDepth: optional cert pin via env var |
| **S4** | API hands a browser-side query to Search in the same cluster | In-cluster HTTP (port 8080) | Service-account-derived admin token in header | NetworkPolicy: only `omnivec-api` pods may reach `omnivec-search` | Query text | T-NET-1 (NetworkPolicy default-deny + allow rule) |
| **S5** | Search validates a `scope=search` bearer presented at `searchIngress` | HTTPS to Azure Cosmos endpoint | Managed Identity (UAMI) | Cosmos data-plane RBAC; **read-only** on `omnivec.metadata.tokens` partition | Token hash + caller label + scope + TTL | T-MET-1 (hashed-at-rest), T-SRCH-2 (rotation) |
| **S6** | Search asks DocGrok to embed the query text into a vector | In-cluster HTTP (port 8080) | `X-Admin-Token` header (rotated at deploy) | NetworkPolicy: only `omnivec-search` may reach `docgrok-router` | Query text | T-NET-1, T-API-1 (token confined to in-cluster) |
| **S7** | DocGrok calls **customer's** Azure OpenAI / Foundry to produce the embedding (shared with I7 in §3.4) | HTTPS to `*.openai.azure.com` | Managed Identity (UAMI, preferred); or legacy API key from `omnivec.metadata.docgrok_model.api_key` | RBAC on the AOAI resource (customer-managed); content filters (customer-managed) | Query text → embedding vector | T-MET-1 (legacy keys deprecated; AAD-only model records), T-RL-1 (per-deployment embed semaphore); out-of-scope: Foundry resource itself |
| **S8** | Search runs vector kNN against the customer's vector store | HTTPS to customer Cosmos endpoint | Managed Identity (UAMI) | Cosmos data-plane RBAC; OmniVec further filters results by source-id ACL in code | Query embedding + retrieved chunks (may contain PII) | T-VEC-1 (PII classification; cascade purge), T-NET-1 |

> **Identity model on S1 vs S2:** S1 uses AAD JWT (browser) or opaque `scope=admin` bearer (CLI / scripts that don't want an AAD app registration). S2 always uses an opaque `scope=search` bearer, never AAD. Customers who want AAD-only on the search path can leave `searchIngress.enabled=false` and rely on S1 → S4 only.

### 3.4 Ingestion / embedding data plane

> **Audience:** data-plane / SRE reviewers. Shows how customer documents become embeddings and land in the customer's vector store. **No user identities, no AAD, no API ingress on this path** — everything runs unattended under workload identities.
>
> **Two processing modes** (per `pipeline.processing_mode`):
> - **`queue` (default)** — Ingestor enqueues work to Service Bus; `dotnet-worker` drains, asks DocGrok to embed, writes to a separate vectors destination. Higher latency, higher throughput, decoupled retry. Flows **I1–I8**.
> - **`inline`** — Ingestor calls DocGrok directly and **patches the source document** with the embedding (source = destination; no Service Bus, no `dotnet-worker`). Lower latency, no queue overhead, suitable when source and destination are the same Cosmos container. Flows **I1–I3, I9–I11** (skips I4–I8).
>
> **Authorization:** every flow uses Managed Identity (UAMI). The customer's RBAC on their own CosmosDB / Blob / Service Bus *is* the authorization boundary; OmniVec applies host-allowlists for SSRF and a parser sandbox for hostile content.

```mermaid
flowchart LR
  csrc(["Customer source<br/>CosmosDB / Blob<br/>[ext · hostile content assumed]"])
  cmeta(["CosmosDB metadata<br/>pipeline / source / model config<br/>[ext, Azure-managed · read-only here]"])
  ingest["Ingestion<br/>(change-feed watcher · queue producer · inline embedder)"]
  sb(["Service Bus<br/>[ext, Azure-managed]"])
  worker["dotnet-worker<br/>(queue consumer)"]
  docgrok["DocGrok<br/>(router + pipeline-worker · parser sandbox)"]
  cvec(["Customer vectors destination<br/>e2eblob.vectors / pgvector<br/>[ext]"])

  subgraph custsub["Customer Azure subscription — out of scope"]
    foundry(["Azure OpenAI / Foundry<br/>[ext, OOS]"])
  end

  csrc -->|"I1 · GET /dbs/{db}/colls/{c}/_changefeed (source docs)<br/>HTTPS · Managed Identity (UAMI) · Cosmos read on source + lease container"| ingest
  csrc -->|"I2 · GET attachment blob (PDF/Office/image)<br/>HTTPS · UAMI or SAS · attachment_blob_account_allowlist"| ingest
  cmeta -->|"I3 · GET pipeline/source/model record<br/>HTTPS · UAMI · Cosmos read-only on omnivec.metadata"| ingest

  ingest -->|"I4 [queue] · POST topics/{source}/messages<br/>HTTPS to *.servicebus.windows.net · UAMI · SB Send"| sb
  sb -->|"I5 [queue] · receive subs/{source}/messages<br/>HTTPS · UAMI · SB Receive"| worker
  worker -->|"I6 [queue] · POST /v1/embed/batch (in-cluster)<br/>HTTP · X-Admin-Token · NetworkPolicy: dotnet-worker → docgrok-router"| docgrok
  docgrok -->|"I7 · POST /openai/deployments/{name}/embeddings<br/>HTTPS · UAMI access token (or legacy API key)"| foundry
  worker -->|"I8 [queue] · PATCH /dbs/{db}/colls/{c}/docs (vector upsert)<br/>HTTPS · UAMI · Cosmos write on e2eblob.vectors"| cvec

  ingest -.->|"I9 [inline] · POST /v1/embed (in-cluster)<br/>HTTP · X-Admin-Token · NetworkPolicy: ingestion → docgrok-router"| docgrok
  ingest -.->|"I10 [inline] · PATCH /dbs/{db}/colls/{c}/docs (embed inline into source doc)<br/>HTTPS · UAMI · Cosmos write on source container"| csrc
  ingest -.->|"I11 [inline · optional] · PATCH /dbs/{db}/colls/{c}/docs (vector upsert to separate destination)<br/>HTTPS · UAMI · Cosmos write on e2eblob.vectors"| cvec

  style custsub fill:#fff5f5,stroke:#c00,stroke-dasharray: 5 5
```

> **Note 1 (shared embed hop):** the **same** `docgrok → foundry` hop (I7) is reused on **all three** embed paths — queue (I6 → I7), inline (I9 → I7), and Search read path (S6 → S7). Threats on this hop apply uniformly.
>
> **Note 2 (inline-mode write target):** in inline mode, the default is to write the embedding back into the *source* document (I10), so `csrc` becomes its own destination. A pipeline can also be configured to write to a separate destination container (I11) — same UAMI, same Cosmos RBAC, same threats as I8.

**Flow details — Ingestion / embedding**

| # | Mode | Purpose | Transport | AuthN | AuthZ | Data on the wire | Mitigations (refs §5) |
|---|---|---|---|---|---|---|---|
| **I1** | both | Ingestor reads new/updated documents from customer Cosmos change-feed | HTTPS to customer Cosmos endpoint | Managed Identity (UAMI) | Cosmos data-plane read on customer's source container; dedicated lease container | Document JSON (third-party-supplied content possible; may contain PII) | T-CON-1 (optional dedicated lease account) |
| **I2** | both | Ingestor downloads attachment binaries referenced by docs | HTTPS to customer Blob endpoint | Managed Identity (UAMI) or pre-shared SAS URL | **Host allowlist** (`attachment_blob_account_allowlist`) — only configured storage accounts accepted | Binary content from a customer-configured (potentially third-party-authored) source | T-CON-2 (SSRF: mandatory allowlist; absolute-URL host pinning), T-PWK-1 (parser sandbox) |
| **I3** | both | Ingestor loads pipeline / source / model definition from OmniVec metadata | HTTPS to Azure Cosmos endpoint | Managed Identity (UAMI) | Cosmos data-plane RBAC; **read** scope on `omnivec.metadata` | Pipeline / source records (refs only) | T-MET-1 |
| **I4** | queue | Ingestor publishes per-document work items to Service Bus | HTTPS to `*.servicebus.windows.net` | Managed Identity (UAMI) | Service Bus RBAC; **send** on per-source topic | Document id + source ref + blob URL (no body) | T-NET-1 (out-of-cluster traffic on TLS) |
| **I5** | queue | dotnet-worker drains work items from Service Bus | HTTPS to `*.servicebus.windows.net` | Managed Identity (UAMI) | Service Bus RBAC; **receive** on per-source subscription | Document id + source ref + blob URL | — |
| **I6** | queue | dotnet-worker asks DocGrok to parse and embed a batch | In-cluster HTTP (port 8080) | `X-Admin-Token` header (rotated at deploy) | NetworkPolicy: only `omnivec-dotnet-worker` may reach `docgrok-router` | Parsed text chunks (may contain PII) | T-PWK-1 (subprocess sandbox in DocGrok parser), T-NET-1 |
| **I7** | both | DocGrok calls **customer's** Azure OpenAI / Foundry to produce the embedding (shared with I6, I9, S6) | HTTPS to `*.openai.azure.com` | Managed Identity (UAMI, preferred); or legacy API key from `omnivec.metadata.docgrok_model.api_key` | RBAC on the AOAI resource (customer-managed); content filters (customer-managed) | Text → embedding vector | T-MET-1 (legacy keys deprecated; AAD-only model records), T-RL-1 (per-deployment embed semaphore); out-of-scope: Foundry resource itself |
| **I8** | queue | dotnet-worker writes resulting vectors to the customer's vector store | HTTPS to customer Cosmos endpoint | Managed Identity (UAMI) | Cosmos data-plane RBAC; **write** scope on `e2eblob.vectors` | Embedding vectors + chunk metadata (PII-derived) | T-VEC-1 (cascade-purge endpoint; PII classification) |
| **I9** | inline | Ingestor itself asks DocGrok to embed (no queue, no worker) | In-cluster HTTP (port 8080) | `X-Admin-Token` header | NetworkPolicy: only `omnivec-ingestion` may reach `docgrok-router` | Parsed text chunks (may contain PII) | T-PWK-1, T-NET-1; **inline-mode-specific:** failures are not retried via queue, so transient DocGrok / Foundry errors bubble straight to the change-feed checkpoint and are retried only on the next poll |
| **I10** | inline | Ingestor patches the **source document** in-place with the embedding (source = destination) | HTTPS to customer Cosmos endpoint | Managed Identity (UAMI) | Cosmos data-plane RBAC; **write** scope on the *source* container (broader than queue mode, which only writes to the vectors destination) | Embedding vectors + chunk metadata patched onto source doc | T-VEC-1; **inline-mode-specific:** broader write surface — UAMI now needs Cosmos write on source data, not just destination |
| **I11** | inline (opt.) | Ingestor writes vectors to a separate destination (when configured) | HTTPS to customer Cosmos endpoint | Managed Identity (UAMI) | Cosmos data-plane RBAC; **write** scope on `e2eblob.vectors` | Embedding vectors + chunk metadata | T-VEC-1 (same as I8) |



## 4. Assets

| Asset | Sensitivity | Where it lives |
|---|---|---|
| Customer document content | **High** (may be PII) | Customer Blob → AKS RAM (transient) |
| Vector embeddings of customer content | **High** (PII-derived; partially invertible) | Customer destination (`e2eblob.vectors`) |
| AOAI API keys (legacy) | **High** | `omnivec.metadata.docgrok_model.api_key` — being removed in favor of AAD |
| `OMNIVEC_ADMIN_TOKEN` | **High** | Pod env var; long-lived; breakglass-only after AAD migration |
| AAD JWT signing keys (JWKS) | High | Microsoft tenant — out of OmniVec control |
| Workload-identity federated credential | High | UAMI; rotated by AKS |
| Pipeline / model definitions | Medium | `omnivec.metadata` |
| Service Bus messages (blob URLs + IDs) | Medium | Service Bus |

## 5. What can go wrong (top 10 design threats)

Selected manually at boundary crossings. Risk rating uses the SDL scale: **Critical / Important / Moderate / Low / DefenseInDepth**. *Status*: ✅ shipped · ⚠️ partial · ❌ open.

| Id | Boundary | STRIDE | Threat | Risk | Status | Mitigation & residual |
|---|---|---|---|---|---|---|
| **T-API-1** | TB-1 → API | S/E/R | Static admin bearer token grants full admin; no rotation, no per-call audit | **Important** | ✅ | AAD bearer + group→role mapping; admin token now breakglass-only. Residual: rotation runbook needed. |
| **T-MET-1** | API/DocGrok → metadata | I/T | AOAI API keys stored cleartext in metadata Cosmos | **Important** | ✅ | AAD-only model records; legacy keys purged. Residual: legacy fallback path still readable. |
| **T-PWK-1** | TB-4 → DocGrok | D/E | Malicious customer document crashes parser or escapes via Pillow/PyMuPDF CVE | **Important** | ⚠️ | Subprocess sandbox with `RLIMIT_*` behind `DOCGROK_PARSER_SANDBOX=1`. Residual: seccomp-bpf not enforced; flag default-off. |
| **T-CON-2** | TB-4 → Ingestion (SSRF) | T/I | Customer attachment URL points at attacker storage account | **Important** | ✅ | `attachment_blob_account_allowlist` mandatory; URL host pinned. Residual: relies on operator config. |
| **T-CON-1** | TB-2 (Ingestion lease) | T/D | Change-feed lease container shares Cosmos DB with metadata; cross-write can DoS | **Moderate** | ✅ | Optional dedicated lease account. Residual: not enabled by default. |
| **T-VEC-1** | TB-4 (data at rest) | I | Vectors are PII-derived, partially invertible (embedding-inversion); residency obligations apply | **Moderate** | ✅ | PII classification documented; `DELETE /api/sources/{id}/vectors` cascade-purge. Residual: must propagate to data agreements. |
| **T-RL-1** | TB-3 (DocGrok → AOAI) | D | One pipeline saturates AOAI tier RPM → 429 cascade starves others | **Moderate** | ✅ | Per-deployment embed semaphore + jittered backoff. Residual: no circuit-breaker. |
| **T-SRCH-1** | TB-1 → API (search) | I/T | Search endpoint was exposed via plain-HTTP `LoadBalancer` | **Important** | ✅ | Default `ClusterIP`; new `searchIngress` template provides TLS-terminating ingress. |
| **T-SRCH-2** | TB-1 → Search (service-app path, Scenario D) | S/E/R | Search-scope bearer tokens are long-lived OmniVec-issued opaque tokens (not AAD); minted via `POST /api/auth/tokens` and stored hashed in Cosmos. No automatic rotation, no per-call audit beyond AppInsights, and the bootstrap `OMNIVEC_SEARCH_TOKEN` env value never expires. A leaked token grants read-only search until manually revoked. | **Important** | ⚠️ | Hashed-at-rest storage; admin-scope tokens rejected by default; rate limit on `searchIngress`; `searchIngress.enabled=false` by default so customers opt-in. Residual: rotation runbook + optional AAD-only mode (drop opaque tokens entirely) — see §6. |
| **T-NET-1** | TB-2 (in-cluster) | S/T/I | No NetworkPolicy / mTLS — compromised pod can call any service unauth | **Moderate** | ✅ | Default-deny + per-component allow rules behind `networkPolicy.enabled`. Residual: mTLS / mesh roadmap. |
| **T-SUP-1** | Pre-cluster (image source) | T | Compromised registry / MITM swaps an OmniVec image | **Moderate** | ⚠️ | Cosign keyless signing on every push. Residual: admission verification (Ratify/Kyverno) not enforced by default. |
| **T-TLS-1** | TB-1 → API (transport) | I/T | TLS floor not enforced in application code; downgrade to TLS 1.0/1.1 possible if ingress / OS misconfigured | **Moderate** | ⚠️ | Diagrams continue to show `HTTPS`; **customer responsibility** to configure ingress (NGINX `ssl-protocols`, AGIC `appgw-ssl-policy`, Traefik `minVersion`) to require TLS ≥ 1.2. Restated in §0.3 + §0.6.2; tracked in §6 backlog. |
| **T-FUZ-1** | TB-1 → API; TB-4 → DocGrok; ingestion parser sandbox | T/D/I | Service team **does not demonstrate the presence of systematic external or continuous Fuzz Testing across all exposed entry points** (Bug 123354 / TMP review booksql114276). API request handlers, DocGrok router/parser, and the document parser sandbox have not been fuzz-tested in CI. Latent input-handling defects (malformed JSON shapes, malformed PDFs / Office docs / images, oversized inputs) could escape unit tests and — in the worst case — enable RCE, DoS, or memory-disclosure attacks against customer-facing endpoints. SDL requires that issues identified by fuzz testing be fixed (per *Issues Identified by Fuzz Testing must be fixed — Liquid*). | **Moderate** | ❌ | Backlog: (1) contact the **Fuzz Testing v-team led by Sharath Unni** to scope the options available to the service; (2) evaluate **[OneFuzz](https://aka.ms/onefuzz)** as the primary continuous-fuzz platform, falling back to *General \| Fuzzing @ Microsoft* tooling if not viable; (3) stand up harnesses for the externally exposed entry points first (API route handlers in `api/api.py`, the `docgrok-router` parsing path, and the `worker.py::_pdf_subprocess_target` parser sandbox); (4) **integrate fuzz runs with the build pipeline** (PR-gating smoke + nightly long-runs) so coverage is continuous, not one-off; (5) wire findings into the existing SDL bug-tracking workflow. Tracked at Bug **123354** (TMP review **booksql114276**); SLA = next 6-month review. |

> The CI/CD pipeline itself (the GitHub Actions runner that produces those signatures) has its own threat model in [`cicd-threat-model.md`](./cicd-threat-model.md).

## 6. What we did about it (mitigation backlog)

Open items only — closed items are the ✅ rows above.

| Threat | Action | Owner | ETA |
|---|---|---|---|
| T-PWK-1 | Switch `DOCGROK_PARSER_SANDBOX` default to `1`; ship seccomp-bpf profile and require it via PSA `restricted` | DocGrok / OmniVec | next batch |
| T-SRCH-2 | Document search-token rotation runbook; add optional `SEARCH_AAD_ONLY` mode that disables opaque-token auth and requires AAD JWT with `scope=search` claim on the `searchIngress` path | OmniVec | follow-up |
| T-SUP-1 | Add Ratify or Kyverno admission-controller chart that requires cosign signature on every OmniVec image | OmniVec | follow-up |
| T-API-1 (residual) | Document admin-token rotation runbook + deletion-after-AAD-cutover policy | OmniVec | follow-up |
| T-NET-1 (residual) | mTLS or service-mesh between tiers (defence in depth on top of NetworkPolicy) | OmniVec | follow-up |
| T-TLS-1 | Document explicit TLS-floor configuration snippets per ingress (NGINX / AGIC / Traefik) in the operator bring-up checklist; consider shipping default Helm values with TLS 1.2+ enforcement annotations preset | OmniVec | next-batch |
| T-FUZ-1 (Bug 123354 · TMP review booksql114276) | Engage **Sharath Unni's Fuzz Testing v-team** to scope options; adopt **[OneFuzz](https://aka.ms/onefuzz)** (or alternative from *General \| Fuzzing @ Microsoft*); stand up harnesses for customer-exposed entry points first — `api/api.py` route handlers, `docgrok-router` parsing path, parser-sandbox PDF/Office/image corpus; **integrate continuous fuzz runs into the build pipeline** (PR-gating smoke + nightly long-runs); wire findings to SDL bug workflow; land before next 6-month review | OmniVec / DocGrok / Security | next 6-month review |

## 7. Did we do enough? (review log)

| Date | Reviewer | Outcome | Notes |
|---|---|---|---|
| 2026-05-06 | Internal | Initial STRIDE-per-element pass | See `threat-model.md.bak` (pre-DPSS-refactor structure) |
| 2026-05-11 | Internal (DPSS-style refactor) | Trimmed to 10 boundary threats; collapsed DFD; surfaced T-SRCH-1, T-NET-1, T-SUP-1 from inter-component audit | Ready for SQL Security Review Board submission |
| 2026-05-11 | Internal (T-SRCH-1, T-NET-1 closure) | Search default switched to `ClusterIP` + new `searchIngress` template; `templates/networkpolicy.yaml` adds default-deny + per-tier allow rules behind `networkPolicy.enabled` toggle | Both threats now ✅ |
| 2026-05-11 | Reviewer feedback (Curzi-style) | Restructured: added §0 *Threat Model Information* (deployment / identity / networking / customer assumptions), replaced single complex DFD with one ≤10-shape high-level view + 3 scenario diagrams (Search, Ingestion, Admin), two-line flow labels (`purpose` / `how secured`), authorization noted per scenario, external interactors marked `[ext]` with justifications, response flows omitted | Doc now matches DPSS readability guidance |
| 2026-05-12 | Reviewer follow-up (per-flow detail) | Added flow IDs (A1–A7, B1–B7, C1–C4) on every arrow; added a **Flow Details** table after each scenario with columns: Purpose, Transport, AuthN, AuthZ, Data on the wire, Mitigations (cross-referenced to §5 threats) | Reviewers can now read per-flow security measures without crowding the diagram |
| 2026-05-12 | Reviewer follow-up (terminology) | Dropped "untrusted input" framing on the customer data plane (the customer trusts their own data). Replaced with the actual concern: third-party-supplied document content + URL hosts must be parser-hardened and SSRF-guarded (links to T-PWK-1, T-CON-2). Updated diagrams, tables, and TM7 stencil text | Avoids implying customer data is hostile; keeps the concrete risk visible |
| 2026-05-12 | Reviewer audit (vague labels) | Replaced remaining vague flow labels (e.g., "in-cluster HTTP · NetworkPolicy", "embed call", "fetch attachment") with concrete labels naming the actual route/verb (e.g., `POST /v1/embed/batch (HTTP/1.1, in-cluster)`), the auth header (`X-Admin-Token`), the specific NetworkPolicy allow rule, and Azure RBAC role names. Tightened §2 Components-table column wording. Verified all 16 reviewer recommendations are met | Audit complete; no vague labels remain |
| 2026-05-12 | Reviewer follow-up (AAD scoping) | Marked **Azure AD** explicitly as `[ext, out of scope]` in all three scenario diagrams and the high-level view; added it to the §0.5 OOS table with reason ("Microsoft-operated identity provider; OmniVec only validates JWTs, does not run/secure/rotate AAD"); reworded TB-1 to state that AAD sits *outside* the boundary and only token validation is in scope; renamed TM7 trust boundary from "TB-1 Internet / AAD" to "TB-1 Internet (public HTTPS surface)" to remove the implication that AAD is enclosed | AAD now treated consistently with Foundry — shown because API calls it, OOS because Microsoft operates it |
| 2026-05-12 | Reviewer follow-up (AAD layout) | Visually separated AAD into its own **TB-1a Microsoft-operated identity (out of scope)** boundary in both the mermaid diagrams (high-level + Scenario A + Scenario C) and the TM7 file; Foundry/AOAI shown enclosed in a matching **"Customer Azure subscription — out of scope"** rectangle in the scenario diagrams. AAD shape now sits outside the TB-1 rectangle in TMT instead of inside it | Reviewers can no longer mistake AAD for an in-scope component of TB-1 |
| 2026-05-12 | Reviewer follow-up (terminology) | Replaced "WIF" with "Managed Identity (UAMI)" throughout the doc and TM7 to match how the team describes the mechanism. Same underlying setup (Azure AD Workload Identity binding a UAMI to the pod's ServiceAccount); the resource-level name is more discoverable for reviewers | No mechanism change |
| 2026-05-21 | External reviewer (post-batch-8 TM review) | New risk + 6 diagram improvements accepted (must land before next 6-month review): (1) **removed** the `Callers → Azure AD` OIDC sign-in flow from the high-level view and §3.2 — External Interactors are black boxes and we do not represent their internal workings; (2) **unified** the OmniVec ↔ customer-data-plane read+write into one bidirectional OmniVec-initiated arrow (was previously two directional artifacts); (3) **added** the local configuration source node (`envcfg` — env vars + Helm values) so the config path is visible in both the high-level view and §3.2 (new flow **U7**); (4) **split** the OmniVec server box into three distinct process components — *Web/API control plane*, *Event listener / Embedding processor*, *Telemetry/Logging* — each with its own credentials column; (5) kept `HTTPS` on diagrams but added §0.3 + §0.6.2 + new threat **T-TLS-1** documenting that **TLS protocol-floor enforcement is customer responsibility** (OmniVec does not pin TLS ≥ 1.2 in app code — operators configure the ingress / OS); (6) expanded §0.6 *Operator responsibilities* to surface non-obvious assumptions (TLS floor, env-var ownership, allowlists). Also catalogued **T-FUZ-1 (Bug 123354)** — fuzz testing of API + DocGrok parser surfaces is not yet automated; backlog entry added with 6-month SLA. TM7 regenerated via `python scripts/gen_threat_model_tm7.py`. | Approved with the six items + Bug 123354 due by next review cycle |
| 2026-05-12 | Reviewer follow-up (Scenario D — service-app search) | Added a missing flow: backend apps / scripts / partner systems can call Search **directly** via the `searchIngress` HTTPS endpoint, bypassing the API. Auth on that path is **not** AAD — it's an OmniVec-issued opaque bearer (scope=`search`) minted by `POST /api/auth/tokens` and stored SHA-256-hashed in `omnivec.metadata`. Added Scenario D diagram + flow-detail table (D0–D5), new threat **T-SRCH-2** (long-lived static search tokens; no automatic rotation; bootstrap token never expires), and a new external interactor "Service caller" in the TM7 with one flow into the API/Search surface | Programmatic / partner-app search path is now modeled distinctly from the browser path |

**To request a Threat Model Review:** upload `threat-model.tm7` + this `.md` via the Threat Modeling Portal ([aka.ms/dpgtrack](https://aka.ms/dpgtrack)). Per DPSS guidance, the review meeting is a 2–3 hour call; this document is sized to fit.

## 8. How to update this model

1. Edit this file. Diff is the source of truth; `.tm7` is the visualization.
2. If the architecture changes, update the high-level view in §2 *and* the affected scenario diagram in §3.
3. Regenerate `.tm7` with `python scripts/gen_threat_model_tm7.py`.
4. Add new boundary threats to §5 with id `T-XXX-N` and a risk rating from the SDL scale.
5. Update §7 review log on every formal review or material refactor.

---

## Appendix A — Detailed DFD (19 elements, archived)

> **Note:** This view is preserved for historical context only. The high-level view in §2 plus the 3 scenario diagrams in §3 are the **current** source for `.tm7`. Per reviewer feedback, the doc no longer relies on a single dense diagram.

```mermaid
flowchart LR
  subgraph "INTERNET / AAD trust boundary"
    user["End-user browser"]
    aad["Azure AD<br/>(login.microsoftonline.com)"]
  end

  subgraph "AKS cluster (omnivec namespace)"
    subgraph "Web / API tier"
      web["omnivec-web<br/>(Next.js)"]
      api["omnivec-api<br/>(FastAPI)"]
      search["omnivec-search<br/>(Go)"]
    end
    subgraph "DocGrok tier"
      router["docgrok-router<br/>(Rust)"]
      pworker["docgrok-pipeline-worker"]
      incluster["in-cluster embedders<br/>CLIP / BGE / DSE-Qwen2"]
    end
    subgraph "Ingestor tier (.NET)"
      ingestor["omnivec-ingestor (.NET)<br/>change-feed watcher"]
      dotnetworker["omnivec-dotnet-worker<br/>(Service Bus consumer)"]
    end
  end

  subgraph "Azure managed services"
    aoai["Azure OpenAI"]
    cmeta["CosmosDB<br/>omnivec.metadata"]
    kv["Azure Key Vault"]
    sb["Azure Service Bus"]
    appinsights["Azure Monitor<br/>App Insights"]
  end

  subgraph "Customer-owned (external trust)"
    csrc["Customer CosmosDB<br/>(source w/ attachments)"]
    bsrc["Customer Blob source"]
    cvec["Customer CosmosDB<br/>(vectors destination)"]
    blob["Customer Blob<br/>(attachments source)"]
  end

  user -->|HTTPS + Bearer token| web
  user -->|HTTPS + Bearer token| api
  api -->|JWKS fetch (cached)| aad
  web --> api
  web --> search
  api --> cmeta
  api --> kv
  api --> sb
  api --> router
  search --> cvec
  search -->|/embed (query)| router
  search --> cmeta
  router -->|API key OR AAD| aoai
  router --> incluster
  router --> cmeta
  router --> pworker
  pworker --> sb
  pworker --> bsrc
  pworker -->|fetch attachment binary| blob
  pworker --> router
  pworker --> cvec
  ingestor -->|change-feed read| csrc
  ingestor -->|fetch attachment binary| blob
  ingestor -->|enumerate (azure-blob source)| bsrc
  ingestor -->|pipeline / source config read| cmeta
  ingestor -->|enqueue work (queue mode)| sb
  ingestor -->|"/embed/batch (inline mode)"| router
  ingestor -->|vector patch (inline mode)| csrc
  dotnetworker -->|drain SB topic| sb
  dotnetworker -->|"/embed/batch (queue mode)"| router
  dotnetworker -->|vector write| cvec
  dotnetworker -->|model record read| cmeta
  api -->|telemetry| appinsights
  search -->|telemetry| appinsights
  ingestor -->|telemetry| appinsights
  dotnetworker -->|telemetry| appinsights
```

## Appendix B — Prior STRIDE-per-element analysis (archive)

The previous version of this document (kept at `threat-model.md.bak` in the repo
working tree, also visible in git history before commit replacing it) contained
exhaustive STRIDE rows per process, per data store, and per external interactor.
That format was flagged by reviewers as **too detailed and lacking clarity from
a security-design standpoint** — most rows duplicated SDL/CodeQL coverage
rather than calling out boundary risks specific to OmniVec.

The 10 threats in §5 are the curated subset of those rows that represent
*design risk that STRIDE-at-boundaries surfaces and SDL/CodeQL does not*. If you
need the historical per-element matrix for context, see `threat-model.md.bak`.
