#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None
from datetime import datetime, timezone


COST_TIER_RANK = {"cheap": 0, "standard": 1, "premium": 2}
COST_TIERS = ["cheap", "standard", "premium"]


class GithubApiError(RuntimeError):
    pass


def load_policy(policy_path):
    if tomllib is None:
        raise RuntimeError(
            "resolve-runner requires Python 3.11+ for tomllib support"
        )
    with open(policy_path, "rb") as f:
        return tomllib.load(f)


def repo_job_override(policy, repo, job):
    return (
        policy.get("repos", {})
        .get(repo, {})
        .get("jobs", {})
        .get(job, {})
    )


def effective_lane(policy, lane, repo, job):
    override = repo_job_override(policy, repo, job)
    return override.get("lane", lane)


def effective_value(policy, lane_cfg, repo, job, key, default=None):
    override = repo_job_override(policy, repo, job)
    if key in override:
        return override[key]
    if key in lane_cfg:
        return lane_cfg[key]
    return policy.get("defaults", {}).get(key, default)


def effective_int(policy, lane_cfg, repo, job, key):
    value = effective_value(policy, lane_cfg, repo, job, key)
    if value is None:
        return None
    return int(value)


def effective_bool(policy, lane_cfg, repo, job, key):
    value = effective_value(policy, lane_cfg, repo, job, key)
    return bool(value)


def effective_threshold(policy, lane_cfg, repo, job):
    override = repo_job_override(policy, repo, job)
    if "queue_wait_seconds" in override:
        return int(override["queue_wait_seconds"])
    if "queue_wait_seconds" in lane_cfg:
        return int(lane_cfg["queue_wait_seconds"])
    return int(policy["defaults"]["queue_wait_seconds"])


def effective_max_resolver_wait(policy, lane_cfg, repo, job):
    override = repo_job_override(policy, repo, job)
    if "max_resolver_wait_seconds" in override:
        return int(override["max_resolver_wait_seconds"])
    if "max_resolver_wait_seconds" in lane_cfg:
        return int(lane_cfg["max_resolver_wait_seconds"])
    return int(policy["defaults"].get("max_resolver_wait_seconds", policy["defaults"]["queue_wait_seconds"]))


# How much existing queue still permits narrowing to a lane's preferred
# sub-pool. Small on purpose: any real backlog means capacity, not latency, is
# the binding constraint. Override per lane/job with `prefer_max_queued_seconds`.
DEFAULT_PREFER_MAX_QUEUED_SECONDS = 60


def effective_prefer_max_queued_seconds(policy, lane_cfg, repo, job):
    value = effective_value(
        policy,
        lane_cfg,
        repo,
        job,
        "prefer_max_queued_seconds",
        DEFAULT_PREFER_MAX_QUEUED_SECONDS,
    )
    return int(value)


def has_warpbuild_fallback(lane_cfg):
    return bool(lane_cfg.get("warpbuild_tiers") or lane_cfg.get("warpbuild"))


def self_hosted_allowed(policy, lane_cfg, repo, job):
    return bool(effective_value(policy, lane_cfg, repo, job, "self_hosted_allowed", True))


def fallback_tiers(lane_cfg):
    tiers = lane_cfg.get("warpbuild_tiers") or []
    if tiers:
        return list(tiers)
    if lane_cfg.get("warpbuild"):
        return [{"cost_tier": "standard", "runner": lane_cfg["warpbuild"]}]
    return []


def tier_rank(cost_tier):
    return COST_TIER_RANK.get(cost_tier, COST_TIER_RANK["standard"])


def next_cost_tier(cost_tier):
    rank = min(tier_rank(cost_tier) + 1, len(COST_TIERS) - 1)
    return COST_TIERS[rank]


def baseline_cost_tier(job_family):
    family = (job_family or "validation").lower()
    if "control" in family or "route" in family:
        return "cheap"
    return "standard"


def select_tier(policy, lane_cfg, repo, job):
    tiers = fallback_tiers(lane_cfg)
    if not tiers:
        return None

    job_family = str(effective_value(policy, lane_cfg, repo, job, "job_family", "validation"))
    urgency = str(effective_value(policy, lane_cfg, repo, job, "urgency", "required"))
    max_cost_tier = str(effective_value(policy, lane_cfg, repo, job, "max_cost_tier", "standard"))
    p95_seconds = effective_int(policy, lane_cfg, repo, job, "p95_seconds")
    wall_time_budget_seconds = effective_int(policy, lane_cfg, repo, job, "wall_time_budget_seconds")

    desired = baseline_cost_tier(job_family)
    if (
        urgency != "advisory"
        and p95_seconds is not None
        and wall_time_budget_seconds is not None
        and p95_seconds > wall_time_budget_seconds
    ):
        desired = next_cost_tier(desired)

    candidate = None
    desired_rank = tier_rank(desired)
    for tier in tiers:
        if tier_rank(tier["cost_tier"]) >= desired_rank:
            candidate = tier
            break
    if candidate is None:
        candidate = tiers[-1]

    max_rank = tier_rank(max_cost_tier)
    if tier_rank(candidate["cost_tier"]) <= max_rank:
        reason = "budget-tier-selected"
        selected = candidate
    else:
        allowed = [tier for tier in tiers if tier_rank(tier["cost_tier"]) <= max_rank]
        selected = allowed[-1] if allowed else tiers[0]
        reason = "cost-ceiling-clamped"

    return {
        "runner": selected["runner"],
        "cost_tier": selected["cost_tier"],
        "job_family": job_family,
        "desired_cost_tier": desired,
        "max_cost_tier": max_cost_tier,
        "reason": reason,
    }


def warpbuild_decision(policy, lane, lane_cfg, repo, job, queued_seconds, threshold, reason=None):
    tier = select_tier(policy, lane_cfg, repo, job)
    if tier is None:
        return {
            "lane": lane,
            "runs_on": lane_cfg["self_hosted"],
            "provider": "self-hosted",
            "reason": "warpbuild-fallback-unconfigured",
            "threshold_seconds": threshold,
            "queued_seconds": queued_seconds,
        }

    decision_reason = reason or tier["reason"]
    decision = {
        "lane": lane,
        "runs_on": tier["runner"],
        "provider": "warpbuild",
        "reason": decision_reason,
        "threshold_seconds": threshold,
        "queued_seconds": queued_seconds,
        "tier": tier["runner"],
        "tier_runner": tier["runner"],
        "cost_tier": tier["cost_tier"],
        "job_family": tier["job_family"],
        "desired_cost_tier": tier["desired_cost_tier"],
        "max_cost_tier": tier["max_cost_tier"],
    }
    if reason and tier["reason"] != "budget-tier-selected":
        decision["tier_reason"] = tier["reason"]
    return decision


def self_hosted_disallowed_decision(policy, lane, repo, job):
    selected_lane = effective_lane(policy, lane, repo, job)
    lane_cfg = policy["lanes"][selected_lane]
    if not has_warpbuild_fallback(lane_cfg):
        raise ValueError(
            f"lane {selected_lane} disallows self-hosted runners but has no hosted runner"
        )
    return warpbuild_decision(
        policy,
        selected_lane,
        lane_cfg,
        repo,
        job,
        0,
        0,
        reason="self-hosted-disallowed",
    )


def capped_fallback_decision(policy, lane, repo, job, runner_state):
    selected_lane = effective_lane(policy, lane, repo, job)
    lane_cfg = policy["lanes"][selected_lane]
    threshold = effective_threshold(policy, lane_cfg, repo, job)
    max_wait = effective_max_resolver_wait(policy, lane_cfg, repo, job)
    state = runner_state.get("lanes", {}).get(selected_lane, {})
    queued_seconds = int(state.get("queued_seconds", 0))
    decision = warpbuild_decision(
        policy,
        selected_lane,
        lane_cfg,
        repo,
        job,
        queued_seconds,
        threshold,
        reason="resolver-wait-cap-exceeded",
    )
    decision["max_resolver_wait_seconds"] = max_wait
    return decision


def resolve(policy, lane, repo, job, runner_state):
    selected_lane = effective_lane(policy, lane, repo, job)
    lane_cfg = policy["lanes"][selected_lane]
    if not self_hosted_allowed(policy, lane_cfg, repo, job):
        return self_hosted_disallowed_decision(policy, lane, repo, job)
    threshold = effective_threshold(policy, lane_cfg, repo, job)
    fallback_enabled = effective_bool(policy, lane_cfg, repo, job, "fallback_enabled")
    state = runner_state.get("lanes", {}).get(selected_lane, {})
    idle = int(state.get("idle", 0))
    idle_preferred = int(state.get("idle_preferred", 0))
    queued_seconds = int(state.get("queued_seconds", 0))
    prefer = lane_cfg.get("prefer_self_hosted")

    # Soft preference: when the lane declares a faster sub-pool and one of those
    # boxes is idle right now, pin the job to the narrower label set so it lands
    # on a fast box. Otherwise fall through to the broad pool below, so
    # preference never shrinks capacity.
    #
    # `queued_seconds` gates this, and that gate is load-bearing rather than
    # decorative. Observed in production on maxi-core#3139: the resolver narrowed
    # a heavy build from 5 eligible boxes to 2 while the lane ALREADY had a
    # 1476-second queue, purely because one fast box was momentarily idle. That
    # box was taken before the job scheduled, and since `build-pr` deliberately
    # cannot spill to hosted runners (maxi-core#2800) it then waited ~20 further
    # minutes for a specific box when any of five would have served.
    #
    # Preference is a latency optimisation for a lane with spare capacity. Under
    # contention the broad pool is strictly better, so stop preferring the moment
    # a real queue exists.
    prefer_max_queued = effective_prefer_max_queued_seconds(policy, lane_cfg, repo, job)
    prefer_applies = bool(prefer) and idle_preferred > 0 and queued_seconds <= prefer_max_queued
    if prefer_applies:
        return {
            "lane": selected_lane,
            "runs_on": prefer,
            "provider": "self-hosted",
            "reason": "idle-preferred-self-hosted-runner",
            "threshold_seconds": threshold,
            "queued_seconds": queued_seconds,
        }

    if idle > 0:
        return {
            "lane": selected_lane,
            "runs_on": lane_cfg["self_hosted"],
            "provider": "self-hosted",
            "reason": "idle-self-hosted-runner",
            "threshold_seconds": threshold,
            "queued_seconds": queued_seconds,
        }

    if not fallback_enabled:
        return {
            "lane": selected_lane,
            "runs_on": lane_cfg["self_hosted"],
            "provider": "self-hosted",
            "reason": "fallback-disabled",
            "threshold_seconds": threshold,
            "queued_seconds": queued_seconds,
        }

    if queued_seconds >= threshold and has_warpbuild_fallback(lane_cfg):
        return warpbuild_decision(
            policy,
            selected_lane,
            lane_cfg,
            repo,
            job,
            queued_seconds,
            threshold,
        )

    return {
        "lane": selected_lane,
        "runs_on": lane_cfg["self_hosted"],
        "provider": "self-hosted",
        "reason": "below-queue-threshold",
        "threshold_seconds": threshold,
        "queued_seconds": queued_seconds,
    }


def api_unavailable_decision(policy, lane, repo, job, error):
    selected_lane = effective_lane(policy, lane, repo, job)
    lane_cfg = policy["lanes"][selected_lane]
    if not self_hosted_allowed(policy, lane_cfg, repo, job):
        return self_hosted_disallowed_decision(policy, lane, repo, job)
    threshold = effective_threshold(policy, lane_cfg, repo, job)
    fallback_enabled = effective_bool(policy, lane_cfg, repo, job, "fallback_enabled")
    # Without a probe we cannot prove any matching self-hosted runner exists;
    # routing there blindly wedges the job forever if the lane is empty
    # (maxi-config#162). Prefer the hosted fallback whenever one is allowed.
    if fallback_enabled and has_warpbuild_fallback(lane_cfg):
        decision = warpbuild_decision(
            policy,
            selected_lane,
            lane_cfg,
            repo,
            job,
            0,
            threshold,
            reason="github-api-unavailable",
        )
        decision["api_error"] = format_api_error(error)
        return decision
    return {
        "lane": selected_lane,
        "runs_on": lane_cfg["self_hosted"],
        "provider": "self-hosted",
        "reason": "github-api-unavailable",
        "threshold_seconds": threshold,
        "queued_seconds": 0,
        "api_error": format_api_error(error),
    }


def format_api_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code} {error.reason}"
    return str(error)


def parse_github_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def runner_labels(runner):
    return {label.get("name", "").lower() for label in runner.get("labels", [])}


def runner_has_labels(runner, labels):
    have = runner_labels(runner)
    return all(label.lower() in have for label in labels)


def runner_matches_lane(runner, lane_cfg):
    return runner_has_labels(runner, lane_cfg["self_hosted"])


def runner_is_idle(runner):
    return runner.get("status") == "online" and not runner.get("busy", False)


def oldest_queued_seconds(runs, now):
    created = [
        parse_github_time(run["created_at"])
        for run in runs
        if run.get("created_at")
    ]
    if not created:
        return 0
    return max(0, int((now - min(created)).total_seconds()))


class GithubClient:
    def __init__(self, token, api_url=None):
        self.token = token
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def api_json(self, path, params=None):
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(self.api_url + path + query)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "maxi-ci-runner-resolver")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GithubApiError(format_api_error(exc)) from exc
        except urllib.error.URLError as exc:
            raise GithubApiError(str(exc.reason)) from exc

    def org_runners(self, owner):
        runners = []
        page = 1
        while True:
            data = self.api_json(
                f"/orgs/{owner}/actions/runners",
                {"per_page": "100", "page": str(page)},
            )
            batch = data.get("runners", [])
            if not batch:
                break
            runners.extend(batch)
            page += 1
        return runners

    def queued_runs(self, owner, repo):
        runs = []
        page = 1
        while True:
            data = self.api_json(
                f"/repos/{owner}/{repo}/actions/runs",
                {"status": "queued", "per_page": "100", "page": str(page)},
            )
            batch = data.get("workflow_runs", [])
            if not batch:
                break
            runs.extend(batch)
            page += 1
        return runs


def probe_runner_state(policy, client, owner, repo, now=None):
    now = now or datetime.now(timezone.utc)
    runners = client.org_runners(owner)
    queued_seconds = oldest_queued_seconds(client.queued_runs(owner, repo), now)
    lanes = {}
    for lane, lane_cfg in policy["lanes"].items():
        idle = sum(
            1
            for runner in runners
            if runner_is_idle(runner) and runner_matches_lane(runner, lane_cfg)
        )
        prefer = lane_cfg.get("prefer_self_hosted")
        idle_preferred = 0
        if prefer:
            idle_preferred = sum(
                1
                for runner in runners
                if runner_is_idle(runner) and runner_has_labels(runner, prefer)
            )
        lanes[lane] = {
            "idle": idle,
            "idle_preferred": idle_preferred,
            "queued_seconds": queued_seconds,
        }
    # Preserve the SAME probe used to choose the selector. A later probe
    # would describe a different fleet, not the decision-time candidates.
    return {"lanes": lanes, "observed_at": now.isoformat(), "inventory": runners}

def snapshot_decision(decision, state):
    if "inventory" in state and isinstance(decision["runs_on"], list):
        try:
            decision["observation"] = {
                "observed_at": state["observed_at"],
                "selector": decision["runs_on"],
                "online": [
                    {"id": r["name"], "busy": r.get("busy", False),
                     "labels": sorted(runner_labels(r))}
                    for r in state["inventory"]
                    if r.get("status") == "online" and runner_has_labels(r, decision["runs_on"])
                ],
            }
        except (KeyError, TypeError, ValueError) as exc:
            # Observation must never turn an otherwise valid route into a
            # failed plan when a fleet API field is malformed.
            decision.pop("observation", None)
            print(f"shadow snapshot unavailable: {exc}", file=sys.stderr)
    return decision


def resolve_after_wait(policy, lane, repo, job, client, owner, poll_seconds=15, sleep_fn=time.sleep):
    selected_lane = effective_lane(policy, lane, repo, job)
    lane_cfg = policy["lanes"][selected_lane]
    if not self_hosted_allowed(policy, lane_cfg, repo, job):
        return self_hosted_disallowed_decision(policy, lane, repo, job)
    elapsed = 0
    while True:
        try:
            runner_state = probe_runner_state(policy, client, owner, repo)
        except (GithubApiError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            return api_unavailable_decision(policy, lane, repo, job, exc)
        selected_lane = effective_lane(policy, lane, repo, job)
        state = runner_state.setdefault("lanes", {}).setdefault(selected_lane, {})
        state["queued_seconds"] = max(int(state.get("queued_seconds", 0)), elapsed)
        decision = resolve(policy, lane, repo, job, runner_state)

        if decision["reason"] != "below-queue-threshold":
            return snapshot_decision(decision, runner_state)

        lane_cfg = policy["lanes"][selected_lane]
        max_wait = effective_max_resolver_wait(policy, lane_cfg, repo, job)
        can_cap_to_warp = has_warpbuild_fallback(lane_cfg)
        if can_cap_to_warp and elapsed >= max_wait:
            return snapshot_decision(capped_fallback_decision(policy, lane, repo, job, runner_state), runner_state)

        remaining = decision["threshold_seconds"] - decision["queued_seconds"]
        if remaining <= 0:
            return snapshot_decision(decision, runner_state)

        wait_seconds = min(poll_seconds, remaining)
        if can_cap_to_warp:
            wait_seconds = min(wait_seconds, max_wait - elapsed)
        if wait_seconds <= 0:
            return snapshot_decision(decision, runner_state)
        sleep_fn(wait_seconds)
        elapsed += wait_seconds


def set_output(name, value):
    out = os.environ.get("GITHUB_OUTPUT")
    rendered = json.dumps(value) if name == "runs-on" else str(value)
    if out:
        with open(out, "a") as f:
            print(f"{name}={rendered}", file=f)
    else:
        print(f"{name}={rendered}")


def write_summary(decision):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    tier = decision.get("tier_runner") or decision.get("tier") or ""
    cost_tier = decision.get("cost_tier")
    tier_display = f"{tier} ({cost_tier})" if tier and cost_tier else tier
    queued = decision.get("queued_seconds", 0)
    threshold = decision.get("threshold_seconds", 0)
    with open(summary_path, "a") as f:
        print("### CI runner routing", file=f)
        print("", file=f)
        print("| Lane | Provider | Tier | Reason | Queued / threshold seconds |", file=f)
        print("| --- | --- | --- | --- | --- |", file=f)
        print(
            f"| {decision['lane']} | {decision['provider']} | {tier_display} | "
            f"{decision['reason']} | {queued} / {threshold} |",
            file=f,
        )


def main(argv):
    policy_path = os.environ.get("INPUT_POLICY_PATH", "ci/runner-routing.toml")
    state_path = os.environ.get("INPUT_RUNNER_STATE_PATH", "")
    lane = os.environ["INPUT_LANE"]
    repo = os.environ.get("INPUT_REPO", "")
    job = os.environ.get("INPUT_JOB", "")
    owner = os.environ.get("INPUT_OWNER", os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    github_token = os.environ.get("INPUT_GITHUB_TOKEN", "")
    policy = load_policy(policy_path)
    runner_state = {"lanes": {}}
    if state_path:
        with open(state_path) as f:
            runner_state = json.load(f)
    if state_path:
        decision = resolve(policy, lane, repo, job, runner_state)
    elif github_token and owner and repo:
        decision = resolve_after_wait(policy, lane, repo, job, GithubClient(github_token), owner)
    else:
        decision = resolve(policy, lane, repo, job, runner_state)
    snapshot_path = os.environ.get("INPUT_DECISION_SNAPSHOT_PATH")
    if snapshot_path and "observation" in decision:
        # Shadow sink: the observer joins this immutable snapshot to Actions'
        # later host assignment. When maxi-daemon owns placement, replace the
        # file/artifact transport with mesh ingestion, not the record schema.
        try:
            Path(snapshot_path).write_text(json.dumps(decision, sort_keys=True) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            print(f"shadow snapshot write unavailable: {exc}", file=sys.stderr)
    set_output("runs-on", decision["runs_on"])
    set_output("provider", decision["provider"])
    set_output("lane", decision["lane"])
    set_output("reason", decision["reason"])
    set_output("tier", decision.get("tier", ""))
    set_output("cost_tier", decision.get("cost_tier", ""))
    write_summary(decision)
    print(json.dumps(decision, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
