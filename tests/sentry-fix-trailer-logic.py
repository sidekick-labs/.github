#!/usr/bin/env python3
"""Behaviour tests for the Sentry fix-trailer guard (sentry-fix-trailer.yml).

Why this exists
---------------
The guard is advisory: it posts, updates or deletes ONE sticky PR comment keyed
on `<!-- sentry-fix-trailer-guard -->`, and it must never go red on a finding or
on a read-only fork token. Four product repos call it at `@main`, so a regex or
upsert regression here lands everywhere at once with every run still green.

The logic is an inline `actions/github-script` script (it runs in the CALLING
repo, where a script file in this repo would not exist). So this test extracts
that script from the workflow and runs it under Node against a mocked `github`,
`context` and `core`, then asserts on the writes it made. Network-free.

Each "must prompt" case sits beside a "must stay quiet" control, so a script
that always (or never) commented cannot pass.

A code counts as covered by `Fixes|Resolves|Closes <CODE>` in the PR BODY (the
primary place: the Sentry repos squash with the PR body as the commit message,
and Sentry resolves from PR descriptions) or in any commit message (for a
rebase merge). See octo-brain#558.

Run: `python3 tests/sentry-fix-trailer-logic.py` (exits non-zero on failure).
Needs `node` on PATH (preinstalled on ubuntu-latest).
"""
import json
import os
import subprocess
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "sentry-fix-trailer.yml")
MARKER = "<!-- sentry-fix-trailer-guard -->"

# Node harness: wraps the extracted script in an async function, feeds it one
# scenario's mocked API state, and prints every write plus core.* output.
HARNESS = r"""
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
const sc = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const log = { writes: [], warnings: [], infos: [] };
const failWrite = async () => { const e = new Error('Resource not accessible by integration'); e.status = 403; throw e; };
const rec = (kind) => async (args) => {
  if (sc.writesFail) return failWrite();
  log.writes.push({ kind, ...args });
  return { data: {} };
};
const listCommits = async () => {};
const listComments = async () => {};
const get = async () => {
  if (sc.getFails) throw new Error('Server Error');
  return { data: { body: sc.apiBody !== undefined ? sc.apiBody : sc.body } };
};
const github = {
  rest: {
    pulls: { listCommits, get },
    issues: {
      listComments,
      createComment: rec('create'),
      updateComment: rec('update'),
      deleteComment: rec('delete'),
    },
  },
  paginate: async (fn) => {
    if (fn === listCommits) return sc.commits.map((m) => ({ commit: { message: m } }));
    if (fn === listComments) return sc.comments;
    throw new Error('unexpected paginate target');
  },
};
const context = {
  repo: { owner: 'sidekick-labs', repo: 'sidekick-web' },
  payload: sc.noPr ? {} : { pull_request: { number: 7, body: sc.body } },
};
const core = {
  warning: (m) => log.warnings.push(String(m)),
  info: (m) => log.infos.push(String(m)),
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('github', 'context', 'core', script)(github, context, core)
  .then(() => { process.stdout.write(JSON.stringify(log)); })
  .catch((e) => { process.stdout.write(JSON.stringify({ threw: String(e) })); });
"""


def extract_script():
    with open(WORKFLOW, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    # PyYAML reads the bare `on:` key as boolean True.
    on = wf.get("on", wf.get(True))
    assert isinstance(on, dict) and "workflow_call" in on, "must stay a workflow_call reusable"
    steps = wf["jobs"]["check"]["steps"]
    scripts = [s["with"]["script"] for s in steps if "github-script" in s.get("uses", "")]
    assert len(scripts) == 1, f"expected one github-script step, got {len(scripts)}"
    return wf, scripts[0]


def run(script_path, harness_path, tmp, scenario):
    sc = {"body": "", "commits": [], "comments": [], **scenario}
    sc_path = os.path.join(tmp, "scenario.json")
    with open(sc_path, "w", encoding="utf-8") as fh:
        json.dump(sc, fh)
    out = subprocess.run(
        ["node", harness_path, script_path, sc_path],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if out.returncode != 0:
        raise AssertionError(f"node exited {out.returncode}: {out.stderr}")
    return json.loads(out.stdout)


STALE = [{"id": 99, "body": f"{MARKER}\nold prompt"}, {"id": 5, "body": "unrelated"}]

# (name, scenario, check(result) -> error string or None)
CASES = [
    ("no mentions: quiet", {"body": "Refactor only.", "commits": ["chore: tidy"]},
     lambda r: None if not r["writes"] and not r["warnings"] else "expected no writes/warnings"),
    ("mention in PR body, no Fixes anywhere: prompts", {"body": "Addresses SIDEKICK-WEB-1C.", "commits": ["fix: guard nil"]},
     lambda r: None if [w["kind"] for w in r["writes"]] == ["create"]
     and MARKER in r["writes"][0]["body"] and "`SIDEKICK-WEB-1C`" in r["writes"][0]["body"]
     and any("SIDEKICK-WEB-1C" in w for w in r["warnings"]) else f"expected one create: {r}"),
    ("Fixes trailer in a commit: quiet", {"body": "Addresses SIDEKICK-WEB-1C.", "commits": ["fix: guard nil\n\nFixes SIDEKICK-WEB-1C"]},
     lambda r: None if not r["writes"] and not r["warnings"] else f"expected quiet: {r}"),
    # The premise fix (octo-brain#558): these repos squash with the PR body as
    # the commit message, and Sentry resolves from PR descriptions, so a Fixes
    # line in the BODY is the primary way to satisfy the guard.
    ("Fixes in PR body only: quiet", {"body": "Fixes SIDEKICK-HARNESS-4D", "commits": ["fix: x"]},
     lambda r: None if not r["writes"] and not r["warnings"] else f"body trailer must count: {r}"),
    ("mention in a commit, Fixes in the body: quiet",
     {"body": "Closes SIDEKICK-WEB-2E", "commits": ["fix: SIDEKICK-WEB-2E nil guard"]},
     lambda r: None if not r["writes"] else f"body trailer must cover a commit mention: {r}"),
    ("stale payload body, edited body via API has Fixes: quiet",
     {"body": "Addresses SIDEKICK-WEB-1C", "apiBody": "Addresses SIDEKICK-WEB-1C\n\nFixes SIDEKICK-WEB-1C"},
     lambda r: None if not r["writes"] else f"must re-read the body from the API: {r}"),
    ("stale payload body, Fixes removed via API: prompts",
     {"body": "Fixes SIDEKICK-WEB-1C", "apiBody": "Mentions SIDEKICK-WEB-1C"},
     lambda r: None if [w["kind"] for w in r["writes"]] == ["create"] else f"must use the API body: {r}"),
    ("PR body read fails: falls back to payload, never throws",
     {"body": "Fixes SIDEKICK-WEB-1C", "getFails": True},
     lambda r: None if "threw" not in r and not r["writes"]
     and any("re-read the PR body" in w for w in r["warnings"]) else f"expected payload fallback: {r}"),
    ("guidance points at the PR description",
     {"body": "SIDEKICK-WEB-1C"},
     lambda r: None if r["writes"] and "**PR description**" in r["writes"][0]["body"]
     and "not just the PR description" not in r["writes"][0]["body"]
     and "squash" in r["writes"][0]["body"] else f"comment guidance wrong: {r}"),
    ("lowercase resolves trailer counts", {"commits": ["fix SIDEKICK-INFERENCE-22\n\nresolves SIDEKICK-INFERENCE-22"]},
     lambda r: None if not r["writes"] else f"lowercase trailer must satisfy: {r}"),
    ("closes trailer for a kit project counts", {"commits": ["fix\n\nCloses SIDEKICK-COMPANION-KIT-M"]},
     lambda r: None if not r["writes"] else f"expected quiet: {r}"),
    ("kit prefixes are recognised", {"body": "SIDEKICK-ADMIN-KIT-3 and SIDEKICK-RDP-CLIENT-A1"},
     lambda r: None if r["writes"] and "`SIDEKICK-ADMIN-KIT-3`" in r["writes"][0]["body"]
     and "`SIDEKICK-RDP-CLIENT-A1`" in r["writes"][0]["body"] else f"expected both codes: {r}"),
    ("repo refs and unknown prefixes are not short-codes",
     {"body": "See sidekick-harness#843, SIDEKICK-WEB, SIDEKICK-FOO-12 and SIDEKICK-WEB-TOOLONG1."},
     lambda r: None if not r["writes"] else f"false positive: {r}"),
    ("partial trailer: prompts only for the missing code",
     {"body": "SIDEKICK-WEB-1C SIDEKICK-WEB-1Y", "commits": ["fix\n\nFixes SIDEKICK-WEB-1C"]},
     lambda r: None if r["writes"] and "`SIDEKICK-WEB-1Y`" in r["writes"][0]["body"]
     and "`SIDEKICK-WEB-1C`" not in r["writes"][0]["body"] else f"expected only 1Y: {r}"),
    ("existing sticky + still missing: updates in place",
     {"body": "SIDEKICK-WEB-1C", "comments": STALE},
     lambda r: None if [(w["kind"], w.get("comment_id")) for w in r["writes"]] == [("update", 99)]
     else f"expected update of 99: {r}"),
    ("existing sticky + now fixed: deletes it",
     {"body": "SIDEKICK-WEB-1C", "commits": ["Fixes SIDEKICK-WEB-1C"], "comments": STALE},
     lambda r: None if [(w["kind"], w.get("comment_id")) for w in r["writes"]] == [("delete", 99)]
     else f"expected delete of 99: {r}"),
    ("read-only token: never throws, warns instead",
     {"body": "SIDEKICK-WEB-1C", "writesFail": True},
     lambda r: None if "threw" not in r and any("Could not post" in w for w in r["warnings"])
     else f"must swallow the 403: {r}"),
    ("read-only token on stale delete: never throws",
     {"commits": ["Fixes SIDEKICK-WEB-1C"], "comments": STALE, "writesFail": True},
     lambda r: None if "threw" not in r and any("Could not delete" in w for w in r["warnings"])
     else f"must swallow the 403: {r}"),
    ("no pull_request payload: no-op", {"noPr": True},
     lambda r: None if "threw" not in r and not r["writes"] else f"expected no-op: {r}"),
]


def main():
    wf, script = extract_script()
    failures = []

    # Structural: advisory semantics. No continue-on-error (it would hide real
    # breakage) and no workflow-level concurrency (it would collide with the
    # caller's group, since a callee sees the caller's github.workflow).
    job = wf["jobs"]["check"]
    if job.get("name") != "Sentry fix-trailer check":
        failures.append("job name must stay 'Sentry fix-trailer check'")
    if any(s.get("continue-on-error") for s in job["steps"]) or job.get("continue-on-error"):
        failures.append("continue-on-error must not be set (see the script's comment)")
    if "concurrency" in wf:
        failures.append("concurrency belongs in the caller, not the reusable")
    if job.get("permissions") != {"contents": "read", "pull-requests": "write"}:
        failures.append(f"job permissions drifted: {job.get('permissions')}")

    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "script.js")
        harness_path = os.path.join(tmp, "harness.js")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)
        with open(harness_path, "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        for name, scenario, check in CASES:
            try:
                err = check(run(script_path, harness_path, tmp, scenario))
            except AssertionError as e:
                err = str(e)
            print(f"{'FAIL' if err else 'ok  '}  {name}")
            if err:
                failures.append(f"{name}: {err}")

    if failures:
        print("\n" + "\n".join(failures), file=sys.stderr)
        sys.exit(1)
    print(f"\nall {len(CASES)} cases passed")


if __name__ == "__main__":
    main()
