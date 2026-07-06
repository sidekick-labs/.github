#!/usr/bin/env python3
"""Logic tests for the actions-audit noise-control + reconcile changes.

Self-contained, no network: loads recommendations.py / open_issues.py directly,
feeds synthetic findings, and asserts the issue-worthy/digest split, org-shared
coalescing, and the reconcile pass's safety rails (marker-scoped + scanned-scoped).
Run: `python3 tests/audit-recommendations-reconcile-logic.py` (exits non-zero on
failure). Not wired into PR CI yet — see the PR for the follow-up suggestion.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rec = _load("recommendations", "audit/recommendations.py")
oi = _load("open_issues", "audit/open_issues.py")


def wf(repo, name, **kw):
    base = dict(repo=repo, name=name, path=f".github/workflows/{name.lower().replace(' ', '-')}.yml",
                runs=20, failure=0, failed_minutes=0, failure_rate=0.0,
                cancelled=0, cancel_rate=0.0, cancelled_minutes=0,
                flake_count=0, total_minutes=10, avg_minutes=1, p95_minutes=2)
    base.update(kw)
    return base


def test_split_thresholds():
    # No gh precheck for concurrency in tests.
    rec.gh_raw_workflow = lambda r, p: None
    data = {"generated_at": "t", "window_days": 7, "scope": "all", "workflows": [
        # issue-worthy: failed-minutes over both bars
        wf("sidekick-web", "CI", runs=40, failure=25, failed_minutes=120, failure_rate=0.62, total_minutes=50),
        # digest: failed-minutes below bars
        wf("sidekick-harness", "CI", runs=30, failure=4, failed_minutes=12, failure_rate=0.13, total_minutes=40),
        # issue-worthy: concurrency (always)
        wf("sidekick-inference", "Deploy", cancelled=6, cancel_rate=0.30, cancelled_minutes=40, total_minutes=30),
        # issue-worthy: flakes >= 3
        wf("sidekick-admin-kit", "Nightly", flake_count=5, total_minutes=20),
    ]}
    out = rec.partition(data)
    j = {f"{x['repo']}/{x['category']}" for x in out["judgment"]}
    assert j == {"sidekick-web/failed-minutes", "sidekick-inference/concurrency",
                 "sidekick-admin-kit/flakes"}, j
    d = {f"{x['repo']}/{x['category']}" for x in out["digest"]}
    assert "sidekick-harness/failed-minutes" in d, d
    assert out["scanned_repos"] == sorted(
        {"sidekick-web", "sidekick-harness", "sidekick-inference", "sidekick-admin-kit"}), out["scanned_repos"]
    print("split/thresholds/scanned_repos OK")


def test_coalesce_shared():
    rec.gh_raw_workflow = lambda r, p: None
    data = {"generated_at": "t", "window_days": 7, "scope": "all", "workflows": [
        wf("sidekick-web", "Claude Code Review", flake_count=1),
        wf("sidekick-harness", "Claude Code Review", flake_count=2),
        wf("octo-brain", "Claude Code Review", flake_count=1),
    ]}
    out = rec.partition(data)
    flake_digest = [x for x in out["digest"] if x["category"] == "flakes"]
    assert len(flake_digest) == 1, flake_digest          # 3 repos -> 1 coalesced row
    assert flake_digest[0]["coalesced_repos"] == ["octo-brain", "sidekick-harness", "sidekick-web"]
    print("org-shared coalescing OK")


def test_burner_accepted_spend():
    # A burner on an ACCEPTED-spend workflow (sidekick-web/CI, sre-brain#170) is
    # suppressed to the digest even when enormous; an equally large burner
    # elsewhere still files a standing issue.
    rec.gh_raw_workflow = lambda r, p: None
    data = {"generated_at": "t", "window_days": 7, "scope": "all", "workflows": [
        wf("sidekick-web", "CI", total_minutes=1542, runs=181, avg_minutes=8.5, p95_minutes=11.1),
        wf("sidekick-harness", "CI", total_minutes=1712, runs=180, avg_minutes=9.5, p95_minutes=12.1),
    ]}
    out = rec.partition(data)
    j = {f"{x['repo']}/{x['category']}" for x in out["judgment"]}
    d = {f"{x['repo']}/{x['category']}" for x in out["digest"]}
    assert "sidekick-web/burners" not in j, j       # accepted → no standing issue
    assert "sidekick-web/burners" in d, d            # still visible in the digest
    assert "sidekick-harness/burners" in j, j        # control: still files an issue
    print("burner accepted-spend suppression OK")


def test_reconcile_rails():
    closed = []
    oi.close_issue = lambda n, c: (closed.append(n) or True)
    oi.list_open_ci_audit_issues = lambda: [
        {"number": 101, "body": "<!-- ci-target repo=sidekick-labs/sidekick-web workflow=x category=flakes -->\n<!-- actions-audit:a1a1a1a1a1a1 -->"},
        {"number": 102, "body": "<!-- ci-target repo=sidekick-labs/sidekick-web workflow=x category=concurrency -->\n<!-- actions-audit:b2b2b2b2b2b2 -->"},
        {"number": 103, "body": "human-filed, no marker"},
        {"number": 104, "body": "<!-- ci-target repo=sidekick-labs/unscanned-repo workflow=x category=flakes -->\n<!-- actions-audit:c3c3c3c3c3c3 -->"},
    ]
    n = oi.reconcile({"b2b2b2b2b2b2"}, {"sidekick-web", "sidekick-harness"}, "http://run/1")
    assert closed == [101], closed   # only cleared + scanned + marker-bearing
    assert n == 1, n
    print("reconcile safety rails OK")


if __name__ == "__main__":
    test_split_thresholds()
    test_coalesce_shared()
    test_burner_accepted_spend()
    test_reconcile_rails()
    print("ALL TESTS PASSED")
