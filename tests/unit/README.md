# OmniVec unit / regression-trap test suite

This directory holds the **fast, hermetic, no-Azure** unit test suite. It is
designed as a regression trap: each test family catches a specific class of
silent regression we have hit (or want to never hit) in this repo.

## What's covered

| File | What it catches |
|------|-----------------|
| `test_log_filters.py` | Anyone removing or weakening the CR/LF/control-char scrubber or the bearer/api-key redaction in `api/api.py`, `search/main.py`, or `docgrok/api.py`. ~18 property tests, ~1,800 generated assertions in fast mode. |
| `test_openapi_snapshots.py` | Route additions/removals/renames in any of the three FastAPI apps. Combines a stable per-route summary snapshot, a full-schema snapshot, and explicit paranoia checks for known-critical paths. |
| `test_tm7_generator.py` | Drift in `scripts/gen_threat_model_tm7.py` — element counts, kinds (process/external/store), out-of-scope flags, and flow direction tuples per diagram. End-to-end XML render is also asserted. |
| `test_tm7_xml.py` | Structural correctness invariants over every `.tm7` in `docs/security/` — external interactors cannot be Out-Of-Scope, flows cannot connect two external interactors, and every flow endpoint must reference a real element. |
| `test_models.py` | Round-trip serialization + validation for every Pydantic model in `api/models.py`, plus exhaustive enum value assertions to catch silent renames. |

## Running locally

```powershell
# from repo root
pip install -r tests\unit\requirements.txt
pytest tests\unit -v
```

Typical wall-clock time: **~30 seconds** on a dev laptop, all hermetic.

## Hypothesis modes

The suite registers two Hypothesis profiles in `conftest.py`:

* **fast** (default) — 100 examples per `@given`.
* **thorough** — 1,000 examples per `@given`. Enable with:

```powershell
$env:HYPOTHESIS_THOROUGH = "1"
pytest tests\unit
```

Run the thorough profile in CI nightly or before a security-sensitive
release. Local dev should leave it off.

## Updating snapshots

We use [`syrupy`](https://github.com/syrupy-project/syrupy). When you
intentionally add/remove/rename a route, regenerate the snapshots:

```powershell
pytest tests\unit\test_openapi_snapshots.py --snapshot-update
git add tests\unit\__snapshots__\
git commit -m "tests: refresh OpenAPI snapshots for <reason>"
```

The paranoia checks (`test_required_route_exists`) survive snapshot
regeneration — they're hard-coded route lists that must always pass.

## Adding new tests

1. Drop a new `test_*.py` next to the existing ones. They are auto-discovered.
2. If you need a FastAPI app, use the `api_app` / `search_app` / `docgrok_app`
   fixtures from `conftest.py` — they handle env vars and Azure client stubs.
3. Property tests are preferred over example tests when input space is
   open-ended (text, integers, etc.). Use `hypothesis.strategies`.
4. **Never** add tests that hit Cosmos, Azure OpenAI, or any network host.
   If you need to test integration behavior, that belongs in a separate
   integration tier (not yet present here).

## CI

`.github/workflows/python-tests.yml` runs this suite on every push and PR
against `main` using Python 3.11 on `ubuntu-latest`. JUnit XML is uploaded
as a workflow artifact on failure for triage.
