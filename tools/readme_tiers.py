#!/usr/bin/env python3
"""Write the README's "Verified, not just claimed" table from the committed
checker reports and the test suite, so no number in it is typed by hand.

    python tools/readme_tiers.py           rewrite the table in README.md
    python tools/readme_tiers.py --check   exit 1 if README.md is out of date (CI runs this)

The table sits between <!-- tiers:start --> and <!-- tiers:end -->.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START, END = "<!-- tiers:start -->", "<!-- tiers:end -->"
VERDICT = re.compile(r"\.{2,} (PASS|FAIL)$", re.M)


def report_counts(path: Path) -> tuple[int, int]:
    """(passed, total) verdict lines in a checker report."""
    verdicts = VERDICT.findall(path.read_text(encoding="utf-8"))
    return verdicts.count("PASS"), len(verdicts)


def test_count() -> int:
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    m = re.search(r"^(\d+) tests? collected", out, re.M)
    if not m:
        raise SystemExit(f"could not count the tests:\n{out[-500:]}")
    return int(m.group(1))


def result(passed: int, total: int, report: str, extra: str = "") -> str:
    verdict = f"{passed} of {total} pass" if passed == total else f"**{total - passed} of {total} fail**"
    return f"[{verdict}]({report}){extra}"


def table(tests: int | None = None) -> str:
    official = report_counts(ROOT / "acceptance-report.txt")
    extended = report_counts(ROOT / "acceptance-report-extended.txt")
    tests = test_count() if tests is None else tests
    return "\n".join([
        START,
        "| Tiers | Checked by | Result |",
        "|---|---|---|",
        f"| T1, T2 | `tools/acceptance_run.py`, the published `run.py` byte for byte | "
        f"{result(*official, 'acceptance-report.txt')} |",
        f"| T3, T4 | [`tools/acceptance_extended.py`](tools/acceptance_extended.py): the same kind of checker, for the "
        f"tiers `run.py` has no checks for | {result(*extended, 'acceptance-report-extended.txt', '; every verdict lists the HTTP requests it rests on')} |",
        f"| All of it | {tests} tests, and CI running both checkers against a real `docker compose up` on every push, "
        "and again with the network cut off | [`tests/`](tests), [`ci.yml`](.github/workflows/ci.yml) |",
        END,
    ])


def render(readme: str, block: str) -> str:
    if START not in readme or END not in readme:
        raise SystemExit(f"README.md needs {START} and {END} around the tier table")
    before, rest = readme.split(START, 1)
    _, after = rest.split(END, 1)
    return before + block + after


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    path = ROOT / "README.md"
    readme = path.read_text(encoding="utf-8")
    updated = render(readme, table())
    if args.check:
        if updated != readme:
            print("README.md's tier table is out of date: run python tools/readme_tiers.py", file=sys.stderr)
            return 1
        print("README.md's tier table matches the reports and the test suite")
        return 0
    path.write_text(updated, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
