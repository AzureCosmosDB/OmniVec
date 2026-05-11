# OmniVec CI/CD threat model

> Status: **draft**, batch 8 (closes RES-5).
> Scope: `.github/workflows/`, `infra/`, `terraform/`, ACR / AKS deploy
> path. Out of scope: data-plane threat model (see
> `threat-model.md` + `threat-model-review-2026-05.md`).

## 1. Assets

| Asset | Sensitivity | Where it lives |
|---|---|---|
| Source code | Public (the repo is open) | github.com/AzureCosmosDB/OmniVec |
| Container images | Integrity-critical | `omnivecregistry.azurecr.io/*` |
| ACR push token (`secrets.ACR_USERNAME` / `ACR_PASSWORD`) | High | GitHub Actions secret |
| DocGrok submodule SSH key (`secrets.DOCGROK_DEPLOY_KEY`) | High | GitHub Actions secret |
| Cosign OIDC identity | Integrity-critical | GitHub OIDC (no static secret) |
| Helm release credentials (kubeconfig) | High | Stored on the deploy runner / Azure DevOps service connection |
| Terraform state | High | Azure Storage backend |

## 2. Trust boundaries

```
┌──────────────┐  PR/push  ┌──────────────┐ image push ┌──────────┐ helm
│ developer    │──────────▶│ GitHub       │───────────▶│ ACR      │─────▶ AKS
│ workstation  │           │ Actions      │            │          │
└──────────────┘           └──────────────┘            └──────────┘
                                  ▲
                                  │ OIDC
                                  ▼
                          Sigstore Fulcio + Rekor (cosign)
```

Crossings:
1. Developer → GitHub: SSH keys + 2FA + branch protection.
2. GitHub Actions → ACR: scope-map token (push-only, repo-scoped).
3. GitHub Actions → Sigstore: short-lived OIDC token, no static secret.
4. ACR → AKS: AKS-attached managed identity for image pull.
5. AKS admission → ACR signatures: enforced by sigstore
   policy-controller (RES-3, batch 8).

## 3. Threats (STRIDE)

### T-CI-1 (Med) — Malicious PR injects a step into a workflow
A pull request from a forked branch could modify `.github/workflows/*`
and execute attacker-controlled code with workflow permissions.

**Mitigations**:
* `pull_request` triggers on this repo only fire `permissions: read-all`
  by default; secrets are NOT exposed.
* `pull_request_target` is **not** used for any production-pushing
  workflow. (Verify before adopting it for any future workflow.)
* Branch protection on `main` / `dev` requires PR review + green CI
  before merge.

**Residual**: A reviewer who blindly approves a malicious workflow diff
remains the weakest link — same as any human-review-driven security
control. Mitigated organisationally by the two-reviewer rule on the
default branch.

### T-CI-2 (High) — Third-party action takeover
`docker/build-push-action`, `actions/checkout`, `sigstore/cosign-installer`
etc. are pinned to **major version** (`@v5`, `@v3`). If an upstream
maintainer is compromised, a tag re-point can land on every subsequent
build.

**Mitigation TODO** (next batch): pin to **commit SHA**, e.g.
`docker/build-push-action@4f58ea79222b3b9dc2c8bbdd6debcef730109a75`.
Renovate / dependabot can auto-PR digest bumps.

**Residual**: Until the SHA pin lands, a re-tag attack is possible.
Tracked as `T-CI-2` in the review doc backlog.

### T-CI-3 (Med) — Secret exfiltration via build script
A Dockerfile or script invoked by the build can print secrets into the
build log if `set -x` is enabled.

**Mitigations**:
* GitHub redacts known-secret values from logs.
* No `pull_request` workflow has access to push secrets (T-CI-1
  defence-in-depth).
* Cosign signing replaces the static ACR push token with short-lived
  OIDC for the *signing* path — the push token is still required for
  the build.

**Residual**: A malicious script in the source tree could base64-encode
secrets and embed them in image layers; cosign signs the resulting
image, so the leak is "signed" but verifiable. Mitigated by code
review.

### T-CI-4 (Low) — ACR scope-map token rotation
The `secrets.ACR_PASSWORD` is a long-lived ACR scope-map token. If it
leaks, it grants push for the lifetime of the token.

**Mitigation**: rotate quarterly via the platform-team runbook.
**Future**: Workload Identity Federation between GitHub OIDC and ACR
(removes the long-lived secret entirely). Tracked separately as
`T-CI-5` (post-batch-8 backlog).

### T-CI-5 (Med) — AKS deploy uses static kubeconfig
Helm installs run from a CI/CD runner that authenticates with a
kubeconfig stored as a service connection.

**Mitigation TODO** (next batch): switch to AKS-attached managed
identity using `Azure/login` + `Azure/aks-set-context` with OIDC
federated credentials.

**Residual**: Compromise of the runner = cluster admin. Mitigated by
network segregation (self-hosted runners in isolated VNet) where
applicable.

### T-CI-6 (Low) — Branch protection bypass
`main` / `dev` are protected, but **administrators** can override.

**Mitigations**:
* Two-person admin rule on the GitHub org.
* Audit log review (centralised in the org's SIEM).

## 4. Controls in place (batch-8 status)

| Control | Status |
|---|---|
| Branch protection on `main` / `dev` | ✅ |
| `permissions:` block on every workflow | ✅ |
| Cosign keyless signing on every image | ✅ batch 8 (`build-images.yml`) |
| Cosign verification at admission (opt-in) | ✅ batch 8 (`templates/cosign-policy.yaml`) |
| BinSkim scan on Rust binaries | ✅ pre-batch |
| CodeQL scan on Python + C# | ✅ pre-batch |
| SBOM generation | ❌ — backlog (T-CI-7) |
| SHA-pinned third-party actions | ❌ — backlog (T-CI-2) |
| ACR via Workload Identity Federation | ❌ — backlog (T-CI-4 follow-up) |
| AKS deploy via OIDC federated identity | ❌ — backlog (T-CI-5) |

## 5. Backlog

Tracked in this doc; promote to the main `threat-model-review-*.md`
backlog at the start of each quarterly review.

1. **T-CI-2**: SHA-pin all third-party actions; auto-PR digest bumps.
2. **T-CI-4 follow-up**: replace ACR push secret with WIF.
3. **T-CI-5**: AKS deploy via OIDC federated identity (kill kubeconfig).
4. **T-CI-7**: SBOM (CycloneDX) generation + attestation alongside
   cosign signatures (`cosign attest --predicate sbom.json`).
5. **CodeQL coverage extension**: add JavaScript (web/) and HCL
   (terraform/) scanners.

## 6. Verification

| Check | Status |
|---|---|
| All `.github/workflows/` reviewed | ✅ batch 8 |
| Cosign signing dry-run on dev branch | ⏳ pending first push after merge |
| Policy-controller installation runbook drafted | ⏳ ops follow-up |
