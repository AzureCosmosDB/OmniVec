# OmniVec service emulator

Stateful in-process fakes of **az**, **azd**, **kubectl**, **helm** and **docker**
that let the real `hooks/preprovision.sh` + `hooks/postprovision.sh` run
end-to-end in seconds — **no Azure subscription, no AKS cluster, no
docker daemon required**.

## Why

Previously we had canned-response stubs under `tests/hooks/mocks/` that were
"good enough" for preprovision unit tests but had no persistent state. The
postprovision path (`helm upgrade` → `kubectl get pods` → `helm status`)
needed a real cluster to exercise. That made regression testing painful.

This emulator:

- **Persists state** across commands in `$OMNIVEC_EMU_STATE` (resource
  groups, ACR images, helm releases, Kubernetes deployments/pods).
- **Synthesises realistic cluster state** when `helm upgrade` runs —
  subsequent `kubectl get deploy/pods/svc` returns ready workloads.
- **Supports fault injection** (delay, transient, hard-fail) via env vars
  so we can unit-test retry/skip/heartbeat logic deterministically.

## Quick start

```sh
chmod +x tests/emu/bin/* tests/emu/run-azd-up.sh
tests/emu/run-azd-up.sh       # runs preprovision + postprovision end-to-end
```

Output ends with something like:

```
== Done (rc=0) ==
State dir: /tmp/omnivec-emu.XXXX
Event log: /tmp/omnivec-emu.XXXX/events.log
```

Inspect what the hooks actually called:

```sh
cat $OMNIVEC_EMU_STATE/events.log
ls  $OMNIVEC_EMU_STATE/k8s/ns/omnivec/deployments
cat $OMNIVEC_EMU_STATE/helm/releases/omnivec
```

## Fault injection

All flags are **env vars**; set them before invoking `run-azd-up.sh` (or
directly before the hook under test).

| Env var                            | Behaviour                                                                |
|------------------------------------|--------------------------------------------------------------------------|
| `OMNIVEC_EMU_MODE=success`         | Default — everything succeeds.                                          |
| `OMNIVEC_EMU_MODE=slow`            | Every emulated call sleeps `OMNIVEC_EMU_SLOW_SECS` (default 2).         |
| `OMNIVEC_EMU_MODE=transient`       | `helm upgrade` fails the first `OMNIVEC_EMU_TRANSIENT_N` (def 2) times. |
| `OMNIVEC_EMU_MODE=fail`            | `helm upgrade/install` fails immediately with a non-transient error.    |
| `OMNIVEC_EMU_FAIL_CMD=<regex>`     | Match argv → fail with injected non-transient error.                    |
| `OMNIVEC_EMU_TRANSIENT_CMD=<re>:N` | Match → fail first N invocations with 503 Service Unavailable.          |
| `OMNIVEC_EMU_DELAY_CMD=<re>:SECS`  | Match → sleep SECS before responding.                                   |

Regex matches against the full `binary args...` string, e.g.
`"az acr import"`, `"helm upgrade"`, `"kubectl rollout"`.

### Examples

```sh
# Simulate a flaky helm (fails twice, succeeds on 3rd)
OMNIVEC_EMU_TRANSIENT_CMD='helm upgrade:2' tests/emu/run-azd-up.sh

# Simulate a permanent helm failure
OMNIVEC_EMU_FAIL_CMD='helm upgrade' tests/emu/run-azd-up.sh

# Simulate slow ACR import (5s per call)
OMNIVEC_EMU_DELAY_CMD='az acr import:5' tests/emu/run-azd-up.sh

# Combine: transient helm + slow ACR
OMNIVEC_EMU_TRANSIENT_CMD='helm upgrade:1' \
OMNIVEC_EMU_DELAY_CMD='az acr import:2' \
  tests/emu/run-azd-up.sh
```

## State layout

```
$OMNIVEC_EMU_STATE/
  events.log                        # chronological command log (epoch\tbin\targv)
  fault/                            # per-pattern invocation counters
  azd-env/<KEY>                     # azd env vars
  arm/
    groups/<rg>                     # resource groups
    acr/<name>/images/<repo>_<tag>  # ACR images (built or imported)
  docker/
    images/<tag>                    # local docker images
    registry/<tag>                  # pushed images
  k8s/ns/<namespace>/
    _meta                           # namespace metadata
    deployments/<name>              # key=value deployment spec + status
    pods/<name>                     # pod spec + status
    services/<name>
    secrets/<name>
    events/warnings                 # Warning events surfaced by kubectl
  helm/releases/<name>              # helm release metadata
```

## Tests

- `tests/hooks/test-emu-e2e.sh` — happy-path: full azd up completes, helm
  release recorded, 7 deployments synthesised, event log populated.
- `tests/hooks/test-emu-faults.sh` — fault scenarios: transient recovery,
  hard failure halt, delay absorption, idempotent rerun (skip-helm).

Run manually:
```sh
bash tests/hooks/test-emu-e2e.sh
bash tests/hooks/test-emu-faults.sh
```

Or as part of the normal suite:
```sh
bash tests/run.sh --shell bash
```

## Limitations

- **Not a real Kubernetes API.** JSONPath support is limited to the
  specific patterns the hooks use (availableReplicas, readyReplicas,
  LoadBalancer external IP, Warning events). Extending is straightforward
  — add a case in `tests/emu/bin/kubectl` `_handle_jsonpath`.
- **Bicep is not executed.** `run-azd-up.sh` seeds the ARM resources Bicep
  would create (RG, ACR, AKS, Cosmos, Key Vault, Storage, Service Bus).
  If you change Bicep outputs consumed by postprovision, also update
  `run-azd-up.sh`.
- **No admission webhooks / CRDs.** Helm upgrade always "succeeds"
  structurally; fault injection is the way to simulate failures.
