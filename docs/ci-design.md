# Shared CI: design and usage

Interfaces, data flow and the trust boundary for the workflows in this
repository. Kept out of `README.md` so the repository front page stays plain;
this file is the reference for callers inside the organization.

## Why this repository is public

GitHub will not resolve a `uses:` from a **public** repository into a **private**
one. That holds even when the private repository's Actions access is set to
"accessible from repositories in the organization" — which `maxi-config`'s
already is. The run fails at workflow parse time: no jobs are created, no log is
produced, and the only signal is a check-run annotation reading
*"This run likely failed because of a workflow file issue."*

Measured before this repository existed: **720 runs across the four public
consumers, every one `failure`, every one producing zero jobs**, while the
identical wrapper succeeded in private consumers. Visibility was the only
variable.

So the workflows live here, in public, where any consumer can resolve them.

## What is here, and what is deliberately not

This repository holds **mechanism**. It holds no policy, no inventory and no
credentials.

```
.github/workflows/
  rust-ci.yml                  lane composition (workflow_call only)
  lane-plan.yml                routing + packaging-impact router
  lane-check.yml               fmt + clippy
  lane-test.yml                cargo test + repo-local ci/lane-test.sh
  lane-package.yml             release build + artifact
  lane-sign-publish.yml        trusted release refs only
  lane-release-verify.yml      trusted release refs only
  review-gate-reusable.yml     unresolved threads + non-author review
.github/actions/
  fetch-policy/                pulls policy files over the Contents API
  collect-pr-review-state/     two paginated GraphQL reads
  pr-review-gate/              the verdict logic and its truth table
```

Everything these workflows *decide with* stays in the private `maxi-config`
repository and is read **at run time**, authenticated by the caller's App token:

| Data | Read by | How |
| --- | --- | --- |
| `ci/runner-routing.toml` | `resolve-runner` | sparse checkout in `lane-plan` |
| `ci/packaging-paths.toml` | `plan-packaging-impact.sh` | sparse checkout in `lane-plan` |
| `scripts/select-rust-toolchain.sh` | check / test / package | `fetch-policy`, Contents API |
| `scripts/emit-runner-paths.sh` | check / test / package | `fetch-policy`, Contents API |
| `hooks/clippy-strict-lints.sh` | `lane-check` | `fetch-policy`, Contents API |

`resolve-runner` itself also stays private, because `lane-plan` checks it out
alongside the policy it reads rather than referencing it as an action.

None of the routing data is embedded in this repository, and none of it is
reachable without a token that already has read access to it.

## Consuming these

```yaml
jobs:
  merge-gate:
    uses: maxi-tools/ci/.github/workflows/rust-ci.yml@main
    permissions:
      contents: write
      pull-requests: read
      actions: read
      id-token: write
      pages: write
    with:
      repo_policy: standard-rust
    secrets:
      APP_ID: ${{ secrets.APP_ID }}
      APP_PRIVATE_KEY: ${{ secrets.APP_PRIVATE_KEY }}
```

**Name the secrets; do not use `secrets: inherit`.** `inherit` forwards every
organization secret the calling repository can see to a workflow whose source is
world-readable. These workflows declare two secrets and use two.

The review gate takes no secrets at all — it runs on the caller's `GITHUB_TOKEN`:

```yaml
jobs:
  review-gate:
    uses: maxi-tools/ci/.github/workflows/review-gate-reusable.yml@main
    permissions:
      actions: read
      contents: read
      pull-requests: read
      statuses: write
```

`statuses: write` has to be granted by the caller. A reusable workflow cannot
elevate the token it is handed.

## Trust boundary

The source being readable is not what makes any of this safe. The gates here are
identity gates, and they are stated plainly rather than hidden:

- The lane chain runs for **same-repository** pull requests only.
- `workflow_dispatch` is pinned to the owner **by numeric user id**, because
  logins can be reassigned and ids cannot.
- The fan-out bypass in `pr-review-gate` requires the branch prefix **and** the
  App identity that opens those branches. The prefix alone is not sufficient,
  because a head ref is attacker-chosen.
- Fork pull requests reaching self-hosted runners are governed at organization
  level, not by any `if:` in these files.

Write access to this repository is equivalent to code execution in every
consumer, so it is protected accordingly: reviewed pull requests only, no
force-push, no branch deletion.
