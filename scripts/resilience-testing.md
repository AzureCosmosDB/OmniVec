# Chaos and scale testing

OmniVec reliability testing is maintained as repository tooling, not as
unrecorded one-off commands.

## Recovery chaos

`recovery-chaos.py` is the fail-closed Kubernetes recovery harness. Its default
plan mode performs no external calls, and live execution is restricted to its
reviewed environment allowlist. See `recovery-chaos.md` for prerequisites,
watchdog behavior, data-integrity checks, and cleanup guarantees.

```powershell
python scripts\recovery-chaos.py
python -m pytest tests\unit\test_recovery_chaos.py tests\unit\test_recovery_chaos_probes.py -q
```

## HTTP scale profiles

`scale-load.py` runs versioned warmup, load, spike, and soak profiles against an
explicitly allowlisted HTTP endpoint. It uses bounded concurrency and timeouts,
loads sensitive headers only from environment variables, never writes header
values to reports, and evaluates error-rate, request-count, and p95 thresholds.

Plan mode is the default and performs no network calls:

```powershell
python scripts\scale-load.py --config scripts\scale-load-example.json
```

Live execution requires an exact confirmation and a new report file:

```powershell
$env:OMNIVEC_SCALE_TOKEN = "Bearer <token>"
python scripts\scale-load.py `
  --config scripts\scale-load-search.json `
  --execute `
  --confirm OMNIVEC-SCALE-LOAD-AUTHORIZED `
  --report scale-load-search-UNIQUE.json
```

Commit environment-specific profiles only when their host and target data are
dedicated to testing. Profiles must not contain credentials.

## Expansion matrix

The maintained suite should grow through reviewed scenarios in this order:

1. Search API load, spike, and soak profiles with latency/error thresholds.
2. Service Bus backlog growth while workers scale down and recover.
3. Pod deletion and rolling restarts during steady ingestion load.
4. Node drain and node-pool scale-out/scale-in with an independent watchdog.
5. Garnet restart, unavailable endpoint, memory pressure, and persistence loss.
6. Duplicate, delayed, and out-of-order source operations with version fencing.
7. Fabric throttling, job rejection, delayed completion, and stale-job races.
8. OneLake checkpoint loss, retry, empty-content deletion, and backfill recovery.

Every live scenario must have a default zero-call plan, exact target allowlist,
explicit authorization, bounded deadlines, independent restoration, structured
evidence, and ownership-checked cleanup.
