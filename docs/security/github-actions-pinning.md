# GitHub Actions pinning: issue and remediation

**Repository:** AzureCosmosDB/OmniVec  
**Status as of November 16, 2026:** Resolved. [PR #213](https://github.com/AzureCosmosDB/OmniVec/pull/213) merged into `main` as [`56d2dd2`](https://github.com/AzureCosmosDB/OmniVec/commit/56d2dd23fc08f51da1172c5cabe021d2039bd557), and Dependabot [PR #214](https://github.com/AzureCosmosDB/OmniVec/pull/214) followed as [`bb79260`](https://github.com/AzureCosmosDB/OmniVec/commit/bb7926033cf8dcdb0a4b9e716df536cede93aa69). The push-triggered workflows passed on both commits, and `main` has no unpinned action references. Nine stale branches that still contained mutable references were deleted.

## What was the issue?

The 1ES policy for the AzureCosmosDB organization requires GitHub Actions to reference full-length, 40-character commit SHAs. Enforcement was announced for November 6, 2026.

References such as `actions/checkout@v4` and `dtolnay/rust-toolchain@stable` use mutable tags or branches. Their maintainers can move those references to different commits, changing the code a workflow executes without a repository change. Pinning to an exact commit prevents that reference from silently selecting different action code.

On current `main`, the remaining violation was in `.github/workflows/python-tests.yml`:

```yaml
- uses: dtolnay/rust-toolchain@stable
```

GitHub rejected the `Isolated mock source / model / destination` job during action preparation, before its tests could run. The [failing main-branch run](https://github.com/AzureCosmosDB/OmniVec/actions/runs/42719897177) reported:

```text
The action dtolnay/rust-toolchain@stable is not allowed in AzureCosmosDB/OmniVec because all actions must be pinned to a full-length commit SHA.
```

This was a workflow security-policy failure, not a failing Rust test or an application-code defect. The affected job failed and made the `python-tests` workflow fail; this does not mean every workflow in the repository was blocked.

## Why was this reference missed?

[PR #208](https://github.com/AzureCosmosDB/OmniVec/pull/208) pinned the tag-based references, such as `actions/checkout@v4`, but left `dtolnay/rust-toolchain@stable` unchanged. In that repository, `stable` is a branch, not a version tag. [PR #211](https://github.com/AzureCosmosDB/OmniVec/pull/211) later updated the pinned actions to newer versions but also did not change the branch reference.

| Date (UTC) | Event |
| --- | --- |
| November 2 | Scheduled CodeQL run on `main` used unpinned references. This was the only run that day, matching the date cited in the 1ES notice. |
| November 4, 17:50 | PR #208 merged. Tag references were pinned; `@stable` was not. `python-tests` still passed because enforcement was not yet active. |
| November 6 | 1ES enforcement began. |
| November 9, 23:22 | PR #211 merged. The resulting `main` `python-tests` run failed on `rust-toolchain@stable`. |
| November 11 | Dependabot [PR #212](https://github.com/AzureCosmosDB/OmniVec/pull/212) failed with the same error. |
| November 16, 04:20 | PR #213 opened; all checks passed. |
| November 16, 04:43 | PR #213 merged by an admin; required review bypassed. `python-tests`, CodeQL, and `build-and-push-images` passed on `main`. Dependabot replaced PR #212 with [PR #214](https://github.com/AzureCosmosDB/OmniVec/pull/214), which keeps every reference SHA-pinned. |
| November 16 | Nine stale remote branches that still had mutable references were deleted. Their work was already merged or closed through PRs. |
| November 16, 05:01 | PR #214 approved and merged normally, with no bypass. Its SHAs were checked against the upstream release tags. `python-tests`, CodeQL, and `build-and-push-images` passed on `bb79260`. |

## Why did the notice also list `actions/checkout` and `actions/setup-dotnet`?

On `main`, both have been pinned since PR #208 merged on November 4. Other branches keep their own copies of the workflow files, however. A scan of every remote branch (`git ls-remote --heads origin`, then each branch's `.github/workflows/`) found 12 branches besides `main` with mutable `uses:` lines. The notice does not identify which branch or run it scanned, so this document does not assume which branch caused it.

The local clone fetched only `main`, so its `origin/*` branch list was out of date and listed only a few of these branches. Audits must query the remote directly.

Deleted, because their work was already merged or closed through a PR. Each can be restored from its PR page with **Restore branch**:

| Branch | PR |
| --- | --- |
| `feat-cosmos-fts-search` | [#180](https://github.com/AzureCosmosDB/OmniVec/pull/180) merged |
| `feature/sharepoint-source` | [#183](https://github.com/AzureCosmosDB/OmniVec/pull/183) merged |
| `fix/helm-isolated-repository-cache` | [#187](https://github.com/AzureCosmosDB/OmniVec/pull/187) merged |
| `feature/sharepoint-cross-tenant` | [#188](https://github.com/AzureCosmosDB/OmniVec/pull/188) merged |
| `feat/actionable-permission-guidance` | [#198](https://github.com/AzureCosmosDB/OmniVec/pull/198) merged |
| `ui/redesign` | [#199](https://github.com/AzureCosmosDB/OmniVec/pull/199) merged |
| `fix/appinsights-appmetrics` | [#202](https://github.com/AzureCosmosDB/OmniVec/pull/202) closed |
| `fix/v1.2.2-readiness-and-contracts` | [#209](https://github.com/AzureCosmosDB/OmniVec/pull/209) merged |
| `feature/pipeline-capacity-and-docs` | [#210](https://github.com/AzureCosmosDB/OmniVec/pull/210) merged |

`feat/playground-fts-hybrid` ([#181](https://github.com/AzureCosmosDB/OmniVec/pull/181)) had already been deleted.

Kept, because they contain work that is not on `main` or need an owner decision:

| Branch | Reason |
| --- | --- |
| `dev` | It has no unique work missing from `main`, and it is not protected. Its workflows run on pushes to `dev`, so they still contain mutable references that policy would reject. |
| `feature/onelake-iceberg-garnet` | Unmerged work with no PR. |
| `prsasatt/parametric-card-extract` | Unmerged work with no PR. |

Before reusing a kept branch, update it from `main` so its workflows use the pinned references.

Similarly, a local working branch that predated PR #208 initially showed 40 mutable references. An early local correction pinned only 14 of them. The final remediation was therefore based on current `main`, which avoided downgrading the action versions from PR #211 and excluded unrelated local changes.

## How was it fixed?

[PR #213](https://github.com/AzureCosmosDB/OmniVec/pull/213), commit [`f0415101e3acc5d202cda5af7f02f38859c42055`](https://github.com/AzureCosmosDB/OmniVec/commit/f0415101e3acc5d202cda5af7f02f38859c42055), changes only the Rust action reference:

```diff
-      - uses: dtolnay/rust-toolchain@stable
+      - uses: dtolnay/rust-toolchain@39008827e6b4319a349e9a42dfc90579dd25f1ca # stable
```

The SHA was resolved from the upstream `stable` reference and confirmed to exist in the upstream repository. Its [action metadata](https://github.com/dtolnay/rust-toolchain/blob/39008827e6b4319a349e9a42dfc90579dd25f1ca/action.yml) defaults the `toolchain` input to `stable`, so the workflow retains its existing toolchain selection without additional inputs.

The trailing `# stable` is a readability comment, not an executable reference. GitHub selects the action using the SHA.

On the PR branch, all 40 direct action references across the five workflow files are full-length SHA pins. Existing action upgrades, triggers, permissions, build commands, and application code are unchanged.

## What confirms that this fixes the reported problem?

The evidence is not limited to inspecting the YAML. GitHub executed the corrected workflow on the PR's fix commit:

| Evidence | Result |
| --- | --- |
| [Previously blocked mock-components job](https://github.com/AzureCosmosDB/OmniVec/actions/runs/43803501429/job/133088880758) | Passed, including the SHA-pinned Rust action, Helm contracts, .NET regressions, and both Rust test steps. |
| [Entire python-tests workflow](https://github.com/AzureCosmosDB/OmniVec/actions/runs/43803501429) | Passed all four jobs. |
| [CodeQL Security Scan](https://github.com/AzureCosmosDB/OmniVec/actions/runs/43803501564) | Passed. |
| [PR image builds](https://github.com/AzureCosmosDB/OmniVec/actions/runs/43803501424) | All nine image builds passed, including the three required checks for API, web, and changefeed. |

These results confirmed the fix on the PR branch before merge. Post-merge verification on `main`:

| Workflow on `56d2dd2` | Result |
| --- | --- |
| [python-tests](https://github.com/AzureCosmosDB/OmniVec/actions/runs/43805846054) | All four jobs passed, including the previously blocked mock-components job. |
| [CodeQL Security Scan](https://github.com/AzureCosmosDB/OmniVec/actions/runs/43805846007) | All four language analyses passed. |
| [build-and-push-images](https://github.com/AzureCosmosDB/OmniVec/actions/runs/43805846065) | All 11 image builds passed. |

## What remains?

The follow-ups from the original remediation are complete:

- Stale branches: the nine branches listed above were deleted.
- Dependabot [PR #214](https://github.com/AzureCosmosDB/OmniVec/pull/214) was reviewed and merged. It did not change the Rust action pin.
- `T-CI-2` in [cicd-threat-model.md](cicd-threat-model.md) is marked as mitigated.

Still open:

1. PR #213 was merged without an approving review, using the admin override, because its author could not approve it. A reviewer may optionally review it after the fact.
2. Reset `dev` to `main` or delete it, and decide whether it should be protected.
3. BinSkim: after this remediation, a manual BinSkim run on `main` failed for two reasons unrelated to pinning. Under setup-go v7, the hard-coded Go 1.22 could no longer upgrade itself to the Go version required by `cli/go.mod`. The unversioned BinSkim NuGet install also picked up a release that moved `BinSkim.exe`. [PR #215](https://github.com/AzureCosmosDB/OmniVec/pull/215) fixes both and adds the workflow file to its own path filters. Without that filter entry, PR #211's change to `binskim.yml` never ran BinSkim.

## Preventing recurrence

- Require full-length commit SHAs for remote action references in new or edited workflows, keeping version comments for readability.
- Review all action references, rather than only the names listed in an older notification.
- Base remediation on current `main` to preserve fixes and dependency upgrades already merged by others.
- Keep the existing weekly GitHub Actions Dependabot updates on `main`, and review updates before merging. Neither PR #208 nor Dependabot PR #211 pinned the branch reference, so do not rely on automation alone to detect every mutable branch reference.
- Keep this pin on a commit from the action's `stable` branch. Commits on its `master` branch require the `toolchain` input and define no default. If the pin is ever moved to a `master` commit, add `with: toolchain: stable`; otherwise, the job fails with `'toolchain' is a required input`.
- Treat compiler and tool versions separately: pinning this action freezes its action code, but its `stable` input still selects the stable Rust toolchain. This remediation is not a claim that every tool or downloaded dependency is immutable.
- Pin tools that workflows download at runtime, such as the BinSkim NuGet package. Unpinned tools can change without a repository change in the same way mutable action references can.
- Audit every branch, not only `main`. Use `git ls-remote --heads origin` because a clone's `origin/*` refs can be out of date, and delete branches after their PRs merge.
