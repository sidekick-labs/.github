#!/usr/bin/env python3
"""
Partition actions-audit findings into deduped `ci-audit` issue recs (the SENSOR).

This is the SENSOR half of the CI self-healing loop (DEC-OCTO-0003, sre-brain#109):
a deterministic scan that turns EVERY actionable finding into a `ci-audit` issue in
the org's CI-health tracker (sidekick-labs/sre-brain). It opens NO PRs — the fix is
the actuator's job (sre-brain's ci-fix-sweep + ci-fix-engine), which reads these
issues, proposes a guarded workflow fix, and opens a ready-for-review PR that links
back to the originating issue. Keeping the sensor PR-free is the whole point: one
auditable work unit per finding, fixed (or not) downstream where a human gates it.

(Previously this file also produced an `auto_pr` list — a narrow inline
`concurrency:` auto-fix PR. Per sre-brain#109 that path moved to the ci-fix engine,
so `concurrency` is now just another `judgment` finding category like flakes /
failed-minutes / burners. The `auto_pr`/`false_positives` lists are retained in the
output for shape stability but `auto_pr` is always empty.)

Findings (all routed to `ci-audit` issues):
  - concurrency:    a workflow with a >=10% cancel rate (>=3 cancels) and NO
                    top-level `concurrency:` block (the audit's Lens-4 threshold) —
                    the engine's most common fix.
  - flakes:         same-SHA retry-then-success flake(s); needs root-cause.
  - failed-minutes: failures that burn real minutes before failing.
  - burners:        top minute-consumers worth a human/engine look.

Each rec carries a STABLE rec id = short hash(repo, workflow file, category). The
workflow embeds a hidden `<!-- actions-audit:<rec-id> -->` marker in the `ci-audit`
issue body and searches for it before creating, so re-runs refresh rather than
stack duplicates (the issue-spine dedup discipline). Crucially, every rec carries
the TARGET REPO (`repo`, owner-stripped) AND the offending workflow file
(`workflow_file`, the `.github/workflows/<file>.yml` path) so the downstream
ci-fix engine knows exactly what to fix — open_issues.py renders these as
machine-parseable `repo:` / `workflow:` lines in the issue body.

Cheap pre-check (Skill Rule #1): for a `concurrency` rec we fetch the workflow's
raw YAML via `gh api` and, if a top-level `concurrency:` key is already present,
DROP the rec as a false positive before it ever becomes an issue. The engine
prompt repeats this verification at edit time; this is the inexpensive first gate.

Output: a single JSON object on stdout:
  {"auto_pr": [], "judgment": [...], "false_positives": [...]}
Diagnostics go to stderr. Auth: GH_TOKEN env (read by `gh`), same as audit.py.

The workflow's scope INCLUDES sidekick-labs/.github itself (locked decision), so
this repo's own reusable workflows are audited like any other.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

ORG = os.environ.get("AUDIT_ORG", "sidekick-labs")

# Lens-4 thresholds (mirror audit.py's cancellation lens exactly).
CANCEL_RATE_MIN = 0.10
CANCEL_COUNT_MIN = 3

# Finding category for the missing-concurrency pattern. The ci-fix engine reads
# the `suggested_fix` snippet below as a strong hint (it re-verifies before editing).
CAT_CONCURRENCY = "concurrency"

CONCURRENCY_SNIPPET = """concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true"""

# Matches a top-level `concurrency:` key (column 0, not nested under a job).
_TOP_LEVEL_CONCURRENCY = re.compile(r"(?m)^concurrency\s*:")


def rec_id(repo: str, workflow_file: str, change_type: str) -> str:
    """Stable short id for idempotency. `change_type` is the finding `category`
    (concurrency / flakes / failed-minutes / burners); the id is embedded as the
    `<!-- actions-audit:<rec-id> -->` marker in the `ci-audit` issue body and
    dedups on it across runs (refresh in place, never stack duplicates)."""
    h = hashlib.sha256(f"{repo}\0{workflow_file}\0{change_type}".encode()).hexdigest()
    return h[:12]


def gh_raw_workflow(repo: str, workflow_path: str) -> str | None:
    """Fetch a workflow file's raw text via the contents API. None on failure."""
    if not workflow_path:
        return None
    api = f"/repos/{ORG}/{repo}/contents/{workflow_path}"
    result = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github.raw+json", api],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[precheck] could not fetch {repo}/{workflow_path}: "
              f"{result.stderr.strip()}", file=sys.stderr)
        return None
    return result.stdout


def has_concurrency_block(yaml_text: str) -> bool:
    return bool(_TOP_LEVEL_CONCURRENCY.search(yaml_text))


def partition(data: dict) -> dict:
    workflows = data.get("workflows", [])
    auto_pr: list[dict] = []
    judgment: list[dict] = []
    false_positives: list[dict] = []

    for w in workflows:
        repo = w["repo"]
        name = w["name"]
        wf_path = w.get("path", "")

        # ---- Missing concurrency on a high-cancel workflow -> ci-audit issue.
        # The ci-fix engine (sre-brain) reads the issue, re-verifies the block is
        # genuinely missing, and opens a guarded ready-for-review PR. We still run
        # the cheap pre-check here so already-fixed workflows never become noise.
        if w.get("cancelled", 0) >= CANCEL_COUNT_MIN and w.get("cancel_rate", 0) >= CANCEL_RATE_MIN:
            note = (
                f"{name} in {repo} cancelled {w['cancelled']} runs "
                f"({w['cancel_rate']*100:.0f}% cancel rate), wasting "
                f"~{w.get('cancelled_minutes', 0):.0f} min. A top-level "
                f"`concurrency:` block keyed on the ref with "
                f"`cancel-in-progress: true` collapses superseded in-flight runs."
            )

            if not wf_path:
                # Can't locate the file (e.g. run.path was empty) -> still file a
                # ci-audit issue, but flag that the engine must locate the workflow.
                judgment.append({
                    "repo": repo,
                    "workflow_name": name,
                    "workflow_file": wf_path,
                    "category": CAT_CONCURRENCY,
                    "note": note + " NOTE: workflow file path unknown — the engine "
                            "must locate the offending workflow before editing.",
                    "metrics": {
                        "cancelled": w["cancelled"],
                        "cancel_rate": w["cancel_rate"],
                        "cancelled_minutes": w.get("cancelled_minutes", 0),
                    },
                })
            else:
                # Cheap pre-check (Skill Rule #1): if the YAML already has a
                # top-level concurrency block, this is a false positive — drop it
                # before it ever becomes a ci-audit issue.
                yaml_text = gh_raw_workflow(repo, wf_path)
                if yaml_text is not None and has_concurrency_block(yaml_text):
                    false_positives.append({
                        "repo": repo,
                        "workflow_name": name,
                        "workflow_file": wf_path,
                        "category": CAT_CONCURRENCY,
                        "reason": "top-level `concurrency:` block already present",
                    })
                else:
                    judgment.append({
                        "repo": repo,
                        "workflow_name": name,
                        "workflow_file": wf_path,
                        "category": CAT_CONCURRENCY,
                        "note": note,
                        # A concrete fix hint the engine treats as a strong
                        # suggestion (it re-verifies + yaml-validates before push).
                        "suggested_fix": CONCURRENCY_SNIPPET,
                        "metrics": {
                            "cancelled": w["cancelled"],
                            "cancel_rate": w["cancel_rate"],
                            "cancelled_minutes": w.get("cancelled_minutes", 0),
                        },
                    })

        # ---- Judgment-call signal -> GitHub-issue tracker (not a committed report).
        if w.get("flake_count", 0) > 0:
            judgment.append({
                "repo": repo,
                "workflow_name": name,
                "workflow_file": wf_path,
                "category": "flakes",
                "note": f"{w['flake_count']} same-SHA retry-then-success flake(s); "
                        f"needs root-cause (pin runner / retry-once / investigate).",
                "metrics": {"flake_count": w["flake_count"], "runs": w["runs"]},
                "top_failing_jobs": w.get("top_failing_jobs", []),
            })

        # Slow-failing workflows: failures that burn real minutes before failing.
        if w.get("failure", 0) >= CANCEL_COUNT_MIN and w.get("failed_minutes", 0) > 0:
            judgment.append({
                "repo": repo,
                "workflow_name": name,
                "workflow_file": wf_path,
                "category": "failed-minutes",
                "note": f"{w['failure']}/{w['runs']} runs failed, burning "
                        f"~{w['failed_minutes']:.0f} min; consider fail-fast job "
                        f"ordering or a path filter.",
                "metrics": {
                    "failure": w["failure"],
                    "failure_rate": w.get("failure_rate", 0),
                    "failed_minutes": w["failed_minutes"],
                },
                "top_failing_jobs": w.get("top_failing_jobs", []),
            })

    # Burners: top minute-consumers worth a human look (independent of failures).
    burners = sorted(workflows, key=lambda x: -x.get("total_minutes", 0))[:5]
    for w in burners:
        if w.get("total_minutes", 0) <= 0:
            continue
        judgment.append({
            "repo": w["repo"],
            "workflow_name": w["name"],
            "workflow_file": w.get("path", ""),
            "category": "burners",
            "note": f"top minute-burner: {w['total_minutes']:.0f} min over "
                    f"{w['runs']} runs (avg {w.get('avg_minutes', 0):.1f}m, "
                    f"p95 {w.get('p95_minutes', 0):.1f}m); review for macOS->ubuntu, "
                    f"matrix trim, caching, or PR-only triggers.",
            "metrics": {
                "total_minutes": w["total_minutes"],
                "runs": w["runs"],
                "avg_minutes": w.get("avg_minutes", 0),
                "p95_minutes": w.get("p95_minutes", 0),
            },
        })

    # Stamp every finding with a STABLE rec_id = hash(repo, workflow_file,
    # category). The workflow embeds this id as a hidden
    # `<!-- actions-audit:<rec-id> -->` marker in the `ci-audit` issue and dedups
    # on it across runs (refresh, don't stack). Stamped here (one place) rather
    # than at each of the append sites.
    for j in judgment:
        j["rec_id"] = rec_id(j["repo"], j.get("workflow_file", ""), j["category"])

    return {
        "generated_at": data.get("generated_at"),
        "window_days": data.get("window_days"),
        "scope": data.get("scope"),
        # `auto_pr` is retained for output-shape stability but is always empty:
        # the actuator (sre-brain ci-fix) now owns ALL fixes (sre-brain#109).
        "auto_pr": auto_pr,
        "judgment": judgment,
        "false_positives": false_positives,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("findings", help="Path to audit.py's findings JSON (audit.json).")
    ap.add_argument("--output", default="", help="Write the partition JSON here (default: stdout only).")
    ap.add_argument("--no-precheck", action="store_true",
                    help="Skip the gh contents pre-check for already-present concurrency blocks.")
    args = ap.parse_args()

    with open(args.findings) as f:
        data = json.load(f)

    if args.no_precheck:
        global gh_raw_workflow

        def gh_raw_workflow(repo, workflow_path):  # type: ignore[misc]
            return None

    result = partition(data)
    out = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(out)
    print(out)

    print(
        f"[recommendations] auto_pr={len(result['auto_pr'])} "
        f"judgment={len(result['judgment'])} "
        f"false_positives={len(result['false_positives'])}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
