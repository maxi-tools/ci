# Pinning first-party reusable workflows

```toml
subject = "every `uses: maxi-tools/ci/.github/workflows/*.yml@...` reference from a consumer repository"
enforced_by = ["self-check / Check `uses:` references", ".github/workflows/fanout-ci-pin.yml"]
summary = "Pin to a 40-character sha. Advance on every merge to ci main via fanout-ci-pin.yml. Reject `@main`, `@v1`, branches, and tags."
```

## The rule

Every cross-repository reference into `maxi-tools/ci` is pinned to a 40-character
sha. Branch refs (`@main`), tags (`@v1`), and unpinned references are rejected.

## Why

`maxi-tools/ci` exists because **GitHub will not resolve a `uses:` from a
public repository into a private one**. 14 of the org's active repositories
are public, so the shared workflows live here, and for a consumer on the
mirror path that mirror -- not this repository -- is what executes.

`tests/test_public_ci_copy_agrees.py` in `maxi-config` proves the public
mirror is byte-equal to this tree at the pinned sha. The pin is what makes
that proof testable. `@main` would let the mirror diverge silently and the
fork-guard (`tests/test_self_hosted_fork_guard.py`) reads the bytes that
*will* run, not the bytes this tree *intends* to run. See
`maxi-config/docs/contracts/mirror-pin-is-a-security-boundary.md` for the
full argument, including the 14-repo measurement.

## What the pin costs, and how this contract pays for it

A sha pin means **a fix merged here is invisible until consumers advance**.

Measured 2026-09-20 on the dedupe-action change (ci#18 / ci#19): the
composite action landed and fixed two HIGH bugs in the helper that crashed
every run, but **every consumer was pinned at a sha predating the
composite-action introduction** (ci#9, `2eb4d94f`), so the action was
never called. The fix was merged-and-inert; no signal existed that it was
inert. (`maxi-core` ran at the older `a1287f8b` for this PR; `maxi-config`
and others ran at `44793c2b`; six consumer repos ran at `d9818097`.) The
fan-out that would have moved all of them to the new tip ran **days late
and by hand**.

The pin is correct; the fan-out was the missing mechanism. This contract
fixes that.

## Fan-out is the inert-detector

A merged PR on ci main triggers `.github/workflows/fanout-ci-pin.yml`. That
workflow:

1. Reads the new tip sha.
2. Lists consumers (the org repos whose `.github/workflows/review-gate.yml`
   names `maxi-tools/ci/...@<sha>`).
3. For each consumer whose pinned sha is older than the new tip, opens a
   PR titled `ci: advance pin to <tip>`.
4. Posts a job summary listing every consumer for which a PR was opened.

The fan-out PR is the **detector** for merged-but-inert: the PR exists on
the consumer the moment ci merges, and stays open until the consumer
merges it (or closes it with a reason). A consumer that does not merge the
fan-out PR within the contract's grace window is **silently broken** in
exactly the way ci#18 / ci#19 were silently broken -- and there is now an
open PR on that consumer recording the gap, with its `created_at` as the
age. `fanout-ci-pin.yml` re-runs on a weekly cron; if a fan-out PR is
still open a week later, the workflow reopens it (no-op if already open)
and posts a stale-PR comment naming the PR number and age.

`ci-pin-staleness-check.yml` is the backstop: a weekly scheduled CI that
reconciles the consumer-pin inventory against ci main without trying to
fan out (so a closed fleet token does not hide drift). It opens an issue
on this repository listing consumers that are two or more ci-tip-shas
behind, with the pin SHAs and the tip they should be at.

## Why not `@main`

A `@main` reference auto-advances the moment the new sha lands, so the
fan-out problem dissolves. But:

1. `tests/test_public_ci_copy_agrees.py` (in `maxi-config`) cannot test
   `maxi-tools/ci@main` against this tree. A consumer on the mirror path
   would execute `maxi-tools/ci`'s main, not this tree, and nothing would
   report the two diverging.
2. Fork-guard parity depends on the byte-identity proof above. Without
   the pin the proof is unreachable, and the fork guard degrades to
   "trust the source tree", which it cannot.
3. Reverting a broken ci main becomes a fleet-wide outage. `@main` is
   not revertible in a single action; the fleet-wide pin makes it so.

These are documented with measurements in
`maxi-config/docs/contracts/mirror-pin-is-a-security-boundary.md` and
are not relitigated here.

## Why not a moving `@v1` tag

Tags are mutable, and `actions/checkout` does not protect against a tag
being force-moved after the fact. A consumer that pinned `@v1` on
2026-09-19 and the org force-moved `v1` on 2026-09-20 would silently
execute the new bytes with the consumer's trust still attached to the
old ones. Tags add a moving target on top of the fork-guard problem
without removing it. They are rejected.

## Why not a bot that auto-merges fan-out PRs

A bot that auto-merges a fleet-wide pin advance would defeat the pin
entirely: any PR that lands on ci main would reach every consumer without
human review, and a malicious ci PR would compromise every consumer in
the same merge commit. The fan-out PR is the **review surface**, not an
obstacle to it. Each consumer's maintainer (or the reviewer the
consumer's `CODEOWNERS` names) sees the change in their own repo's PR
queue and approves it. The fan-out IS the human-in-the-loop detector.

## Known limits

1. **First-time consumer adoption.** A repo that does not yet have a
   `uses: maxi-tools/ci/...@...` reference cannot be fan-out'd into
   one. Adoption is a separate mechanism (maxi-config's `fanout-*`
   workflows and the `distribute-*.yml` set). This contract concerns
   the post-adoption advance.

2. **Fork pull requests.** The fan-out workflow runs with
   `permissions: contents: read, pull-requests: write` and is gated to
   `push` and `workflow_dispatch`; it never executes on a fork PR. The
   same gate that protects `review-gate-reusable.yml` protects this
   workflow.

3. **Mirror drift.** A consumer on the mirror path reads from
   `maxi-tools/ci`'s main, not from this tree. If the mirror lags the
   tip, the fan-out PR's "advance to <tip>" will silently fail to
   resolve at the consumer until the mirror catches up. The
   `ci-pin-staleness-check.yml` job summary names the lag.

4. **Consumer-side opt-out.** A consumer may close the fan-out PR with
   the comment `pin: skip` and a reason, and the next fan-out run
   honours that opt-out for that consumer. The opt-out is recorded in
   `.github/scripts/fanout_ci_pin.py:opt_outs` so a re-introduction is
   a deliberate change, not an accident.

## Operational state measured 2026-09-20 (post-merge)

`fanout-ci-pin.yml` ran cleanly for the first time at 11:06Z on the
2026-09-20 push (run 35506929343). It opened **48 fan-out PRs** at
head `ci/fanout-pin` against the 49 repos in `DEFAULT_CONSUMERS`, each
advancing `review-gate-reusable.yml` from `2eb4d94f` or `a1287f8b` to
`0d09e02af3cd` (40-char sha). Of those 48 PRs:

- **0 merged** as of 2026-09-20 ~22:50Z.
- **48 open**, no `pin-intra-org` label applied by the engine.
- Branch tip on every consumer diverged from `main`; structural
  mergeability varies (CONFLICTING on `maxi-tools/maxi-tui` PR #98
  after a subsequent main push; MERGEABLE on most others).
- `lint-gate` is the dominant failure on the open PRs; the second
  sweep at 22:46Z (run 35541790216, on PR #36 merge) **failed with
  `error connecting to api.github.com` from `gh pr list` calls** and
  did not dispatch. The next push to ci main will re-fire.

The fan-out PR is the inert-detector, and it is firing: 48 PRs are
visible, dated 2026-09-20, recording exactly the gap this contract
was written to expose. The consumer-side merge is the missing step.

**The fan-out PR was designed not to auto-merge** (see "Why not a
bot that auto-merges fan-out PRs" above). Landing them is a
per-consumer maintainer (or `CODEOWNERS`) decision. The intent is that
each consumer's owner sees the diff in their own PR queue, and the
fleet-wide advance happens at human speed on a human's clock.

**Known merge-blocker on the open PRs:** `lint-gate` rejects
intra-org pin updates without the `pin-intra-org` label (per
`maxi-config` ruleset policy). The fan-out engine opens the PR but
does not label it. Either the engine must apply the label, or the
consumer's reviewer must. Both paths are deliberate; this contract
does not pick one. Tracked as `pin-intra-org` / "advance the pins" in
the next board iteration.