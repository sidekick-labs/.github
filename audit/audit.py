#!/usr/bin/env python3
"""
GitHub Actions waste audit for sidekick-labs (CI port).

Ported from the local `/actions-audit` skill (.claude/skills/actions-audit/audit.py)
for org-level CI use. Differences from the skill:

- Paths are parameterized: the output dir defaults to ``$GITHUB_WORKSPACE/tmp/audit``
  (overridable with ``--output-dir``); there is no hardcoded local report path and
  no committed ``.actions-reports/``. Week-over-week deltas are handled by the
  workflow via ``actions/cache`` (it stages the previous run's ``audit.json`` and
  passes it with ``--prev``), not by a ``latest.json`` on disk.
- Auth comes from the ``GH_TOKEN`` env var (the minted GitHub App installation
  token), which the ``gh`` CLI reads automatically — no local ``gh auth login``.
- Emits findings JSON as the primary artifact (consumed by recommendations.py).
  The markdown report is still produced for the run summary / human review.

The four lenses (burners, failed-minutes, flakes, cancellations) are unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Iterable

ORG = os.environ.get("AUDIT_ORG", "sidekick-labs")
CORE_REPOS = [
    "sidekick-web",
    "sidekick-harness",
    "sidekick-rdp-client",
    "sidekick-admin-kit",
    "sidekick-companion-kit",
]


def gh_api(path: str, paginate: bool = False) -> list | dict:
    cmd = ["gh", "api", path]
    if paginate:
        cmd.insert(2, "--paginate")
        cmd.insert(3, "--slurp")
    # gh reads GH_TOKEN from the environment automatically; no local auth needed.
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {result.stderr.strip()}")
    data = json.loads(result.stdout) if result.stdout.strip() else []
    if paginate and isinstance(data, list) and data:
        if isinstance(data[0], list):
            flat: list = []
            for page in data:
                flat.extend(page)
            return flat
        if isinstance(data[0], dict):
            merged: dict = {}
            for page in data:
                for k, v in page.items():
                    if isinstance(v, list):
                        merged.setdefault(k, []).extend(v)
                    else:
                        merged[k] = v
            return merged
    return data


def discover_repos(scope: str) -> list[str]:
    if scope == "core5":
        return CORE_REPOS
    repos = gh_api(f"/orgs/{ORG}/repos?per_page=100", paginate=True)
    if isinstance(repos, dict):
        repos = repos.get("items", [])
    names = [r["name"] for r in repos if not r.get("archived")]
    return sorted(names)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_workflow_names(repo: str) -> dict[int, str]:
    """Return {workflow_id: canonical_name} for a repo."""
    try:
        data = gh_api(f"/repos/{ORG}/{repo}/actions/workflows?per_page=100", paginate=True)
    except RuntimeError:
        return {}
    workflows = data.get("workflows", []) if isinstance(data, dict) else []
    return {w["id"]: w["name"] for w in workflows}


def fetch_workflow_paths(repo: str) -> dict[int, str]:
    """Return {workflow_id: .github/workflows/<file>.yml} for a repo.

    The workflow file path is what recommendations.py needs to target a fix
    (and to derive a stable rec id). The skill version did not need this.
    """
    try:
        data = gh_api(f"/repos/{ORG}/{repo}/actions/workflows?per_page=100", paginate=True)
    except RuntimeError:
        return {}
    workflows = data.get("workflows", []) if isinstance(data, dict) else []
    return {w["id"]: w.get("path", "") for w in workflows}


def fetch_runs(repo: str, since: datetime) -> list[dict]:
    iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    path = (
        f"/repos/{ORG}/{repo}/actions/runs"
        f"?created=>={iso}&per_page=100&exclude_pull_requests=false"
    )
    data = gh_api(path, paginate=True)
    if isinstance(data, dict):
        return data.get("workflow_runs", [])
    return []


def fetch_jobs(repo: str, run_id: int) -> list[dict]:
    path = f"/repos/{ORG}/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    data = gh_api(path, paginate=True)
    if isinstance(data, dict):
        return data.get("jobs", [])
    return []


@dataclass
class WorkflowStats:
    repo: str
    workflow_id: int
    name: str
    path: str = ""
    runs: int = 0
    success: int = 0
    failure: int = 0
    cancelled: int = 0
    timed_out: int = 0
    other: int = 0
    total_minutes: float = 0.0
    failed_minutes: float = 0.0
    cancelled_minutes: float = 0.0
    durations_min: list[float] = field(default_factory=list)
    flake_count: int = 0
    failing_job_names: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def slug(self) -> str:
        return f"{self.repo}/{self.name}"

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "workflow_id": self.workflow_id,
            "name": self.name,
            "path": self.path,
            "runs": self.runs,
            "success": self.success,
            "failure": self.failure,
            "cancelled": self.cancelled,
            "timed_out": self.timed_out,
            "other": self.other,
            "total_minutes": round(self.total_minutes, 1),
            "failed_minutes": round(self.failed_minutes, 1),
            "cancelled_minutes": round(self.cancelled_minutes, 1),
            "avg_minutes": round(mean(self.durations_min), 1) if self.durations_min else 0,
            "p95_minutes": round(percentile(self.durations_min, 95), 1) if self.durations_min else 0,
            "failure_rate": round(self.failure / self.runs, 3) if self.runs else 0,
            "cancel_rate": round(self.cancelled / self.runs, 3) if self.runs else 0,
            "flake_count": self.flake_count,
            "top_failing_jobs": sorted(
                self.failing_job_names.items(), key=lambda x: -x[1]
            )[:3],
        }


def percentile(values: Iterable[float], p: int) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def run_minutes(run: dict) -> float:
    start = parse_iso(run.get("run_started_at") or run.get("created_at"))
    end = parse_iso(run.get("updated_at"))
    if not start or not end:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 60)


def has_active_runs(repo: str, since: datetime) -> bool:
    iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    path = f"/repos/{ORG}/{repo}/actions/runs?created=>={iso}&per_page=1"
    try:
        data = gh_api(path)
    except RuntimeError:
        return False
    if isinstance(data, dict):
        return data.get("total_count", 0) > 0
    return False


def collect(scope: str, window_days: int, deep_failures: bool) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    print(f"[discover] scope={scope} window={window_days}d since={since.isoformat()}", file=sys.stderr)

    candidate_repos = discover_repos(scope)
    print(f"[discover] {len(candidate_repos)} candidate repos", file=sys.stderr)

    if scope == "auto" or scope == "all":
        active = []
        for r in candidate_repos:
            if has_active_runs(r, since):
                active.append(r)
        repos = active
        print(f"[discover] {len(repos)} active repos in window", file=sys.stderr)
    else:
        repos = candidate_repos

    by_workflow: dict[tuple[str, int], WorkflowStats] = {}
    flake_groups: dict[tuple[str, int, str], list[dict]] = defaultdict(list)
    total_runs = 0

    for repo in repos:
        wf_names = fetch_workflow_names(repo)
        wf_paths = fetch_workflow_paths(repo)
        runs = fetch_runs(repo, since)
        print(f"[{repo}] {len(runs)} runs", file=sys.stderr)
        for run in runs:
            total_runs += 1
            wf_id = run.get("workflow_id")
            wf_name = wf_names.get(wf_id) or run.get("name") or run.get("path", "?")
            wf_path = wf_paths.get(wf_id) or run.get("path", "")
            key = (repo, wf_id)
            stats = by_workflow.get(key) or WorkflowStats(repo, wf_id, wf_name, wf_path)
            by_workflow[key] = stats

            conclusion = run.get("conclusion") or run.get("status")
            minutes = run_minutes(run)
            stats.runs += 1
            stats.total_minutes += minutes
            stats.durations_min.append(minutes)

            if conclusion == "success":
                stats.success += 1
            elif conclusion == "failure":
                stats.failure += 1
                stats.failed_minutes += minutes
            elif conclusion == "cancelled":
                stats.cancelled += 1
                stats.cancelled_minutes += minutes
            elif conclusion == "timed_out":
                stats.timed_out += 1
                stats.failed_minutes += minutes
            else:
                stats.other += 1

            sha = run.get("head_sha")
            if sha:
                flake_groups[(repo, wf_id, sha)].append({
                    "id": run["id"],
                    "attempt": run.get("run_attempt", 1),
                    "conclusion": conclusion,
                })

    # Flake detection: same (repo, workflow, sha) with at least one failure and one later success
    for (repo, wf_id, sha), attempts in flake_groups.items():
        if len(attempts) < 2:
            continue
        attempts.sort(key=lambda x: x["attempt"])
        had_failure = any(a["conclusion"] in ("failure", "timed_out") for a in attempts[:-1])
        ended_success = attempts[-1]["conclusion"] == "success"
        if had_failure and ended_success:
            stats = by_workflow.get((repo, wf_id))
            if stats:
                stats.flake_count += 1

    # Deep inspection: for failed runs in top offenders, fetch jobs to attribute failure
    if deep_failures:
        # Pick workflows with >=3 failures
        top_failing = sorted(
            [s for s in by_workflow.values() if s.failure >= 3],
            key=lambda s: -s.failed_minutes,
        )[:10]
        for stats in top_failing:
            iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            path = (
                f"/repos/{ORG}/{stats.repo}/actions/runs"
                f"?workflow_id={stats.workflow_id}&status=failure"
                f"&created=>={iso}&per_page=20"
            )
            try:
                data = gh_api(path)
                failed_runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
            except RuntimeError:
                continue
            for run in failed_runs[:10]:  # cap to 10 jobs-fetches per workflow
                try:
                    jobs = fetch_jobs(stats.repo, run["id"])
                except RuntimeError:
                    continue
                for job in jobs:
                    if job.get("conclusion") == "failure":
                        stats.failing_job_names[job.get("name", "?")] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "scope": scope,
        "org": ORG,
        "since": since.isoformat(),
        "repos_audited": repos,
        "total_runs": total_runs,
        "total_minutes": round(sum(s.total_minutes for s in by_workflow.values()), 1),
        "workflows": [s.to_dict() for s in by_workflow.values()],
    }


def md_table(rows: list[list[str]], header: list[str]) -> str:
    if not rows:
        return "_no data_\n"
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join("---" for _ in header) + "|")
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines) + "\n"


def render_report(data: dict, prev: dict | None) -> str:
    workflows = data["workflows"]
    total_min = data["total_minutes"]
    total_runs = data["total_runs"]
    failure_min = sum(w["failed_minutes"] for w in workflows)
    cancel_min = sum(w["cancelled_minutes"] for w in workflows)
    wasted_min = failure_min + cancel_min
    waste_pct = (wasted_min / total_min * 100) if total_min else 0

    delta = ""
    if prev:
        prev_min = prev.get("total_minutes", 0)
        prev_runs = prev.get("total_runs", 0)
        d_min = total_min - prev_min
        d_runs = total_runs - prev_runs
        delta = (
            f"\n**WoW delta:** runs {d_runs:+d} ({d_runs/prev_runs*100:+.1f}% vs {prev_runs}), "
            f"minutes {d_min:+.0f} ({d_min/prev_min*100:+.1f}% vs {prev_min:.0f})\n"
            if prev_runs and prev_min else ""
        )

    # Lens 1: burners — top by total minutes
    burners = sorted(workflows, key=lambda w: -w["total_minutes"])[:10]
    burners_rows = [
        [w["repo"], w["name"], f"{w['total_minutes']:.0f}", w["runs"],
         f"{w['avg_minutes']:.1f}", f"{w['p95_minutes']:.1f}"]
        for w in burners
    ]

    # Lens 2: failed-minutes
    failures = sorted(
        [w for w in workflows if w["failed_minutes"] > 0],
        key=lambda w: -w["failed_minutes"],
    )[:10]
    failure_rows = [
        [w["repo"], w["name"], f"{w['failed_minutes']:.0f}",
         f"{w['failure']}/{w['runs']}", f"{w['failure_rate']*100:.1f}%",
         ", ".join(f"{n} ({c})" for n, c in w["top_failing_jobs"][:2]) or "—"]
        for w in failures
    ]

    # Lens 3: flakes
    flakes = sorted(
        [w for w in workflows if w["flake_count"] > 0],
        key=lambda w: -w["flake_count"],
    )[:10]
    flake_rows = [
        [w["repo"], w["name"], w["flake_count"], w["runs"],
         f"{w['flake_count']/w['runs']*100:.1f}%"]
        for w in flakes
    ]

    # Lens 4: cancellations
    cancels = sorted(
        [w for w in workflows if w["cancelled"] >= 3 and w["cancel_rate"] >= 0.1],
        key=lambda w: -w["cancelled_minutes"],
    )[:10]
    cancel_rows = [
        [w["repo"], w["name"], w["cancelled"], f"{w['cancel_rate']*100:.1f}%",
         f"{w['cancelled_minutes']:.0f}"]
        for w in cancels
    ]

    return f"""# Actions Audit — {data['generated_at'][:10]}

**Window:** {data['window_days']} days (since {data['since'][:10]})
**Scope:** {data['scope']} ({len(data['repos_audited'])} active repos)
**Totals:** {total_runs:,} runs · {total_min:,.0f} minutes · {waste_pct:.1f}% wasted on failures/cancellations
{delta}
---

## Lens 1 — Burners (highest total minutes)

Where the bill comes from. High-volume + long-running workflows. Reductions here have the largest dollar impact.

{md_table(burners_rows, ["repo", "workflow", "total_min", "runs", "avg_min", "p95_min"])}

## Lens 2 — Failed minutes (wasted by failures)

Failures × avg failed-run duration. The "long failures" are worst — they consume minutes before catching the problem.

{md_table(failure_rows, ["repo", "workflow", "failed_min", "failures", "rate", "top failing jobs"])}

## Lens 3 — Flakes (same-SHA retry-then-success)

Runs that failed initially and succeeded on retry of the *same commit*. These are pure waste — the code wasn't actually broken.

{md_table(flake_rows, ["repo", "workflow", "flake_count", "total_runs", "flake_rate"])}

## Lens 4 — Cancellations (often missing concurrency groups)

High cancel rates usually mean PRs trigger multiple in-flight runs. Add a `concurrency:` block keyed on PR ref with `cancel-in-progress: true`.

{md_table(cancel_rows, ["repo", "workflow", "cancelled", "rate", "wasted_min"])}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", choices=["all", "core5", "auto"], default="auto")
    ap.add_argument("--window", type=int, default=7, help="Window in days")
    ap.add_argument("--no-deep", action="store_true",
                    help="Skip per-job fetches for failure attribution")
    ap.add_argument(
        "--output-dir",
        default=os.path.join(os.environ.get("GITHUB_WORKSPACE", "."), "tmp", "audit"),
        help="Directory for audit.json + report.md (default: $GITHUB_WORKSPACE/tmp/audit).",
    )
    ap.add_argument(
        "--prev",
        default="",
        help="Path to a previous run's audit.json for WoW deltas (the workflow "
             "stages this from actions/cache). Optional.",
    )
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = collect(args.scope, args.window, deep_failures=not args.no_deep)

    prev = None
    if args.prev:
        prev_path = Path(args.prev)
        if prev_path.exists():
            try:
                prev = json.loads(prev_path.read_text())
            except json.JSONDecodeError:
                prev = None

    json_path = out_dir / "audit.json"
    md_path = out_dir / "report.md"
    json_path.write_text(json.dumps(data, indent=2))
    md_path.write_text(render_report(data, prev))

    # Print the findings JSON path on stdout (consumed by recommendations.py /
    # the workflow). Diagnostics go to stderr.
    print(str(json_path))


if __name__ == "__main__":
    main()
