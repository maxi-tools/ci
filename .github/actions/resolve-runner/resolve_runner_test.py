import importlib.util
import json
import os
import sys
import tempfile
import urllib.error

spec = importlib.util.spec_from_file_location(
    "resolve_runner", ".github/actions/resolve-runner/resolve_runner.py")
rr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rr)


POLICY = {
    "defaults": {
        "fallback_enabled": True,
        "queue_wait_seconds": 300,
        "max_resolver_wait_seconds": 30,
        "max_cost_tier": "standard",
    },
    "lanes": {
        "linux-x64": {
            "self_hosted": ["self-hosted", "Linux", "X64"],
            "warpbuild": "warp-ubuntu-latest-x64-4x",
            "warpbuild_tiers": [
                {"cost_tier": "cheap", "runner": "warp-ubuntu-latest-x64-2x"},
                {"cost_tier": "standard", "runner": "warp-ubuntu-latest-x64-4x"},
                {"cost_tier": "premium", "runner": "warp-ubuntu-latest-x64-8x"},
            ],
        },
        "linux-x64-ephemeral-review": {
            "self_hosted": ["self-hosted", "Linux", "X64"],
            "self_hosted_allowed": False,
            "warpbuild_tiers": [
                {"cost_tier": "cheap", "runner": "warp-ubuntu-latest-x64-2x"},
                {"cost_tier": "standard", "runner": "warp-ubuntu-latest-x64-4x"},
            ],
        },
        "macos-arm64": {
            "self_hosted": ["self-hosted", "macOS", "ARM64"],
            "warpbuild": "warp-macos-15-arm64-6x",
            "warpbuild_tiers": [
                {"cost_tier": "standard", "runner": "warp-macos-15-arm64-6x"},
                {"cost_tier": "premium", "runner": "warp-macos-15-arm64-12x"},
            ],
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
            "warpbuild": "warp-macos-15-arm64-12x",
            "warpbuild_tiers": [
                {"cost_tier": "standard", "runner": "warp-macos-15-arm64-6x"},
                {"cost_tier": "premium", "runner": "warp-macos-15-arm64-12x"},
            ],
            "max_cost_tier": "premium",
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
            "warpbuild": "warp-macos-15-arm64-12x",
            "warpbuild_tiers": [
                {"cost_tier": "standard", "runner": "warp-macos-15-arm64-6x"},
                {"cost_tier": "premium", "runner": "warp-macos-15-arm64-12x"},
            ],
            "max_cost_tier": "premium",
        },
        "cuda-dgx": {
            "self_hosted": ["self-hosted", "ARM64", "Linux", "dgx", "cuda"],
            "fallback_enabled": False,
        },
    },
    "repos": {
        "maxi-agent-runner": {
            "jobs": {
                "claude-review": {
                    "lane": "linux-x64-ephemeral-review",
                    "job_family": "control-plane",
                    "urgency": "advisory",
                    "max_cost_tier": "cheap",
                }
            }
        },
        "maxi-config": {
            "jobs": {
                "route-clippy": {
                    "lane": "linux-x64",
                    "job_family": "control-plane",
                    "p50_seconds": 45,
                    "p95_seconds": 120,
                    "wall_time_budget_seconds": 300,
                    "urgency": "required",
                },
                "clippy": {
                    "lane": "macos-arm64",
                    "job_family": "macos-portable",
                    "p50_seconds": 300,
                    "p95_seconds": 600,
                    "wall_time_budget_seconds": 900,
                    "urgency": "required",
                },
                "lint-pr": {
                    "lane": "linux-x64",
                    "job_family": "validation",
                    "p50_seconds": 180,
                    "p95_seconds": 300,
                    "wall_time_budget_seconds": 600,
                    "urgency": "required",
                },
                "heavy-scan": {
                    "lane": "linux-x64",
                    "job_family": "validation",
                    "p50_seconds": 600,
                    "p95_seconds": 1200,
                    "wall_time_budget_seconds": 600,
                    "urgency": "required",
                    "max_cost_tier": "premium",
                },
                "slow-default": {
                    "lane": "linux-x64",
                    "job_family": "validation",
                    "p50_seconds": 600,
                    "p95_seconds": 1200,
                    "wall_time_budget_seconds": 600,
                    "urgency": "required",
                },
            }
        },
        "maxi-core": {
            "jobs": {
                "build-pr": {"lane": "macos-arm64", "queue_wait_seconds": 600}
            }
        },
    },
}


def verify(cond, msg):
    if not cond:
        print(f"verification failed: {msg}", file=sys.stderr)
        sys.exit(1)


healthy = {"lanes": {"macos-arm64": {"idle": 1, "queued_seconds": 0}}}
busy = {"lanes": {"macos-arm64": {"idle": 0, "queued_seconds": 700}}}
busy_linux = {"lanes": {"linux-x64": {"idle": 0, "queued_seconds": 700}}}
cuda_busy = {"lanes": {"cuda-dgx": {"idle": 0, "queued_seconds": 9999}}}

out = rr.resolve(POLICY, "macos-arm64", "maxi-core", "lint", healthy)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64"], out)
verify(out["provider"] == "self-hosted", out)

hosted_review = rr.resolve(
    POLICY,
    "linux-x64-ephemeral-review",
    "maxi-agent-runner",
    "claude-review",
    {"lanes": {"linux-x64-ephemeral-review": {"idle": 99, "queued_seconds": 0}}},
)
verify(hosted_review["runs_on"] == "warp-ubuntu-latest-x64-2x", hosted_review)
verify(hosted_review["provider"] == "warpbuild", hosted_review)
verify(hosted_review["reason"] == "self-hosted-disallowed", hosted_review)

class ProbeMustNotRun:
    def org_runners(self, owner):
        raise AssertionError("hosted-only review routing must not probe self-hosted runners")

    def queued_runs(self, owner, repo):
        raise AssertionError("hosted-only review routing must not probe queued runs")

hosted_review = rr.resolve_after_wait(
    POLICY,
    "linux-x64-ephemeral-review",
    "maxi-agent-runner",
    "claude-review",
    ProbeMustNotRun(),
    "maxi-tools",
)
verify(hosted_review["provider"] == "warpbuild", hosted_review)

out = rr.resolve(POLICY, "macos-arm64", "maxi-core", "build-pr", busy)
verify(out["runs_on"] == "warp-macos-15-arm64-6x", out)
verify(out["provider"] == "warpbuild", out)
verify(out["threshold_seconds"] == 600, out)

out = rr.resolve(POLICY, "linux-x64", "maxi-config", "route-clippy", busy_linux)
verify(out["runs_on"] == "warp-ubuntu-latest-x64-2x", out)
verify(out["provider"] == "warpbuild", out)
verify(out["reason"] == "budget-tier-selected", out)
verify(out["cost_tier"] == "cheap", out)
verify(out["tier_runner"] == "warp-ubuntu-latest-x64-2x", out)

out = rr.resolve(POLICY, "linux-x64", "maxi-config", "lint-pr", busy_linux)
verify(out["runs_on"] == "warp-ubuntu-latest-x64-4x", out)
verify(out["cost_tier"] == "standard", out)

out = rr.resolve(POLICY, "linux-x64", "maxi-config", "heavy-scan", busy_linux)
verify(out["runs_on"] == "warp-ubuntu-latest-x64-8x", out)
verify(out["cost_tier"] == "premium", out)
verify(out["reason"] == "budget-tier-selected", out)

out = rr.resolve(POLICY, "macos-arm64", "maxi-config", "clippy", busy)
verify(out["runs_on"] == "warp-macos-15-arm64-6x", out)
verify(out["cost_tier"] == "standard", out)
verify(out["job_family"] == "macos-portable", out)

out = rr.resolve(POLICY, "linux-x64", "maxi-config", "slow-default", busy_linux)
verify(out["runs_on"] == "warp-ubuntu-latest-x64-4x", out)
verify(out["cost_tier"] == "standard", out)
verify(out["reason"] == "cost-ceiling-clamped", out)

out = rr.resolve(POLICY, "cuda-dgx", "maxi-ml", "ane-hardware-tests", cuda_busy)
verify(out["runs_on"] == ["self-hosted", "ARM64", "Linux", "dgx", "cuda"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "fallback-disabled", out)
verify("cost_tier" not in out, out)

# --- prefer-fast lane: prefer tier-fast box when idle, else broad, else warp ---
# An idle fast box wins: route to the narrow tier-fast label set.
prefer_hit = {
    "lanes": {"macos-arm64-heavy": {"idle": 3, "idle_preferred": 1, "queued_seconds": 0}}
}
out = rr.resolve(POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", prefer_hit)
verify(
    out["runs_on"] == ["self-hosted", "macOS", "ARM64", "disk-large", "tier-fast"], out
)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "idle-preferred-self-hosted-runner", out)

# No fast box idle but the broad pool has capacity: fall back to broad labels so
# preference never shrinks the pool under load.
prefer_miss = {
    "lanes": {"macos-arm64-heavy": {"idle": 2, "idle_preferred": 0, "queued_seconds": 0}}
}
out = rr.resolve(POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", prefer_miss)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64", "disk-large"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "idle-self-hosted-runner", out)

# Nothing idle and queued past threshold: spill to the hosted premium fallback.
prefer_none = {
    "lanes": {
        "macos-arm64-heavy": {"idle": 0, "idle_preferred": 0, "queued_seconds": 9999}
    }
}
out = rr.resolve(POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", prefer_none)
verify(out["provider"] == "warpbuild", out)

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
out = rr.resolve(POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", queued_with_idle_fast)
verify(out["runs_on"] == ["self-hosted", "macOS", "ARM64", "disk-large"], out)
verify(out["reason"] == "idle-self-hosted-runner", out)

# Just under the gate, preference still applies — the fast path must not vanish.
barely_queued = {
    "lanes": {"macos-arm64-heavy": {"idle": 5, "idle_preferred": 1, "queued_seconds": 30}}
}
out = rr.resolve(POLICY, "macos-arm64-heavy", "maxi-core", "build-pr-heavy", barely_queued)
verify(
    out["runs_on"] == ["self-hosted", "macOS", "ARM64", "disk-large", "tier-fast"], out
)
verify(out["reason"] == "idle-preferred-self-hosted-runner", out)

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
verify(state["lanes"]["cuda-dgx"]["idle"] == 0, state)
verify(state["lanes"]["macos-arm64"]["queued_seconds"] == 120, state)
verify(state["lanes"]["cuda-dgx"]["queued_seconds"] == 120, state)


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
verify(out["runs_on"] == "warp-macos-15-arm64-6x", out)
verify(out["provider"] == "warpbuild", out)
verify(out["reason"] == "resolver-wait-cap-exceeded", out)
verify(out["cost_tier"] == "standard", out)
verify(out["threshold_seconds"] == 300, out)
verify(out["max_resolver_wait_seconds"] == 30, out)
verify(waits == [30], waits)
verify(busy_client.probes == 2, busy_client.probes)


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
# API failure means the probe cannot prove a matching self-hosted runner
# exists, so lanes with a hosted fallback must use it (maxi-config#162).
verify(out["runs_on"] == "warp-macos-15-arm64-6x", out)
verify(out["provider"] == "warpbuild", out)
verify(out["reason"] == "github-api-unavailable", out)
verify(out["api_error"].startswith("HTTP 429"), out)

out = rr.resolve_after_wait(
    POLICY,
    "cuda-dgx",
    "maxi-ml",
    "ane-hardware-tests",
    NetworkErrorClient(),
    "maxi-tools",
    poll_seconds=150,
    sleep_fn=waits.append,
)
# Lanes without a fallback keep their self-hosted labels even when the
# probe fails: queueing there is the intended behavior.
verify(out["runs_on"] == ["self-hosted", "ARM64", "Linux", "dgx", "cuda"], out)
verify(out["provider"] == "self-hosted", out)
verify(out["reason"] == "github-api-unavailable", out)
verify(out["api_error"].startswith("HTTP 429"), out)

with tempfile.NamedTemporaryFile("r", delete=False) as f:
    output_path = f.name

try:
    os.environ["GITHUB_OUTPUT"] = output_path
    rr.set_output("runs-on", "warp-macos-15-arm64-6x")
    rr.set_output("provider", "warpbuild")
    rr.set_output("tier", "warp-macos-15-arm64-6x")
    rr.set_output("cost_tier", "standard")
    with open(output_path) as f:
        lines = f.read().splitlines()
    verify(lines[0] == 'runs-on="warp-macos-15-arm64-6x"', lines)
    verify(json.loads(lines[0].split("=", 1)[1]) == "warp-macos-15-arm64-6x", lines)
    verify(lines[1] == "provider=warpbuild", lines)
    verify(lines[2] == "tier=warp-macos-15-arm64-6x", lines)
    verify(lines[3] == "cost_tier=standard", lines)
finally:
    os.environ.pop("GITHUB_OUTPUT", None)
    os.unlink(output_path)

with tempfile.NamedTemporaryFile("r", delete=False) as f:
    summary_path = f.name

try:
    os.environ["GITHUB_STEP_SUMMARY"] = summary_path
    rr.write_summary({
        "lane": "linux-x64",
        "provider": "warpbuild",
        "tier_runner": "warp-ubuntu-latest-x64-2x",
        "cost_tier": "cheap",
        "reason": "budget-tier-selected",
        "queued_seconds": 700,
        "threshold_seconds": 300,
    })
    with open(summary_path) as f:
        summary = f.read()
    verify("| linux-x64 | warpbuild | warp-ubuntu-latest-x64-2x (cheap) |" in summary, summary)
    verify("| 700 / 300 |" in summary, summary)
finally:
    os.environ.pop("GITHUB_STEP_SUMMARY", None)
    os.unlink(summary_path)

class SnapshotClient:
    def org_runners(self, owner):
        def runner(name, status, busy, labels):
            return {"name": name, "status": status, "busy": busy,
                    "labels": [{"name": value} for value in labels]}
        base = ["self-hosted", "Linux", "X64"]
        return [runner("idle", "online", False, base),
                runner("busy", "online", True, base),
                runner("offline", "offline", False, base),
                runner("wrong", "online", False, ["self-hosted", "Linux", "ARM64"])]

    def queued_runs(self, owner, repo):
        return []

observed = rr.resolve_after_wait(POLICY, "linux-x64", "maxi-config", "route-clippy",
                                 SnapshotClient(), "maxi-tools")
verify(observed["runs_on"] == ["self-hosted", "Linux", "X64"], observed)
verify({c["id"] for c in observed["observation"]["online"]} == {"idle", "busy"}, observed)
verify(observed["observation"]["selector"] == observed["runs_on"], observed)
malformed = rr.snapshot_decision(
    {"runs_on": ["self-hosted", "Linux", "X64"], "provider": "self-hosted"},
    {"observed_at": "2026-09-26T12:00:00Z", "inventory":
     [{"status": "online", "labels": [{"name": "self-hosted"}, {"name": "Linux"}, {"name": "X64"}]}]},
)
verify("observation" not in malformed and malformed["provider"] == "self-hosted", malformed)

print("resolve_runner_test: passed")
