#!/usr/bin/env python3
"""Exit-code contract tests for the Claude Code Review merge gate.

Why this exists
---------------
`review / Claude Code Review` once reported check conclusion SUCCESS while the
action had errored and posted no review, because the gate step ended a
model-side failure with `exit 0`. Workspace policy makes merging autonomous
*because* the check set is dense, so a green check that reviewed nothing
silently deletes a gate. See sidekick-labs/core-platform-brain#340.

The fix is one line of behaviour -- "no review ran" must exit non-zero -- and
one line is exactly what a future refactor can quietly put back. This file pins
the contract:

    a review happened      -> exit 0
    no review happened     -> exit 1   (whatever the cause)

The gate script is inline `run:` YAML rather than a checked-in shell file, and
deliberately so: this workflow is consumed by ~25 repos via `workflow_call`, and
`actions/checkout` in that job checks out the CALLING repo, so a
`scripts/*.sh` here would not exist at runtime. The test therefore extracts the
step's script out of the workflow and executes it against synthetic
execution-output fixtures. Nothing here talks to the network or to Anthropic.

Run: `python3 tests/review-gate-exit-codes.py` (exits non-zero on failure).
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "claude-code-review.yml")

GATE_STEP = "Verify a review actually ran"
REVIEW_STEP = "Run Claude Code Review"

failures = []


def check(label, ok, detail=""):
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' -- ' + detail) if detail else ''}")
        failures.append(label)


def step_block(src, name):
    """Return the raw YAML text of the step whose `- name:` is `name`."""
    m = re.search(
        r"^      - name: " + re.escape(name) + r"\n(?:.*?)(?=^      - name: |\Z)",
        src,
        re.S | re.M,
    )
    if not m:
        sys.exit(f"FATAL: step '{name}' not found in {WORKFLOW}. "
                 "If it was renamed, update this test deliberately -- do not delete it.")
    return m.group(0)


def strip_comments(block):
    """Drop whole-line YAML comments so prose about `continue-on-error` in a
    step's explanatory comment is not mistaken for the key itself."""
    return "\n".join(
        line for line in block.split("\n") if not line.lstrip().startswith("#")
    )


def gate_script(src):
    """Extract the gate step's `run: |` body, dedented to column 0."""
    blk = step_block(src, GATE_STEP)
    m = re.search(r"^        run: \|\n(.*)", blk, re.S | re.M)
    if not m:
        sys.exit(f"FATAL: no `run:` block in the '{GATE_STEP}' step.")
    body = m.group(1)
    if "${{" in body:
        sys.exit("FATAL: the gate script now contains GitHub expressions; this "
                 "test executes it as plain shell and can no longer do so safely.")
    lines = []
    for line in body.split("\n"):
        lines.append(line[10:] if line.startswith(" " * 10) else line)
    return "\n".join(lines)


def run_gate(script, execution_log=None, retry_outcome="failure", write_file=True):
    """Execute the gate script against a fixture. Returns (exit_code, stdout)."""
    with tempfile.TemporaryDirectory() as tmp:
        exec_path = os.path.join(tmp, "claude-execution-output.json")
        if execution_log is not None and write_file:
            with open(exec_path, "w") as fh:
                json.dump(execution_log, fh)
        env = dict(os.environ)
        env.update({
            "RETRY_OUTCOME": retry_outcome,
            # Point at the fixture only when we wrote one; otherwise leave it
            # empty so the script exercises its $RUNNER_TEMP fallback.
            "EXECUTION_FILE": exec_path if (execution_log is not None and write_file) else "",
            "RUNNER_TEMP": tmp,
            "GITHUB_STEP_SUMMARY": os.path.join(tmp, "summary.md"),
        })
        proc = subprocess.run(
            ["bash", "-c", script], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        return proc.returncode, proc.stdout


# --- fixtures -------------------------------------------------------------
# Shape mirrors a real run's claude-execution-output.json (a JSON array of
# streamed records ending in one `result` record).

def result_record(**over):
    rec = {"type": "result", "subtype": "success", "is_error": False,
           "duration_ms": 41000, "num_turns": 12, "total_cost_usd": 0.42}
    rec.update(over)
    return [{"type": "system", "subtype": "init", "model": "claude-opus-4-8"}, rec]


# The live 2026-07-28 failure: account-level rejection, fast-fail, zero cost.
SPEND_LIMITED = result_record(is_error=True, duration_ms=620, num_turns=1,
                              total_cost_usd=0, api_error_status=429)
TRANSIENT_5XX = result_record(is_error=True, duration_ms=900, num_turns=1,
                              total_cost_usd=0, api_error_status=503)
CLEAN_REVIEW = result_record()


def main():
    with open(WORKFLOW) as fh:
        src = fh.read()
    script = gate_script(src)

    if subprocess.run(["which", "jq"], stdout=subprocess.DEVNULL).returncode != 0:
        sys.exit("FATAL: jq is required (the gate script uses it).")

    print("exit-code contract:")

    # 1. THE #340 REGRESSION. A model-side is_error that persisted through the
    #    retry must fail the job. This asserted exit 0 before the fix.
    rc, out = run_gate(script, SPEND_LIMITED)
    check("model error (429 spend limit) on both attempts -> exit 1", rc == 1,
          f"got {rc}")
    check("  ... and annotates as ::error, not ::warning",
          "::error" in out and "::warning" not in out)

    # 2. Same for an upstream 5xx -- cause does not change the verdict, because
    #    the only question a merge gate can act on is "did a review happen?".
    rc, _ = run_gate(script, TRANSIENT_5XX)
    check("model error (503 upstream) on both attempts -> exit 1", rc == 1, f"got {rc}")

    # 3. Retry succeeded -> a review exists -> green. This is the branch that
    #    keeps a transient blip from turning the estate red.
    rc, out = run_gate(script, CLEAN_REVIEW, retry_outcome="success")
    check("retry produced a review -> exit 0", rc == 0, f"got {rc}")
    check("  ... and annotates as ::notice", "::notice" in out)

    # 4. Setup/credential failure: the step died with no result record at all.
    rc, _ = run_gate(script, [{"type": "system", "subtype": "init"}])
    check("no result record (setup/auth failure) -> exit 1", rc == 1, f"got {rc}")

    # 5. No execution file anywhere -- the fallback path must not silently pass.
    rc, _ = run_gate(script, None)
    check("missing execution log entirely -> exit 1", rc == 1, f"got {rc}")

    # 6. Unparseable log must not be read as "fine".
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "claude-execution-output.json")
        with open(bad, "w") as fh:
            fh.write("not json{{{")
        env = dict(os.environ)
        env.update({"RETRY_OUTCOME": "failure", "EXECUTION_FILE": bad,
                    "RUNNER_TEMP": tmp,
                    "GITHUB_STEP_SUMMARY": os.path.join(tmp, "s.md")})
        rc = subprocess.run(["bash", "-c", script], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
    check("unparseable execution log -> exit 1", rc == 1, f"got {rc}")

    # --- structural guards ------------------------------------------------
    # The gate only decides the job conclusion if it is allowed to fail it.
    print("structural guards:")
    gate_blk = strip_comments(step_block(src, GATE_STEP))
    check("gate step is NOT continue-on-error",
          "continue-on-error" not in gate_blk)
    check("gate step has no `exit 0` outside the review-succeeded branch",
          gate_blk.count("exit 0") == 1)

    review_blk = strip_comments(step_block(src, REVIEW_STEP))
    check("review step keeps continue-on-error (so the gate step runs)",
          "continue-on-error: true" in review_blk)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
