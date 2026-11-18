#!/usr/bin/env python3
"""Tests for the outcome ledger (`.github/actions/outcome-ledger/ledger.py`, EVAL-010).

Two halves:

1. The self-test the composite runs before every real run must PASS on the shipped
   classifier.
2. It must FAIL when the classifier is broken in each of the ways that would make
   a ledger flattering: unknowns folded into a good outcome, reverts not read, an
   unauthorized `/review-ack` honoured, the check-conclusion cross-check dropped.
   A self-test that cannot go red is not a positive control.

Plus the parsers that read model output (triage comments, triage.json) and the
revert index. Network-free. Run: `python3 tests/outcome-ledger.py`.
"""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(ROOT, ".github", "actions", "outcome-ledger", "ledger.py")
ACTION = os.path.join(ROOT, ".github", "actions", "outcome-ledger", "action.yml")

spec = importlib.util.spec_from_file_location("ledger", LEDGER)
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)

failures = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok or not detail else ' -- ' + str(detail)}")
    if not ok:
        failures.append(label)


def mutated(fn):
    """Run the self-test with one mutation applied; restore afterwards."""
    undo = fn()
    try:
        return ledger.self_test()
    finally:
        undo()


def patch(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    return lambda: setattr(obj, name, old)


def main():
    print("self-test on the shipped classifier:")
    f = ledger.self_test()
    check("passes", not f, f)
    rc = subprocess.run([sys.executable, LEDGER, "--self-test"], capture_output=True, text=True)
    check("`ledger.py --self-test` exits 0", rc.returncode == 0, rc.stdout + rc.stderr)

    print("self-test goes RED on a flattering classifier (positive controls):")
    check("unknown folded into good (no settle window) -> fails",
          bool(mutated(lambda: patch(ledger, "settled", lambda at, now, days: True))))
    check("reverts not read -> fails",
          bool(mutated(lambda: patch(ledger.Reverts, "when", lambda self, **kw: None))))
    check("an ack from a read-only user honoured -> fails",
          bool(mutated(lambda: patch(ledger.review_ack, "WRITE_PERMISSIONS",
                                     {"admin", "maintain", "write", "read"}))))
    import re as _re  # noqa: PLC0415
    check("runs not mapped to their PR through the reviewer's run link -> fails",
          bool(mutated(lambda: patch(ledger, "RUN_LINK", _re.compile(r"(?!x)x")))))
    real_derive = ledger.derive_verdict.derive

    def errored_reads_as_pass(*a, **kw):
        out = real_derive(*a, **kw)
        if out["verdict"] == "UNDETERMINED":
            out["verdict"] = "PASS"
        return out
    check("an errored review counted as PASS -> fails",
          bool(mutated(lambda: patch(ledger.derive_verdict, "derive", errored_reads_as_pass))))

    real_compute = ledger.compute

    def every_run_success(facts, now, *a, **kw):
        facts = json.loads(json.dumps(facts))
        for r in facts["review_runs"]:
            r["conclusion"] = "success" if r["conclusion"] == "failure" else r["conclusion"]
        return real_compute(facts, now, *a, **kw)
    check("contradicted conclusions not noticed (all runs read green) -> fails",
          bool(ledger.self_test(compute_fn=every_run_success)))

    print("shape:")
    now = dt.datetime(2026, 11, 18, 12, tzinfo=dt.timezone.utc)
    facts, _ = ledger.planted_world(now)
    out = ledger.compute(facts, now)
    check("schema 1, repo, generated_at, window_days 30",
          out["schema"] == 1 and out["repo"] == "o/r" and out["window_days"] == 30
          and out["generated_at"].endswith("Z"))
    check("every agent carries exactly the shared keys",
          all(set(a) == {"agent", "verdicts", "by_verdict", "outcomes", "unknown", "notes"}
              for a in out["agents"]))
    check("unknown reasons are spelled out in notes",
          "PR still open" in next(a for a in out["agents"] if a["agent"] == "claude-review")["notes"])
    empty = ledger.compute({"repo": "o/r", "pulls": [], "commits": [], "review_runs": [],
                            "comments": {}, "triage": []}, now)
    check("an empty repo yields zero verdicts, zero unknown, all four agents",
          [a["agent"] for a in empty["agents"]] == ["claude-review", "post-deploy-triage",
                                                    "post-deploy-failure-triage", "autofix"]
          and all(a["verdicts"] == 0 and a["unknown"] == 0 for a in empty["agents"]))
    old = ledger.compute(facts, now + dt.timedelta(days=60))
    check("verdicts older than the window are not counted",
          all(a["verdicts"] == 0 for a in old["agents"]))

    print("triage comment parser (model output, read defensively):")
    ok = ledger.parse_triage_comment(
        "### :robot: Post-deploy triage hint\n**Verdict:** likely problem\n"
        "**Suspect lines:** `app/models/user.rb:42`, app/jobs/x_job.rb:7\n**Confidence:** high\n"
        + ledger.SUCCESS_MARKER, ledger.SUCCESS_MARKER)
    check("success verdict, confidence and suspect files",
          ok == ("likely problem", "high", ["app/jobs/x_job.rb", "app/models/user.rb"], None), ok)
    ok = ledger.parse_triage_comment(
        "**Category:** flake / transient\n**Confidence:** medium\n- Failed SHA: `" + "ab12" * 10 + "`",
        ledger.FAILURE_MARKER)
    check("failure category and failed SHA", ok == ("flake / transient", "medium", [], "ab12" * 10), ok)
    check("a verdict outside the allow-list is not read",
          ledger.parse_triage_comment("**Verdict:** catastrophic", ledger.SUCCESS_MARKER) is None)

    print("triage.json validation:")
    check("allow-listed verdict accepted",
          ledger.validate_triage_json({"verdict": "Clean", "confidence": "HIGH",
                                       "suspect_files": []}, "success") == ("clean", "high", []))
    check("unknown verdict rejected",
          ledger.validate_triage_json({"verdict": "fine"}, "success") is None)
    check("success-path key is not accepted on the failure path",
          ledger.validate_triage_json({"verdict": "clean"}, "failure") is None)
    check("path traversal and junk in suspect_files dropped",
          ledger.validate_triage_json({"category": "code regression", "confidence": "x",
                                       "suspect_files": ["../etc/passwd", "a b", 3, "src/ok.ts"]},
                                      "failure") == ("code regression", None, ["src/ok.ts"]))

    print("revert index:")
    t = "2026-11-10T00:00:00Z"
    r = ledger.Reverts([{"message": 'Revert "Add Foo (#12)" (#15)', "date": t},
                        {"message": "fix\n\nThis reverts commit abcdef1234.", "date": t}],
                       [{"title": 'Revert "Add Bar"', "body": "Reverts o/r#20", "merged_at": t}])
    check("GitHub squash title -> PR number", r.when(pr=12) is not None)
    check("git revert body -> SHA (prefix either way)",
          r.when(sha="abcdef1234" + "0" * 30) is not None and r.when(sha="abcdef1") is not None)
    check("revert PR body -> PR number", r.when(pr=20) is not None)
    check("revert before the verdict does not count",
          r.when(pr=12, after=ledger.parse_ts("2026-11-11T00:00:00Z")) is None)
    check("unrelated PR not reverted", r.when(pr=13, sha="1234567") is None)

    print("review-run listing beats the API's 1000-result cap:")

    class FakeApi:
        """1500 runs over 10 days (150/day); any query whose range holds >= 1000
        runs is truncated to 1000, as the real API does."""
        repo = "o/r"

        def __init__(self):
            base = dt.datetime(2026, 11, 1, 12, tzinfo=dt.timezone.utc)
            self.runs = [{"id": i, "created_at": ledger.iso(base + dt.timedelta(days=i // 150)),
                          "conclusion": "success", "head_sha": "x", "pull_requests": []}
                         for i in range(1500)]

        def _in(self, q):
            lo, hi = q["created"].split("..")
            return [r for r in self.runs if lo <= r["created_at"][:10] <= hi]

        def get(self, path, q):
            return {"total_count": len(self._in(q))}

        def paged(self, path, q, key=None):
            yield from self._in(q)[:1000]

    got = ledger.collect_review_runs(FakeApi(), dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
                                     dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc), set())
    check("all 1500 runs listed, none twice", len(got) == 1500 and len({r["run_id"] for r in got}) == 1500,
          len(got))

    print("composite:")
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        check("PyYAML available to validate the composite", False)
    else:
        comp = yaml.safe_load(open(ACTION))
        check("is a composite", comp.get("runs", {}).get("using") == "composite")
        for st in comp["runs"]["steps"]:
            if "run" in st:
                check(f"run step '{st.get('name')}' declares bash", st.get("shell") == "bash")
            if "uses" in st:
                import re  # noqa: PLC0415
                check(f"step '{st.get('name')}' pins its action by SHA",
                      bool(re.search(r"@[0-9a-f]{40}$", st["uses"])), st["uses"])
        body = open(ACTION).read()
        check("no `secrets.` / `vars.` (empty inside a composite)",
              "secrets." not in body and "vars." not in body)
        check("the self-test runs before compute",
              body.index("--self-test") < body.index("compute --repo"))

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
