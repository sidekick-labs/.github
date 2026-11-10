#!/usr/bin/env python3
"""Policy, parsing and exit-code tests for the licence check.

The check's job is to sort every locked package into allow / review / block,
and getting the sort wrong in either direction is costly: a missed AGPL or
source-available licence lets one into a product, and a false block on a
permissive dual licence teaches everyone to ignore the report. Two traps are
pinned here because they are easy to reintroduce:

  * `A OR B` is a choice, so `(MIT OR GPL-3.0)` is allowed; `A AND B` is not.
  * Long-form names such as "GNU Affero General Public License v3 or later
    (AGPLv3+)" contain an `or` that is NOT a disjunction. Splitting on it once
    made Ultralytics (AGPL-3.0) read as "review" instead of "block".

Like pin-integrity-classify.py, the script is inline YAML (a workflow_call job
checks out the CALLING repo), so this extracts it from the workflow. Registry
lookups are stubbed; nothing here touches the network.

    python3 tests/license-check-policy.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import textwrap

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/license-check.yml"
FIXTURE = ROOT / "tests/fixtures/license-check/repo"
HEREDOC = "cat > \"${RUNNER_TEMP}/license_check.py\" <<'LICENSE_CHECK_EOF'\n"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def load_module():
    src = WORKFLOW.read_text()
    if HEREDOC not in src:
        raise SystemExit(
            "license_check.py heredoc not found in license-check.yml — the step was renamed or "
            "restructured, so this test is no longer reading the script it claims to test."
        )
    body = textwrap.dedent(src.split(HEREDOC, 1)[1].split("LICENSE_CHECK_EOF")[0])
    tmp = pathlib.Path(tempfile.mkdtemp()) / "license_check.py"
    tmp.write_text(body)
    spec = importlib.util.spec_from_file_location("license_check", tmp)
    module = importlib.util.module_from_spec(spec)
    sys.modules["license_check"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


POLICY = {
    # allowed
    "MIT": "allow",
    "Apache-2.0 WITH LLVM-exception": "allow",
    "(MIT OR CC0-1.0)": "allow",
    "MIT AND ISC": "allow",
    "MIT OR GPL-3.0": "allow",
    "BSD-2-Clause OR GPL-3.0-only": "allow",
    "Ruby": "allow",
    "Python-2.0": "allow",
    "BlueOak-1.0.0": "allow",
    "OFL-1.1": "allow",
    "MPL-2.0": "allow",
    "Mozilla Public License 2.0 (MPL 2.0)": "allow",
    "Apache Software License": "allow",
    # review (warn)
    "GPL-3.0-only": "review",
    "LGPL-3.0-or-later": "review",
    "Apache-2.0 AND LGPL-3.0-or-later": "review",
    "GNU General Public License v3 or later (GPLv3+)": "review",
    "GNU Lesser General Public License v3 (LGPLv3)": "review",
    "EPL-2.0": "review",
    "SEE LICENSE IN LICENSE.md": "review",
    "UNLICENSED": "review",
    "Standard 'no charge' license: https://gsap.com/standard-license.": "review",
    # blocked
    "AGPL-3.0-only": "block",
    "GNU Affero General Public License v3 or later (AGPLv3+)": "block",
    "FSL-1.1-Apache-2.0": "block",
    "BUSL-1.1": "block",
    "SSPL-1.0": "block",
    "Elastic-2.0": "block",
    "PolyForm-Noncommercial-1.0.0": "block",
    "CC-BY-NC-4.0": "block",
    "Commons Clause": "block",
    "MIT AND AGPL-3.0-only": "block",
}


def test_policy(m) -> None:
    print("\nclassify_expression() — the org policy table\n")
    for expr, want in POLICY.items():
        got = m.classify_expression(expr)
        check(f"{expr!r} -> {want}", got == want, f"got {got}")

    print("\nclassify_alternatives() — registry lists are a choice\n")
    check("RubyGems [Ruby, GPL-2.0-only] -> allow", m.classify_alternatives(["Ruby", "GPL-2.0-only"]) == "allow")
    check("no licence at all -> review", m.classify_alternatives([]) == "review")


def run_fixture(m, *args: str, env_exceptions: str = "") -> tuple[int, str, list]:
    m.FETCHERS["rubygems"] = lambda name, version: {
        "rails": ["MIT"], "actionpack": ["MIT"], "rack": ["MIT"], "racc": ["Ruby", "BSD-2-Clause"],
        "brakeman": ["Brakeman Public Use License"],
    }.get(name, [])
    m.FETCHERS["npm"] = lambda name, version: []
    m.FETCHERS["pypi"] = lambda name, version: []
    out_json = pathlib.Path(tempfile.mkdtemp()) / "rows.json"
    os.environ.pop("GITHUB_STEP_SUMMARY", None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = m.main([str(FIXTURE), "--json", str(out_json), "--exceptions", env_exceptions, *args])
    import json
    return code, buf.getvalue(), json.loads(out_json.read_text())


def test_fixture(m) -> None:
    print("\nmain() over the fixture repo\n")
    code, report, rows = run_fixture(m)
    by = {(r["ecosystem"], r["name"]): r for r in rows}

    check("report-only by default: exit 0 despite a blocked package", code == 0, f"exit {code}")
    check("node_modules/ is not scanned for lockfiles", not any(r["lockfile"].startswith("node_modules") for r in rows))
    check("npm dual licence (MIT OR GPL-3.0) allowed", by[("npm", "dual")]["status"] == "allow")
    check("npm AGPL blocked", by[("npm", "agpl-lib")]["status"] == "block")
    check("npm FSL blocked when not excepted", by[("npm", "fsl-cli")]["status"] == "block")
    check("npm GPL dev dependency is review, scoped dev",
          by[("npm", "gpl-dev")]["status"] == "review" and by[("npm", "gpl-dev")]["scope"] == "dev")
    check("npm 'SEE LICENSE IN' is review", by[("npm", "see-licence")]["status"] == "review")
    check("gem scope: rails closure is prod", by[("rubygems", "rack")]["scope"] == "prod")
    check("gem scope: brakeman is dev/test", by[("rubygems", "brakeman")]["scope"] == "dev/test")
    check("gem licence list is a choice (racc Ruby|BSD-2-Clause)", by[("rubygems", "racc")]["status"] == "allow")
    check("blocked rows are announced as warnings in report-only mode",
          "::warning file=package-lock.json::npm:agpl-lib@1.0.0" in report)
    check("summary names the mode", "report-only (does not fail the build)" in report)

    code, report, rows = run_fixture(
        m, env_exceptions="npm:fsl-cli  # build-time internal use\nrubygems:brakeman  # dev/CI tool\n# a comment line\n"
    )
    by = {(r["ecosystem"], r["name"]): r for r in rows}
    check("exception moves fsl-cli to 'excepted' with its reason",
          by[("npm", "fsl-cli")]["status"] == "excepted" and by[("npm", "fsl-cli")]["reason"] == "build-time internal use")
    check("excepted rows are reported under Recorded exceptions", "### Recorded exceptions" in report and "dev/CI tool" in report)

    code, report, _ = run_fixture(m, "--fail-on-block")
    check("--fail-on-block exits 1 on a blocked, unexcepted package", code == 1, f"exit {code}")
    check("--fail-on-block announces blocks as errors", "::error file=package-lock.json::npm:agpl-lib@1.0.0" in report)

    code, _, _ = run_fixture(m, "--fail-on-block", env_exceptions="npm:fsl-cli # x\nnpm:agpl-lib # x")
    check("--fail-on-block exits 0 once every block is excepted", code == 0, f"exit {code}")


def main() -> int:
    m = load_module()
    test_policy(m)
    test_fixture(m)
    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All licence-check tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
