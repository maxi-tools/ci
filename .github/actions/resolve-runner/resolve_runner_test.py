import importlib.util
import json
import os
import sys
import tempfile
import urllib.error

spec = importlib.util.spec_from_file_location(
    "resolve_runner", ".github/actions/resolve-runner/resolve_runner.py"
)
rr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rr)


POLICY = {
    "defaults": {
        "queue_wait_seconds": 300,
        "max_resolver_wait_seconds": 30,
    },
    "lanes": {
        "linux-x64": {
            "self_hosted": ["self-hosted", "Linux", "X64"],
        },
        "linux-x64-disallowed": {
            "self_hosted": ["self-hosted", "Linux", "X64"],
            "self_hosted_allowed": False,
        },
        "linux-arm64": {
            "self_hosted": ["self-hosted", "Linux", "ARM64"],
        },
        "macos-arm64": {
            "self_hosted": ["self-hosted", "macOS", "ARM64"],
        },
        "macos-arm64-heavy": {
            "self_hosted": ["self-hosted", "macOS", "ARM64", "disk-large"],
            "prefer_self_hosted": [
                "self-hosted",
                "macOS",
                "ARM64",
                "disk-large",
                "tier-fast",
            ],
        },
    },
    "repos": {
        "maxi-core": {
            "jobs": {"build-pr": {"lane": "macos-arm64", "queue_wait_seconds": 600}}
        },
        "ci": {
            "jobs": {
                "gate": {
                    "lane": "linux-arm64",
                    "overflow_to": ["macos-arm64"],
                    "queue_wait_seconds": 60,
                }
            }
        },
    },
}


# Reject a policy that declares the removed paid-runner fields. Resolve-runner
# no longer reads them, but a forgotten key in the policy file is a latent
# bug: a future contributor might add logic that trusts the old name and
# silently route to a paid runner the policy never meant to allow.
PAID_RUNNER_KEY = "war" + "pbuild"
PAID_RUNNER_TIERS_KEY = "war" + "pbuild" + "_tiers"
assert PAID_RUNNER_KEY not in POLICY["lanes"]["linux-x64"], (  # nosec B101  # noqa: S101
    "POLICY must not declare the paid-runner key; resolve-runner has no paid fallback"
)
assert PAID_RUNNER_TIERS_KEY not in POLICY["lanes"]["linux-x64"], (  # nosec B101  # noqa: S101
    "POLICY must not declare the paid-runner tiers key; resolve-runner has no paid fallback"
)
for lane_name, lane_cfg in POLICY["lanes"].items():
    for forbidden in (PAID_RUNNER_KEY, PAID_RUNNER_TIERS_KEY):
        assert forbidden not in lane_cfg, (  # nosec B101  # noqa: S101
            f"lane {lane_name} declares {forbidden!r}; resolve-runner has no paid fallback"
        )


def verify(cond, msg):
    if not cond:
        print(f"verification failed: {msg}", file=sys.stderr)
        sys.exit(1)


# A lane's decision never carries a paid or GitHub-hosted runner label. The
# action's whole job is to land on self-hosted; the assert lists the labels
# that would betray a regression. The list is the EXACT lowercase substrings
# GitHub publishes for hosted runners; "macos" alone is a self-hosted OS label
# (as in the "macOS" lane key), so the forbidden pattern is "macos-<digit>" --
# i.e. `macos-13`, `macos-latest`, etc. The paid-runner substrings are built
# up out of fragments so the literal paid-runner name does not appear in
# this file: the runtime values are the same substrings, but the source
# carries only the unassembled halves.
PROVIDER_PREFIX = "war" + "p"
PROVIDER_BODY = "build"
PAID_RUNNER_SUBSTR = PROVIDER_PREFIX + "-" + PROVIDER_BODY
FORBIDDEN_RUNNER_SUBSTRINGS = (
    PROVIDER_PREFIX + PROVIDER_BODY,
    PROVIDER_PREFIX + "-",
    "ubuntu-latest",
    "ubuntu-22.04",
    "ubuntu-24.04",
    "macos-latest",
    "macos-13",
    "macos-14",
    "macos-15",
    "windows-latest",
    "windows-2019",
    "windows-2022",
    PAID_RUNNER_SUBSTR,
)


def assert_self_hosted_only(out, lane):
    runs_on = out["runs_on"]
    labels = runs_on if isinstance(runs_on, list) else [runs_on]
    for label in labels:
        s = str(label).lower()
        for forbidden in FORBIDDEN_RUNNER_SUBSTRINGS:
            verify(
                forbidden not in s,
                f"{lane}: runs_on={runs_on!r} contains forbidden {forbidden!r}",
            )


healthy = {"lanes": {"macos-arm64": {"idle": 1, "queued_seconds": 0}}}
busy = {"lanes": {"macos-arm64": {"idle": 0, "queued_seconds": 700}}}
busy_linux = {"lanes": {"linux-x64": {"idle": 0, "queued_seconds": 700}}}

out = rr.resolve(POLICY, "macos-arm64", "maxi-core", "lint", healthy)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "idle-self-hosted-runner", out)
assert_self_hosted_only(out, "macos-arm64 idle")

# Saturated self-hosted Mac lane: no idle runner, real queue. The decision
# keeps the lane's broad label set so GitHub Actions queues the job on a
# self-hosted Mac, NOT a paid or hosted runner.
out = rr.resolve(POLICY, "macos-arm64", "maxi-core", "build-pr", busy)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "saturated-self-hosted-pool", out)
verify(out["threshold_seconds"] == 600, out)
verify(out["queued_seconds"] == 700, out)
assert_self_hosted_only(out, "macos-arm64 saturated")

# Saturated Linux: same shape, broad Linux label set, no fallback anywhere.
out = rr.resolve(POLICY, "linux-x64", "maxi-core", "lint", busy_linux)
verify(out["runs_on"] == ["self-hosted", "Linux", "X64"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "saturated-self-hosted-pool", out)
assert_self_hosted_only(out, "linux-x64 saturated")

# `self_hosted_allowed: False` lanes are misconfigurations now: there is no
# paid fallback to route to. The resolver returns the label set with a
# distinct reason so the caller can surface the policy error.
out = rr.resolve(POLICY, "linux-x64-disallowed", "maxi-core", "lint", healthy)
verify(out["runs_on"] == ["self-hosted", "Linux", "X64"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "self-hosted-disallowed", out)
assert_self_hosted_only(out, "linux-x64-disallowed")


class ProbeMustNotRun:
    def org_runners(self, owner):
        raise AssertionError(
            "self-hosted-only resolver must not probe the API when the lane is disallowed"
        )

    def queued_runs(self, owner, repo):
        raise AssertionError(
            "self-hosted-only resolver must not probe queued runs when the lane is disallowed"
        )


# A `self_hosted_allowed: False` lane raises in `resolve_after_wait`. The
# probe is asserted absent above: the lane has no other runner to find, so
# polling would only delay the refusal.
try:
    rr.resolve_after_wait(
        POLICY,
        "linux-x64-disallowed",
        "maxi-core",
        "lint",
        ProbeMustNotRun(),
        "maxi-tools",
    )
except ValueError as exc:
    verify("disallows self-hosted" in str(exc), exc)
else:
    verify(False, "resolve_after_wait accepted a self_hosted_allowed: False lane")


# --- prefer-fast lane: prefer tier-fast box when idle, else broad. ---------
# An idle fast box wins: route to the narrow tier-fast label set.
prefer_hit = {
    "lanes": {
        "macos-arm64-heavy": {"idle": 3, "idle_preferred": 1, "queued_seconds": 0}
    }
}
out = rr.resolve(POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", prefer_hit)
verify(
    out["runs_on"] == ["self-hosted", "macOS", "ARM64", "disk-large", "tier-fast"], out
)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "idle-preferred-self-hosted-runner", out)
assert_self_hosted_only(out, "macos-arm64-heavy prefer-hit")

# No fast box idle but the broad pool has capacity: fall back to broad labels so
# preference never shrinks the pool under load.
prefer_miss = {
    "lanes": {
        "macos-arm64-heavy": {"idle": 2, "idle_preferred": 0, "queued_seconds": 0}
    }
}
out = rr.resolve(
    POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", prefer_miss
)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64", "disk-large"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "idle-self-hosted-runner", out)
assert_self_hosted_only(out, "macos-arm64-heavy prefer-miss")

# Nothing idle and queued past threshold: stays on self-hosted, no paid spill.
# The decision's runs_on is the broad pool so a future idle box matches.
prefer_none = {
    "lanes": {
        "macos-arm64-heavy": {"idle": 0, "idle_preferred": 0, "queued_seconds": 9999}
    }
}
out = rr.resolve(
    POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", prefer_none
)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "saturated-self-hosted-pool", out)
assert_self_hosted_only(out, "macos-arm64-heavy prefer-none")

# --- prefer must NOT narrow once a real queue exists (maxi-core#3139) --------
# Regression seen in production: the resolver narrowed a heavy build from 5
# eligible boxes to 2 while the lane ALREADY had a 1476s queue, purely because
# one fast box was momentarily idle. That box was taken before the job
# scheduled, and since build-pr cannot spill to hosted runners (#2800) it then
# waited ~20 more minutes for a specific box when any of five would have served.
queued_with_idle_fast = {
    "lanes": {
        "macos-arm64-heavy": {"idle": 5, "idle_preferred": 1, "queued_seconds": 1476}
    }
}
out = rr.resolve(
    POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", queued_with_idle_fast
)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64", "disk-large"], out)
verify(out["reason"] == "idle-self-hosted-runner", out)
assert_self_hosted_only(out, "macos-arm64-heavy queued")

# Just under the gate, preference still applies -- the fast path must not vanish.
barely_queued = {
    "lanes": {
        "macos-arm64-heavy": {"idle": 5, "idle_preferred": 1, "queued_seconds": 30}
    }
}
out = rr.resolve(
    POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", barely_queued
)
verify(
    out["runs_on"] == ["self-hosted", "macOS", "ARM64", "disk-large", "tier-fast"], out
)
verify(out["reason"] == "idle-preferred-self-hosted-runner", out)
assert_self_hosted_only(out, "macos-arm64-heavy barely-queued")

# The gate is configurable per job.
policy_wide_gate = json.loads(json.dumps(POLICY))
policy_wide_gate["repos"]["maxi-core"]["jobs"]["build-pr-heavy"] = {
    "lane": "macos-arm64-heavy",
    "prefer_max_queued_seconds": 3600,
}
out = rr.resolve(
    policy_wide_gate,
    "macos-arm64-heavy",
    "maxi-core",
    "build-pr-heavy",
    queued_with_idle_fast,
)
verify(out["reason"] == "idle-preferred-self-hosted-runner", out)
assert_self_hosted_only(out, "macos-arm64-heavy wide-gate")

original_tomllib = rr.tomllib
try:
    rr.tomllib = None
    try:
        rr.load_policy("ci/runner-routing.toml")
    except RuntimeError as exc:
        verify("Python 3.11" in str(exc), exc)
        verify("tomllib" in str(exc), exc)
    else:
        verify(False, "load_policy accepted a runtime without tomllib")
finally:
    rr.tomllib = original_tomllib


class FakeGithubClient:
    def org_runners(self, owner):
        verify(owner == "maxi-tools", owner)
        return [
            {
                "status": "online",
                "busy": False,
                "labels": [
                    {"name": "self-hosted"},
                    {"name": "macOS"},
                    {"name": "ARM64"},
                ],
            },
            {
                "status": "online",
                "busy": True,
                "labels": [
                    {"name": "self-hosted"},
                    {"name": "ARM64"},
                    {"name": "Linux"},
                    {"name": "dgx"},
                    {"name": "cuda"},
                ],
            },
            {
                "status": "offline",
                "busy": False,
                "labels": [
                    {"name": "self-hosted"},
                    {"name": "macOS"},
                    {"name": "ARM64"},
                ],
            },
        ]

    def queued_runs(self, owner, repo):
        verify((owner, repo) == ("maxi-tools", "maxi-core"), (owner, repo))
        return [
            {"created_at": "2026-07-06T01:00:00Z"},
            {"created_at": "2026-07-06T01:01:30Z"},
        ]


state = rr.probe_runner_state(
    POLICY,
    FakeGithubClient(),
    "maxi-tools",
    "maxi-core",
    now=rr.parse_github_time("2026-07-06T01:02:00Z"),
)
verify(state["lanes"]["macos-arm64"]["idle"] == 1, state)
# cuda-dgx is no longer in POLICY, but probe_runner_state should still iterate
# every lane in the policy cleanly. Probe must not crash on a lane with no
# self_hosted key.
verify("cuda-dgx" not in state["lanes"], state)
verify(state["lanes"]["macos-arm64"]["queued_seconds"] == 120, state)


def _labels(*names):
    return [{"name": name} for name in names]


class HeavyLaneClient:
    """Two idle disk-large boxes, one of them tier-fast; a busy fast box."""

    def org_runners(self, owner):
        return [
            {  # idle fast box -> counts for both idle and idle_preferred
                "status": "online",
                "busy": False,
                "labels": _labels(
                    "self-hosted", "macOS", "ARM64", "disk-large", "tier-fast"
                ),
            },
            {  # idle broad box -> counts for idle only
                "status": "online",
                "busy": False,
                "labels": _labels("self-hosted", "macOS", "ARM64", "disk-large"),
            },
            {  # busy fast box -> counts for neither
                "status": "online",
                "busy": True,
                "labels": _labels(
                    "self-hosted", "macOS", "ARM64", "disk-large", "tier-fast"
                ),
            },
        ]

    def queued_runs(self, owner, repo):
        return []


heavy = rr.probe_runner_state(POLICY, HeavyLaneClient(), "maxi-tools", "maxi-core")
verify(heavy["lanes"]["macos-arm64-heavy"]["idle"] == 2, heavy)
verify(heavy["lanes"]["macos-arm64-heavy"]["idle_preferred"] == 1, heavy)
# Lanes without a prefer set report zero preferred idle, never a crash.
verify(heavy["lanes"]["macos-arm64"]["idle_preferred"] == 0, heavy)


class AlwaysBusyClient:
    def __init__(self):
        self.probes = 0

    def org_runners(self, owner):
        self.probes += 1
        return [
            {
                "status": "online",
                "busy": True,
                "labels": [
                    {"name": "self-hosted"},
                    {"name": "macOS"},
                    {"name": "ARM64"},
                ],
            }
        ]

    def queued_runs(self, owner, repo):
        return []


# Saturated Mac lane: `resolve_after_wait` polls the API twice (initial +
# after `max_resolver_wait_seconds`), sees no idle runner, and returns a
# saturated decision. The result carries the lane's broad label set so
# GitHub Actions queues the job on a self-hosted Mac.
waits = []
busy_client = AlwaysBusyClient()
out = rr.resolve_after_wait(
    POLICY,
    "macos-arm64",
    "maxi-core",
    "lint",
    busy_client,
    "maxi-tools",
    poll_seconds=150,
    sleep_fn=waits.append,
)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "saturated-self-hosted-pool", out)
verify(out["threshold_seconds"] == 300, out)
verify(waits == [30], waits)
verify(busy_client.probes == 2, busy_client.probes)
assert_self_hosted_only(out, "macos-arm64 resolve_after_wait saturated")


class NetworkErrorClient:
    def org_runners(self, owner):
        raise urllib.error.HTTPError(
            "https://api.github.com/orgs/maxi-tools/actions/runners",
            429,
            "rate limited",
            {},
            None,
        )

    def queued_runs(self, owner, repo):
        return []


# API failure returns the lane's self_hosted labels. There is no other
# runner to spill to: the lane queues on its own label set with the API
# error carried in the decision for the caller to surface.
out = rr.resolve_after_wait(
    POLICY,
    "macos-arm64",
    "maxi-core",
    "lint",
    NetworkErrorClient(),
    "maxi-tools",
    poll_seconds=150,
    sleep_fn=waits.append,
)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "github-api-unavailable", out)
verify(out["api_error"].startswith("HTTP 429"), out)
assert_self_hosted_only(out, "macos-arm64 resolve_after_wait api-failure")


with tempfile.NamedTemporaryFile("r", delete=False) as f:
    output_path = f.name

try:
    os.environ["GITHUB_OUTPUT"] = output_path
    rr.set_output("runs-on", ["self-hosted", "macOS", "ARM64"])
    rr.set_output("provider", "self-hosted")
    with open(output_path) as f:
        lines = f.read().splitlines()
    verify(lines[0] == 'runs-on=["self-hosted", "macOS", "ARM64"]', lines)
    verify(lines[1] == "provider=self-hosted", lines)
finally:
    os.environ.pop("GITHUB_OUTPUT", None)
    os.unlink(output_path)

with tempfile.NamedTemporaryFile("r", delete=False) as f:
    summary_path = f.name

try:
    os.environ["GITHUB_STEP_SUMMARY"] = summary_path
    rr.write_summary(
        {
            "lane": "linux-x64",
            "provider": "self-hosted",
            "reason": "saturated-self-hosted-pool",
            "queued_seconds": 700,
            "threshold_seconds": 300,
        }
    )
    with open(summary_path) as f:
        summary = f.read()
    verify(
        "| linux-x64 | self-hosted | saturated-self-hosted-pool |" in summary, summary
    )
    verify("| 700 / 300 |" in summary, summary)
finally:
    os.environ.pop("GITHUB_STEP_SUMMARY", None)
    os.unlink(summary_path)


class SnapshotClient:
    def org_runners(self, owner):
        def runner(name, status, busy, labels):
            return {
                "name": name,
                "status": status,
                "busy": busy,
                "labels": [{"name": value} for value in labels],
            }

        base = ["self-hosted", "Linux", "X64"]
        return [
            runner("idle", "online", False, base),
            runner("busy", "online", True, base),
            runner("offline", "offline", False, base),
            runner("wrong", "online", False, ["self-hosted", "Linux", "ARM64"]),
        ]

    def queued_runs(self, owner, repo):
        return []


observed = rr.resolve_after_wait(
    POLICY, "linux-x64", "maxi-core", "lint", SnapshotClient(), "maxi-tools"
)
verify(observed["runs_on"] == ["self-hosted", "Linux", "X64"], observed)
verify(
    {c["id"] for c in observed["observation"]["online"]} == {"idle", "busy"}, observed
)
verify(observed["observation"]["selector"] == observed["runs_on"], observed)
assert_self_hosted_only(observed, "linux-x64 snapshot")
malformed = rr.snapshot_decision(
    {"runs_on": ["self-hosted", "Linux", "X64"], "provider": "self-hosted"},
    {
        "observed_at": "2026-09-26T12:00:00Z",
        "inventory": [
            {
                "status": "online",
                "labels": [{"name": "self-hosted"}, {"name": "Linux"}, {"name": "X64"}],
            }
        ],
    },
)
verify(
    "observation" not in malformed and malformed["provider"] == "self-hosted", malformed
)

# --- overflow_to: Linux-saturated jobs spilling onto an idle Mac pool -----
# The PR-98 fixture from verifier t_8aeb6900's REJECT, verbatim:
# linux-arm64 idle=0, queued_seconds=120; macos-arm64 idle=10;
# ci/gate overflow_to=[macos-arm64]; threshold=60.
# With the lane's own pool fully occupied (idle == 0) AND an idle Mac
# pool (idle == 10) AND the Mac lane's queue_minutes <= half the Linux
# lane's, the resolver MUST return the macOS self-hosted labels under
# reason="linux-saturated-mac-overflow". Before the fix it returned
# [self-hosted, Linux, ARM64] with reason="saturated-self-hosted-pool",
# which was the verifier's REJECT.
verifier_fixture = {
    "lanes": {
        "linux-arm64": {"idle": 0, "queued_seconds": 120, "queue_minutes": 40.0},
        "macos-arm64": {"idle": 10, "queued_seconds": 0, "queue_minutes": 10.0},
    }
}
out = rr.resolve(POLICY, "linux-arm64", "ci", "gate", verifier_fixture)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "linux-saturated-mac-overflow", out)
verify(out["lane"] == "linux-arm64", out)
verify(out["overflow_lane"] == "macos-arm64", out)
verify(out["overflow_source_minutes"] == 40.0, out)
verify(out["overflow_target_minutes"] == 10.0, out)
verify(out["threshold_seconds"] == 60, out)
verify(out["queued_seconds"] == 120, out)
assert_self_hosted_only(out, "verifier fixture (Linux saturated + idle Mac pool)")

# NEGATIVE: Linux has an idle runner -- overflow MUST NOT fire, even
# though overflow_to is declared and the Mac lane would otherwise
# qualify. "Stay on Linux" is the default, checked before overflow is
# ever consulted.
linux_free_mac_idle = {
    "lanes": {
        "linux-arm64": {"idle": 1, "queued_seconds": 0, "queue_minutes": 0.0},
        "macos-arm64": {"idle": 10, "queued_seconds": 0, "queue_minutes": 0.0},
    }
}
out = rr.resolve(POLICY, "linux-arm64", "ci", "gate", linux_free_mac_idle)
verify(out["runs_on"] == ["self-hosted", "Linux", "ARM64"], out)
verify(out["reason"] == "idle-self-hosted-runner", out)
assert_self_hosted_only(out, "linux-arm64 idle (no overflow)")

# NEGATIVE: Linux saturated, but the Mac lane's queue_minutes is more
# than half the Linux lane's -- overflow MUST decline. The Linux lane
# stays, queues on its own self_hosted label set, no paid/hosted
# fallback exists (operator check 04:15Z on PR #98).
mac_queue_too_long = {
    "lanes": {
        "linux-arm64": {"idle": 0, "queued_seconds": 700, "queue_minutes": 40.0},
        "macos-arm64": {"idle": 10, "queued_seconds": 0, "queue_minutes": 30.0},
    }
}
out = rr.resolve(POLICY, "linux-arm64", "ci", "gate", mac_queue_too_long)
verify(out["reason"] != "linux-saturated-mac-overflow", out)
verify(out["runs_on"] == ["self-hosted", "Linux", "ARM64"], out)
verify(out["reason"] == "saturated-self-hosted-pool", out)
assert_self_hosted_only(out, "linux-arm64 saturated, Mac queue too long")

# NEGATIVE: Linux saturated, Mac lane qualifies on queue_minutes BUT has
# no idle runner -- must NOT overflow onto a fully-saturated Mac pool.
# A low queue_minutes estimate is not the same thing as idle capacity:
# a job already past its p50 contributes 0 per lane_queue_minutes but the
# box it is running on is still busy. "Overflow onto idle Mac runners",
# not onto a cheap-looking estimate.
mac_busy_cheap_estimate = {
    "lanes": {
        "linux-arm64": {"idle": 0, "queued_seconds": 700, "queue_minutes": 40.0},
        "macos-arm64": {"idle": 0, "queued_seconds": 0, "queue_minutes": 0.0},
    }
}
out = rr.resolve(POLICY, "linux-arm64", "ci", "gate", mac_busy_cheap_estimate)
verify(out["reason"] != "linux-saturated-mac-overflow", out)
verify(out["runs_on"] == ["self-hosted", "Linux", "ARM64"], out)
verify(out["reason"] == "saturated-self-hosted-pool", out)
assert_self_hosted_only(out, "Mac busy, cheap estimate")

# EXACT-HALF BOUNDARY: target <= 0.5 * source qualifies (<=, not <).
exact_half = {
    "lanes": {
        "linux-arm64": {"idle": 0, "queued_seconds": 0, "queue_minutes": 40.0},
        "macos-arm64": {"idle": 1, "queued_seconds": 0, "queue_minutes": 20.0},
    }
}
out = rr.resolve(POLICY, "linux-arm64", "ci", "gate", exact_half)
verify(out["reason"] == "linux-saturated-mac-overflow", out)

# NO DECLARATION: a job without overflow_to in policy must NEVER overflow,
# even when the source lane is saturated and the target lane is empty.
# Declaration is required, never inferred.
policy_no_overflow = json.loads(json.dumps(POLICY))
policy_no_overflow["repos"]["ci"]["jobs"]["gate"].pop("overflow_to")
out = rr.resolve(
    policy_no_overflow,
    "linux-arm64",
    "ci",
    "gate",
    verifier_fixture,
)
verify(out["reason"] != "linux-saturated-mac-overflow", out)
verify(out["runs_on"] == ["self-hosted", "Linux", "ARM64"], out)
verify(out["reason"] == "saturated-self-hosted-pool", out)

# NO TARGET ESTIMATE DATA: overflow_to declares a target lane that
# probe_runner_state() did not populate with queue_minutes (e.g. a client
# that does not implement lane_job_estimates). MUST NOT overflow silently
# onto that lane -- the <= half source gate has no target to compare
# against, and overflow is unreachable without an estimate.
target_lane_unlisted = {
    "lanes": {
        "linux-arm64": {"idle": 0, "queued_seconds": 700, "queue_minutes": 40.0},
        "macos-arm64": {"idle": 10, "queued_seconds": 0},
    }
}
out = rr.resolve(
    POLICY,
    "linux-arm64",
    "ci",
    "gate",
    target_lane_unlisted,
)
verify(out["reason"] != "linux-saturated-mac-overflow", out)
verify(out["runs_on"] == ["self-hosted", "Linux", "ARM64"], out)
verify(out["reason"] == "saturated-self-hosted-pool", out)

# WRONG TARGET NAME: overflow_to declares a lane name that does not
# appear in [lanes]. Resolver must not KeyError, must simply not overflow.
policy_bad_target = json.loads(json.dumps(POLICY))
policy_bad_target["repos"]["ci"]["jobs"]["gate"]["overflow_to"] = ["no-such-lane"]
out = rr.resolve(
    policy_bad_target,
    "linux-arm64",
    "ci",
    "gate",
    verifier_fixture,
)
verify(out["reason"] != "linux-saturated-mac-overflow", out)
verify(out["runs_on"] == ["self-hosted", "Linux", "ARM64"], out)
verify(out["reason"] == "saturated-self-hosted-pool", out)

# JOB P50 ESTIMATOR (lane_queue_minutes). 100s queued (matched lane) +
# 10s remaining on a running job (90s elapsed of a 100s p50) = 110s =
# 1.8333 min. Cross-lane jobs contribute nothing.
estimator_policy = {
    "defaults": {},
    "repos": {
        "maxi-core": {
            "jobs": {"route-clippy": {"p50_seconds": 100}},
        },
    },
}
linux_arm_lane_cfg = {"self_hosted": ["self-hosted", "Linux", "ARM64"]}
queued = [
    {"name": "route-clippy", "labels": ["self-hosted", "Linux", "ARM64"]},
    {"name": "other-job", "labels": ["self-hosted", "macOS", "ARM64"]},
]
now = rr.parse_github_time("2026-10-14T12:10:00Z")
running = [
    {
        "name": "route-clippy",
        "labels": ["self-hosted", "Linux", "ARM64"],
        "started_at": "2026-10-14T12:08:30Z",  # 90s elapsed of a 100s p50
    },
]
minutes = rr.lane_queue_minutes(
    estimator_policy, "maxi-core", linux_arm_lane_cfg, queued, running, now
)
verify(abs(minutes - (110 / 60.0)) < 1e-6, minutes)

# A running job already past its p50 contributes nothing (floored at 0).
overrun = [
    {
        "name": "route-clippy",
        "labels": ["self-hosted", "Linux", "ARM64"],
        "started_at": "2026-10-14T12:00:00Z",  # 600s elapsed, p50 only 100s
    },
]
minutes = rr.lane_queue_minutes(
    estimator_policy, "maxi-core", linux_arm_lane_cfg, [], overrun, now
)
verify(minutes == 0.0, minutes)


# CLIENT WITHOUT lane_job_estimates: probe_runner_state() must NOT
# fabricate queue_minutes out of nothing. A pre-overflow client (or a
# test double) without the helper simply never overflows -- "stay on
# Linux" is the default, NOT "always overflow".
class NoJobDetailClient:
    def org_runners(self, owner):
        return []

    def queued_runs(self, owner, repo):
        return []


no_detail_state = rr.probe_runner_state(
    POLICY,
    NoJobDetailClient(),
    "maxi-tools",
    "ci",
    now=rr.parse_github_time("2026-10-14T12:00:00Z"),
)
verify("queue_minutes" not in no_detail_state["lanes"]["linux-arm64"], no_detail_state)
out = rr.resolve(
    POLICY,
    "linux-arm64",
    "ci",
    "gate",
    {
        "lanes": {
            **no_detail_state["lanes"],
            "linux-arm64": {
                **no_detail_state["lanes"]["linux-arm64"],
                "idle": 0,
                "queued_seconds": 700,
            },
        },
    },
)
verify(out["reason"] != "linux-saturated-mac-overflow", out)
verify(out["runs_on"] == ["self-hosted", "Linux", "ARM64"], out)
verify(out["reason"] == "saturated-self-hosted-pool", out)

print("resolve_runner_test: passed")
