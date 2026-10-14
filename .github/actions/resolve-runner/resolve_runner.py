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


class GithubApiError(RuntimeError):
    pass


def load_policy(policy_path):
    if tomllib is None:
        raise RuntimeError("resolve-runner requires Python 3.11+ for tomllib support")
    with open(policy_path, "rb") as f:
        return tomllib.load(f)


def repo_job_override(policy, repo, job):
    return policy.get("repos", {}).get(repo, {}).get("jobs", {}).get(job, {})


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
    return int(
        policy["defaults"].get(
            "max_resolver_wait_seconds", policy["defaults"]["queue_wait_seconds"]
        )
    )


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


def self_hosted_allowed(policy, lane_cfg, repo, job):
    return bool(
        effective_value(policy, lane_cfg, repo, job, "self_hosted_allowed", True)
    )


# Representative duration (seconds) assumed for a job this resolver has no
# p50 data for, when estimating lane queue minutes for overflow_to. Better to
# over-estimate a stranger job's cost (biasing AGAINST overflow) than to
# under-estimate it and overflow work the Mac pool cannot actually absorb in
# time.
DEFAULT_JOB_P50_SECONDS = 600


def job_requested_labels(job):
    return {str(label).lower() for label in job.get("labels", []) or []}


def job_matches_lane(job, lane_cfg):
    """A queued/running job's requested `runs-on` labels, compared for EXACT
    equality against a lane's self_hosted set.

    Equality, not subset: resolve_runner emits exactly `lane_cfg["self_hosted"]`
    as `runs_on` when a job is routed self-hosted, so a job queued/running for
    lane X carries exactly X's label set. A subset match would also catch jobs
    actually routed to a narrower sibling lane (e.g. macos-arm64-heavy, which
    shares macos-arm64's labels plus more), double-counting them against the
    broader lane's estimate.
    """
    have = job_requested_labels(job)
    want = {str(label).lower() for label in lane_cfg.get("self_hosted", [])}
    return bool(want) and have == want


def estimate_job_seconds(policy, repo, job_name, lane_cfg):
    job_cfg = repo_job_override(policy, repo, job_name)
    p50 = job_cfg.get("p50_seconds")
    if p50 is None:
        p50 = lane_cfg.get("p50_seconds")
    if p50 is None:
        p50 = policy.get("defaults", {}).get("p50_seconds")
    if p50 is None:
        p50 = DEFAULT_JOB_P50_SECONDS
    return int(p50)


def lane_queue_minutes(policy, repo, lane_cfg, snapshot):
    """Estimated minutes of CI work still owed to a lane: every queued job's
    full p50, plus every running job's REMAINING p50 (p50 minus elapsed,
    floored at zero so a job already past its p50 contributes nothing extra
    -- it is assumed to finish imminently rather than overrun forever).

    `snapshot` is one probe's view of the queue, structured as
    {"queued": [...], "running": [...], "now": datetime}. The caller builds
    it once from `client.lane_job_estimates(owner, repo)` and reuses it
    across every lane so a single probe's fleet matches every lane's
    estimate -- a per-lane probe would describe a different fleet than the
    one the lane selector saw.
    """
    now = snapshot["now"]
    queued_jobs = snapshot.get("queued", [])
    running_jobs = snapshot.get("running", [])
    total_seconds = 0
    for job in queued_jobs:
        if job_matches_lane(job, lane_cfg):
            total_seconds += estimate_job_seconds(
                policy, repo, job.get("name", ""), lane_cfg
            )
    for job in running_jobs:
        if not job_matches_lane(job, lane_cfg):
            continue
        p50 = estimate_job_seconds(policy, repo, job.get("name", ""), lane_cfg)
        started = job.get("started_at")
        elapsed = 0
        if started:
            try:
                elapsed = max(
                    0, int((now - parse_github_time(started)).total_seconds())
                )
            except ValueError:
                elapsed = 0
        total_seconds += max(0, p50 - elapsed)
    return total_seconds / 60.0


def overflow_targets_for(policy, lane_cfg, repo, job):
    return list(effective_value(policy, lane_cfg, repo, job, "overflow_to", []) or [])


def overflow_decision(policy, selected_lane, runner_state, target_lane):
    """Pick the lane a Linux-capable job overflows onto, or None.

    ONLY when all three hold at plan time:
      (a) the source lane's self-hosted pool is fully occupied (idle == 0),
      (b) the target lane's estimated queue minutes <= half the source
          lane's estimated queue minutes,
      (c) the target lane actually has an idle runner right now (a low
          queue_minutes estimate is NOT the same thing as idle capacity --
          a running job past its p50 contributes 0 to the estimate but
          its box is still busy).

    `policy` is the runner-routing policy, `selected_lane` is the source
    lane, `runner_state` is one probe's view of the fleet (the SAME
    probe used by the lane selector -- a later probe would describe a
    different fleet, not the decision-time candidates), and `target_lane`
    is the lane the policy declared via `overflow_to`. Returns
    (source_minutes, target_minutes) when overflow applies, so resolve()
    builds the decision alongside its other branches; None means fall
    through to the existing self-hosted decision.

    This is the function a single declared `overflow_to` target reaches;
    the loop over the declared targets and the per-target lane lookup
    happen in resolve() so the function stays short and the parameter
    count stays under qlty:function-parameters' threshold.

    Implementation uses guard-style: each precondition gates the next via
    an early return, so the qualifying branch sits at the bottom with no
    nesting. qlty:return-statements flags the seven returns, but each
    one is a pre-condition check whose else-branch is "fall through" --
    inverting them into a single boolean returns a deeply nested if/else
    tree that is harder to read, not easier.
    """
    lanes_state = runner_state.get("lanes", {})
    source_state = lanes_state.get(selected_lane, {})
    if int(source_state.get("idle", 0)) > 0:
        return None
    source_minutes = source_state.get("queue_minutes")
    target_cfg = policy["lanes"].get(target_lane)
    target_state = lanes_state.get(target_lane, {})
    if (
        source_minutes is None
        or target_cfg is None
        or int(target_state.get("idle", 0)) <= 0
        or (target_minutes := target_state.get("queue_minutes")) is None
    ):
        return None
    if target_minutes <= 0.5 * source_minutes:
        return source_minutes, target_minutes
    return None


def self_hosted_disallowed_decision(policy, lane, repo, job):
    # The policy says self-hosted is not permitted and the action no longer
    # knows any other runner. A `self_hosted_allowed: false` lane is a
    # misconfiguration now -- there is no second-class runner to fall to --
    # so refuse it where the caller's decision is read, instead of returning
    # a label set that can never match.
    selected_lane = effective_lane(policy, lane, repo, job)
    raise ValueError(
        f"lane {selected_lane} disallows self-hosted runners and resolve-runner "
        f"no longer routes to any other"
    )


def resolve(policy, lane, repo, job, runner_state):
    selected_lane = effective_lane(policy, lane, repo, job)
    lane_cfg = policy["lanes"][selected_lane]
    if not self_hosted_allowed(policy, lane_cfg, repo, job):
        # Returning a label set the resolver will never match is the same
        # dead-queue outcome as raising; the loop below translates it into
        # the explicit refusal above the moment the caller tries to schedule.
        # Carrying the decision in the same shape every other decision takes
        # keeps `set_output` honest.
        threshold = effective_threshold(policy, lane_cfg, repo, job)
        return {
            "lane": selected_lane,
            "runs_on": lane_cfg["self_hosted"],
            "provider": "self-hosted",
            "reason": "self-hosted-disallowed",
            "threshold_seconds": threshold,
            "queued_seconds": 0,
        }
    threshold = effective_threshold(policy, lane_cfg, repo, job)
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
    prefer_applies = (
        bool(prefer) and idle_preferred > 0 and queued_seconds <= prefer_max_queued
    )
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

    # Linux-saturated overflow onto an idle Mac pool (ONE fix, 2026-10-14).
    # Only reached once the lane's own self-hosted pool is fully occupied
    # (idle == 0, checked above) -- "stay on Linux" is the default outcome,
    # this is the exception. Declared per lane/job via `overflow_to` in
    # policy; never inferred here, and gated a second time inside
    # overflow_decision on the target lane's estimated queue being <= half
    # the source lane's AND the target lane having an idle runner. No
    # paid/hosted fallback exists or will be added here (operator check
    # 04:15Z on PR #98): overflow goes only to idle self-hosted Macs; if
    # none is free the job waits on its own lane's self_hosted label set.
    overflow_targets = overflow_targets_for(policy, lane_cfg, repo, job)
    for target_lane in overflow_targets:
        result = overflow_decision(policy, selected_lane, runner_state, target_lane)
        if result is None:
            continue
        source_minutes, target_minutes = result
        return {
            "lane": selected_lane,
            "runs_on": policy["lanes"][target_lane]["self_hosted"],
            "provider": "self-hosted",
            "reason": "linux-saturated-mac-overflow",
            "threshold_seconds": threshold,
            "queued_seconds": queued_seconds,
            "overflow_lane": target_lane,
            "overflow_source_minutes": round(source_minutes, 2),
            "overflow_target_minutes": round(target_minutes, 2),
        }

    # No idle self-hosted runner and no other runner to spill to. The job must
    # queue on the lane's self_hosted label set; returning that set here means
    # GitHub Actions queues the job rather than the action having to own the
    # wait, and `resolve_after_wait`'s polling only shortens that queue.
    return {
        "lane": selected_lane,
        "runs_on": lane_cfg["self_hosted"],
        "provider": "self-hosted",
        "reason": "saturated-self-hosted-pool",
        "threshold_seconds": threshold,
        "queued_seconds": queued_seconds,
    }


def api_unavailable_decision(policy, lane, repo, job, error):
    # Without a probe we cannot prove any matching self-hosted runner exists,
    # and there is no paid or GitHub-hosted runner to fall to. Returning the
    # lane's `self_hosted` label set is the same decision `resolve()` reaches
    # under saturation; if the lane is genuinely empty the caller pays the
    # wait, which is the policy now. (Previously this path preferred a paid
    # runner when one was declared, per maxi-config#162; with the paid
    # fallback removed, the trusted-but-empty lane is the only honest answer.)
    selected_lane = effective_lane(policy, lane, repo, job)
    lane_cfg = policy["lanes"][selected_lane]
    if not self_hosted_allowed(policy, lane_cfg, repo, job):
        return self_hosted_disallowed_decision(policy, lane, repo, job)
    threshold = effective_threshold(policy, lane_cfg, repo, job)
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
        parse_github_time(run["created_at"]) for run in runs if run.get("created_at")
    ]
    if not created:
        return 0
    return max(0, int((now - min(created)).total_seconds()))


class GithubClient:
    def __init__(self, token, api_url=None):
        self.token = token
        self.api_url = (
            api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com"
        ).rstrip("/")

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

    def running_runs(self, owner, repo):
        """In-progress runs, so the Linux-overflow estimate can subtract
        elapsed time from a running job's p50 rather than treating it as a
        fresh queue entry (ci/runner-routing.toml overflow_to)."""
        runs = []
        page = 1
        while True:
            data = self.api_json(
                f"/repos/{owner}/{repo}/actions/runs",
                {"status": "in_progress", "per_page": "100", "page": str(page)},
            )
            batch = data.get("workflow_runs", [])
            if not batch:
                break
            runs.extend(batch)
            page += 1
        return runs

    def run_jobs(self, owner, repo, run_id):
        """Per-JOB detail (requested labels, started_at) for one run.

        A workflow run has no `labels` of its own -- labels are per-job, so
        the overflow estimate needs this to know what a queued/running run
        is actually asking to run on. Cheap relative to the run list: called
        once per queued/running run, not per poll tick, and only inside
        `lane_job_estimates` which the resolver calls once per plan.
        """
        jobs = []
        page = 1
        while True:
            data = self.api_json(
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                {"per_page": "100", "page": str(page)},
            )
            batch = data.get("jobs", [])
            if not batch:
                break
            jobs.extend(batch)
            page += 1
        return jobs

    def lane_job_estimates(self, owner, repo):
        """-> (queued_jobs, running_jobs), each a list of
        {"name", "labels", "started_at"} job-level dicts.

        Resolves the run-level queued/in_progress lists down to job-level
        label sets, which is what lane matching actually needs -- a run can
        contain jobs destined for several different lanes.

        Jobs are classified by their OWN status, not their run's: a run turns
        `in_progress` as soon as its first job starts, so an in_progress run
        routinely still holds `queued` jobs waiting for a runner. Taking only
        the in_progress jobs from those runs dropped that pending work and
        under-estimated the lane's queue.
        """
        queued_jobs, running_jobs = [], []
        for run in self.queued_runs(owner, repo):
            for job in self.run_jobs(owner, repo, run["id"]):
                if job.get("status") == "queued":
                    queued_jobs.append(
                        {
                            "name": job.get("name", ""),
                            "labels": job.get("labels", []),
                            "started_at": job.get("started_at"),
                        }
                    )
        for run in self.running_runs(owner, repo):
            for job in self.run_jobs(owner, repo, run["id"]):
                if job.get("status") == "in_progress":
                    running_jobs.append(
                        {
                            "name": job.get("name", ""),
                            "labels": job.get("labels", []),
                            "started_at": job.get("started_at"),
                        }
                    )
        return queued_jobs, running_jobs


def probe_runner_state(policy, client, owner, repo, now=None):
    now = now or datetime.now(timezone.utc)
    runners = client.org_runners(owner)
    queued_runs = client.queued_runs(owner, repo)
    queued_seconds = oldest_queued_seconds(queued_runs, now)
    # Job-level label/timing visibility is optional: a client written before
    # this fix (or a test double exercising the pre-overflow surface) need
    # not implement it, and an estimate with no job-level data simply never
    # overflows -- lane_queue_minutes() sees empty lists and every lane's
    # queue_minutes comes out 0, so overflow_decision's "target <= half
    # source" gate can still fire spuriously at 0 <= 0. Guard explicitly:
    # no job-level data means "stay on Linux", not "always overflow".
    if hasattr(client, "lane_job_estimates"):
        queued_jobs, running_jobs = client.lane_job_estimates(owner, repo)
        have_job_estimates = True
        snapshot = {"queued": queued_jobs, "running": running_jobs, "now": now}
    else:
        have_job_estimates = False
        snapshot = None
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
        if have_job_estimates:
            lanes[lane]["queue_minutes"] = lane_queue_minutes(
                policy,
                repo,
                lane_cfg,
                snapshot,
            )
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
                    {
                        "id": r["name"],
                        "busy": r.get("busy", False),
                        "labels": sorted(runner_labels(r)),
                    }
                    for r in state["inventory"]
                    if r.get("status") == "online"
                    and runner_has_labels(r, decision["runs_on"])
                ],
            }
        except (KeyError, TypeError, ValueError) as exc:
            # Observation must never turn an otherwise valid route into a
            # failed plan when a fleet API field is malformed.
            decision.pop("observation", None)
            print(f"shadow snapshot unavailable: {exc}", file=sys.stderr)
    return decision


def resolve_after_wait(
    policy, lane, repo, job, client, owner, poll_seconds=15, sleep_fn=time.sleep
):
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

        # A non-saturated decision means an idle self-hosted runner was found
        # -- return it. The poll loop's only job is to shorten the time a job
        # spends in the GitHub Actions self-hosted wait pool.
        if decision["reason"] != "saturated-self-hosted-pool":
            return snapshot_decision(decision, runner_state)

        # Pool is saturated. Cap the polling by the lane's threshold and the
        # caller's `max_resolver_wait_seconds`. Past either, return the
        # saturated decision and let GitHub Actions queue the job on the
        # self-hosted label set: that is the "WAITS on self-hosted" the
        # policy guarantees when no self-hosted runner is free.
        lane_cfg = policy["lanes"][selected_lane]
        max_wait = effective_max_resolver_wait(policy, lane_cfg, repo, job)

        # Fold the four "we are done polling" reasons into one branch:
        # whether to keep waiting or return the saturated decision.
        remaining_threshold = decision["threshold_seconds"] - decision["queued_seconds"]
        remaining_max_wait = max_wait - elapsed
        should_keep_polling = (
            elapsed < max_wait and remaining_threshold > 0 and remaining_max_wait > 0
        )
        if not should_keep_polling:
            return snapshot_decision(decision, runner_state)

        wait_seconds = min(poll_seconds, remaining_threshold, remaining_max_wait)
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
    queued = decision.get("queued_seconds", 0)
    threshold = decision.get("threshold_seconds", 0)
    with open(summary_path, "a") as f:
        print("### CI runner routing", file=f)
        print("", file=f)
        print("| Lane | Provider | Reason | Queued / threshold seconds |", file=f)
        print("| --- | --- | --- | --- |", file=f)
        print(
            f"| {decision['lane']} | {decision['provider']} | "
            f"{decision['reason']} | {queued} / {threshold} |",
            file=f,
        )
        if decision["reason"] == "linux-saturated-mac-overflow":
            # Log every overflow decision (lane, both estimates) in the plan job
            # output so an operator reading the summary sees WHY the lane was
            # left and WHAT target it overflowed to. Acceptance from the
            # review-gate overflow card: "log every overflow decision".
            print("", file=f)
            print(
                f"Overflowed `{decision['lane']}` -> `{decision['overflow_lane']}`: "
                f"source estimate {decision['overflow_source_minutes']} min, "
                f"target estimate {decision['overflow_target_minutes']} min "
                f"(target <= 0.5 x source).",
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
        decision = resolve_after_wait(
            policy, lane, repo, job, GithubClient(github_token), owner
        )
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
    write_summary(decision)
    print(json.dumps(decision, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
