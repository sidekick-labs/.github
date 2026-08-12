#!/usr/bin/env python3
"""Classification + exit-code contract tests for the pin-integrity gate.

Why this exists
---------------
The gate once reported `DOES NOT EXIST` for a pin that was perfectly correct,
because `classify()` returned `dead` on ANY non-200 — a 403, a 5xx and a timeout
were indistinguishable from a 404. One transient call blocked a PR and sent the
reader off to re-pin `actions/github-script@3a2844b7`, which is the dereferenced
commit of the annotated `v9.0.0` tag AND the head of that repo's `main`. Re-running
the identical job passed with no change. See sidekick-labs/sre-brain#420.

The fix distinguishes three states, and over-correcting would be as bad as the
original bug: a gate that stops failing on real dead pins is worse than one that
occasionally false-positives, because a dead pin fails at RUN time on whichever
path uses it — typically a release, long after merge. So both halves are pinned:

    a confirmed 404 on both probes  -> dead     -> exit 1, `DOES NOT EXIST`
    anything else unresolved        -> unknown  -> exit 0, `COULD NOT BE VERIFIED`

Same shape as `review-gate-exit-codes.py`: the script is inline `run:` YAML (it has
to be — `actions/checkout` in a `workflow_call` job checks out the CALLING repo, so
a `scripts/*.py` here would not exist at runtime), so this extracts it from the
workflow and drives it with stubbed API responses. Nothing here touches the network.

    python3 tests/pin-integrity-classify.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/pin-check.yml"
HEREDOC = "cat > \"${RUNNER_TEMP}/pin_integrity.py\" <<'PIN_INTEGRITY_EOF'\n"

SHA = "3a2844b7e9c422d3c10d287c895573f7108da1b3"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def load_module():
    """Extract the inline script from the workflow and import it."""
    src = WORKFLOW.read_text()
    if HEREDOC not in src:
        raise SystemExit(
            "pin_integrity.py heredoc not found in pin-check.yml — the step was renamed or "
            "restructured, so this test is no longer reading the script it claims to test."
        )
    body = textwrap.dedent(src.split(HEREDOC, 1)[1].split("PIN_INTEGRITY_EOF")[0])

    tmp = pathlib.Path(tempfile.mkdtemp()) / "pin_integrity.py"
    tmp.write_text(body)
    spec = importlib.util.spec_from_file_location("pin_integrity", tmp)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # No real backoff: the retry POLICY is under test, the sleeping is not.
    module.time = type("T", (), {"sleep": staticmethod(lambda _s: None)})()
    return module


def test_classify(module) -> None:
    print("\nclassify() — three states, not two\n")

    cases = {
        # A real commit: 200 whose returned sha matches what was asked for.
        "real commit": ({f"/repos/a/b/commits/{SHA}": (200, {"sha": SHA})}, "commit"),
        # The ONLY dead case: both probes definitively say no.
        "confirmed 404 on both probes": (
            {f"/repos/a/b/commits/{SHA}": (404, None), f"/repos/a/b/git/tags/{SHA}": (404, None)},
            "dead",
        ),
        # A pin naming the annotated tag OBJECT rather than its commit.
        "annotated tag object": (
            {
                f"/repos/a/b/commits/{SHA}": (404, None),
                f"/repos/a/b/git/tags/{SHA}": (200, {"object": {"sha": "d" * 40}}),
            },
            "tag-object",
        ),
        # Everything below is the bug this file exists for: NOT evidence of a bad pin.
        "rate limited (403)": (
            {f"/repos/a/b/commits/{SHA}": (403, None), f"/repos/a/b/git/tags/{SHA}": (403, None)},
            "unknown",
        ),
        "server error (502)": (
            {f"/repos/a/b/commits/{SHA}": (502, None), f"/repos/a/b/git/tags/{SHA}": (502, None)},
            "unknown",
        ),
        "no response at all (timeout/DNS/reset)": (
            {f"/repos/a/b/commits/{SHA}": (0, None), f"/repos/a/b/git/tags/{SHA}": (0, None)},
            "unknown",
        ),
        # Mixed: one definite no, one unanswered — a tag-object cannot be ruled out.
        "404 then unreachable": (
            {f"/repos/a/b/commits/{SHA}": (404, None), f"/repos/a/b/git/tags/{SHA}": (0, None)},
            "unknown",
        ),
        # A 200 carrying a DIFFERENT sha is not success; an error body is still JSON.
        # Both probes ANSWERED here, so this is dead rather than unverifiable.
        "200 with a mismatched sha": (
            {
                f"/repos/a/b/commits/{SHA}": (200, {"sha": "f" * 40}),
                f"/repos/a/b/git/tags/{SHA}": (404, None),
            },
            "dead",
        ),
    }

    for label, (table, expected) in cases.items():
        module.api = lambda path, _t=table: _t.get(path, (404, None))
        kind, _detail = module.classify("a", "b", SHA)
        check(f"{label} -> {expected}", kind == expected, f"got {kind}")


def test_retry_policy(module) -> None:
    print("\napi() — retries only what is worth retrying\n")

    for status, want_calls, why in [
        (404, 1, "an ANSWER; retrying it into ambiguity would be its own bug"),
        (401, 1, "deterministic like a 404 — an invalid token stays invalid"),
        (403, 3, "transient: secondary rate limit"),
        (502, 3, "transient"),
    ]:
        calls = {"n": 0}

        def opener(_req, timeout=None, _s=status, _c=calls):
            _c["n"] += 1
            raise urllib.error.HTTPError("u", _s, "e", None, None)

        original = urllib.request.urlopen
        urllib.request.urlopen = opener
        try:
            module.api("/repos/a/b/commits/x")
        finally:
            urllib.request.urlopen = original

        check(
            f"HTTP {status} -> {want_calls} call(s)",
            calls["n"] == want_calls,
            f"{calls['n']} calls; {why}",
        )


def test_exit_codes(module) -> None:
    print("\nthe gate's exit code per state\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        module.collect = lambda _paths: {("third", "party", SHA, "v1.0.0"): ["wf.yml:10"]}
        module.tag_sha = lambda _o, _r, _t: SHA
        sys.argv = ["pin_integrity.py", tmpdir]

        def run(kind: str, detail: str = ""):
            module.classify = lambda _o, _r, _s: (kind, detail)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = module.main()
            return rc, buf.getvalue()

        # Guard against a vacuous pass: main() no-ops when handed no real directory,
        # and the first draft of this test passed for exactly that reason.
        _rc, out = run("commit", SHA)
        check("main() actually ran (not a vacuous pass)", "pin integrity:" in out)

        rc, _out = run("commit", SHA)
        check("commit -> exit 0", rc == 0, f"exit {rc}")

        rc, out = run("tag-object", "d" * 40)
        check("tag-object -> exit 0 + warning", rc == 0 and "::warning::" in out, f"exit {rc}")

        rc, out = run("unknown", "commits=403 git/tags=403")
        check("unknown -> exit 0", rc == 0, f"exit {rc}")
        check("unknown says COULD NOT BE VERIFIED", "COULD NOT BE VERIFIED" in out)
        check("unknown does NOT claim nonexistence", "DOES NOT EXIST" not in out)
        check("unknown is counted in the summary", "1 unverifiable" in out)

        # The other half of the contract, and the one a well-meaning "stop blocking
        # PRs" change would break.
        rc, out = run("dead")
        check("dead -> exit 1", rc == 1, f"exit {rc}")
        check("dead still says DOES NOT EXIST", "DOES NOT EXIST" in out)
        check("dead is counted in the summary", "1 dead pin(s)" in out)


def main() -> int:
    print("pin-integrity classification + exit-code contract")
    # A FRESH module per group. `test_classify` replaces `api` with a stub and
    # `test_exit_codes` replaces `classify`/`collect`; sharing one module made the
    # retry group exercise the stub and count zero calls — passing, had the
    # expectation been "0".
    test_classify(load_module())
    test_retry_policy(load_module())
    test_exit_codes(load_module())

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {failures}")
        return 1
    print("all checks hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
