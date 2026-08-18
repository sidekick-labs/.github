#!/usr/bin/env python3
"""Logic tests for the actions-audit noise-control + reconcile changes.

Self-contained, no network: loads recommendations.py / open_issues.py directly,
feeds synthetic findings, and asserts the issue-worthy/digest split, org-shared
coalescing, and the reconcile pass's safety rails (marker-scoped + scanned-scoped).
Run: `python3 tests/audit-recommendations-reconcile-logic.py` (exits non-zero on
failure). Gated in PR CI by .github/workflows/test-audit-logic.yml, which
path-filters on `audit/**` and on this file.
"""
import importlib.util
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass introspection (audit.py) can resolve the
    # module by name — required on 3.9, harmless on 3.12.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rec = _load("recommendations", "audit/recommendations.py")
oi = _load("open_issues", "audit/open_issues.py")
audit = _load("audit", "audit/audit.py")


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


def _run(conclusion, minutes, mins_ago, event):
    # `started` ordering only needs to be relative; smaller mins_ago = later.
    from datetime import datetime, timedelta, timezone
    return {"conclusion": conclusion, "minutes": minutes, "event": event,
            "started": datetime.now(timezone.utc) - timedelta(minutes=mins_ago),
            "wf_key": ("r", 1)}


def test_wasted_failed_minutes():
    # PR branch fails then goes green on the fix push → NOT waste (sre-brain#211).
    fixed_then_green = [
        _run("failure", 8, mins_ago=30, event="pull_request"),
        _run("success", 8, mins_ago=10, event="pull_request"),
    ]
    assert audit.wasted_failed_minutes(fixed_then_green) == 0.0, "fixed-then-green PR must not count"

    # PR branch that thrashes and never goes green → all failures count.
    thrashing = [
        _run("failure", 8, mins_ago=30, event="pull_request"),
        _run("failure", 7, mins_ago=10, event="pull_request"),
    ]
    assert audit.wasted_failed_minutes(thrashing) == 15.0, "never-green PR branch is waste"

    # Non-PR (push/main going red) always counts, even if a later push is green.
    push_red_then_green = [
        _run("failure", 9, mins_ago=30, event="push"),
        _run("success", 9, mins_ago=10, event="push"),
    ]
    assert audit.wasted_failed_minutes(push_red_then_green) == 9.0, "push failure is always waste"

    # timed_out counts like failure; the earlier green does not retroactively excuse it.
    green_then_fail = [
        _run("success", 5, mins_ago=30, event="pull_request"),
        _run("timed_out", 6, mins_ago=10, event="pull_request"),
    ]
    assert audit.wasted_failed_minutes(green_then_fail) == 6.0, "trailing failure still waste"
    print("wasted_failed_minutes (sre-brain#211) OK")


def test_failed_runs_path_is_workflow_scoped():
    """The deep-inspection path must scope the workflow in the PATH.

    ``?workflow_id=`` is not a supported query parameter on ``/actions/runs``:
    GitHub returns 200 and silently ignores it, so the old form computed
    "top failing jobs" repo-wide while presenting it as per-workflow. Verified
    live against sidekick-web (see failed_runs_path's docstring): a BOGUS
    workflow id returned the same total_count as either real id, and as no
    param at all. This test fails against that old form.
    """
    iso = "2026-08-11T00:00:00Z"
    path = audit.failed_runs_path("sidekick-web", 202404520, iso)

    # The supported, actually-filtering form.
    assert "/repos/sidekick-labs/sidekick-web/actions/workflows/202404520/runs" in path, path
    # The silently-ignored form must be gone: both the param and the bare
    # collection endpoint it hung off.
    assert "workflow_id=" not in path, f"?workflow_id= is silently ignored by GitHub: {path}"
    assert "/actions/runs" not in path, f"must not use the repo-wide collection: {path}"
    # Filters we still rely on are query params (these ARE supported there).
    for expected in ("status=failure", f"created=>={iso}", "per_page=20"):
        assert expected in path, f"missing {expected} in {path}"

    # per_page is a parameter, not a hardcode.
    assert "per_page=5" in audit.failed_runs_path("sidekick-harness", 1, iso, per_page=5)

    # Guard the deliberate exceptions: these two WANT the repo-wide set, so they
    # must keep using /actions/runs and must never grow a ?workflow_id=.
    for fn in (audit.fetch_runs, audit.has_active_runs):
        src = inspect.getsource(fn)
        assert "/actions/runs" in src, fn.__name__
        assert "workflow_id" not in src, f"{fn.__name__} grew an ignored ?workflow_id="
    print("failed_runs_path is workflow-scoped by path (fabricated attribution) OK")


if __name__ == "__main__":
    test_failed_runs_path_is_workflow_scoped()
    test_split_thresholds()
    test_coalesce_shared()
    test_burner_accepted_spend()
    test_reconcile_rails()
    test_wasted_failed_minutes()
    print("ALL TESTS PASSED")
