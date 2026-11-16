"""Print a per-module coverage table and fail if any module is under the bar.

Reads the Cobertura ``coverage.xml`` that ``pytest --cov --cov-report=xml`` writes. Standard
library only, so the workflow needs nothing beyond what ``pytest-cov`` already installed.

The Home Assistant Integration Quality Scale rule ``test-coverage`` (Silver) asks for more than
95% in every module, not 95% overall, so the bar is applied to each file. The figure is
branch-inclusive, the same arithmetic coverage.py prints as "Cover" under ``--cov-branch``:
(covered lines + covered branches) / (lines + branches).

Usage: ``python scripts/coverage_summary.py [coverage.xml]``. The table goes to stdout and, when
``GITHUB_STEP_SUMMARY`` names a file (GitHub Actions; Gitea may leave it unset), is appended there
as Markdown. Exit status is 1 if any module is under ``MIN_COVERAGE_PERCENT`` or no module is found.
"""

from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

# The single place the bar is set.
MIN_COVERAGE_PERCENT = 95.0

_BRANCHES_RE = re.compile(r"\((\d+)/(\d+)\)")


@dataclass(frozen=True)
class ModuleCoverage:
    """One source file's counts."""

    name: str
    lines: int
    lines_hit: int
    branches: int
    branches_hit: int

    @property
    def line_percent(self) -> float:
        """Line coverage alone."""
        return 100.0 if self.lines == 0 else 100.0 * self.lines_hit / self.lines

    @property
    def percent(self) -> float:
        """Line and branch coverage together, as coverage.py reports it."""
        total = self.lines + self.branches
        return 100.0 if total == 0 else 100.0 * (self.lines_hit + self.branches_hit) / total

    @property
    def passes(self) -> bool:
        """Whether the module meets the bar."""
        return self.percent >= MIN_COVERAGE_PERCENT


def read_modules(path: Path) -> list[ModuleCoverage]:
    """Parse coverage.xml into one entry per source file."""
    modules: list[ModuleCoverage] = []
    for cls in ET.parse(path).getroot().iter("class"):
        lines_element = cls.find("lines")
        lines = list(lines_element) if lines_element is not None else []
        hit = sum(1 for line in lines if int(line.get("hits", "0")) > 0)
        branches = branches_hit = 0
        for line in lines:
            if line.get("branch") == "true":
                match = _BRANCHES_RE.search(line.get("condition-coverage", ""))
                if match:
                    branches_hit += int(match.group(1))
                    branches += int(match.group(2))
        name = cls.get("filename", "?").replace("\\", "/").rsplit("/", 1)[-1]
        modules.append(ModuleCoverage(name, len(lines), hit, branches, branches_hit))
    return sorted(modules, key=lambda module: module.name)


def render(modules: list[ModuleCoverage]) -> str:
    """Return the Markdown table, with failing modules marked."""
    rows = [
        f"### Test coverage (every module must reach {MIN_COVERAGE_PERCENT:g}%)",
        "",
        "| Module | Lines | Line % | Branches | Line + branch % | |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for module in modules:
        status = "ok" if module.passes else "**below the bar**"
        rows.append(
            f"| `{module.name}` | {module.lines_hit}/{module.lines} | {module.line_percent:.1f} "
            f"| {module.branches_hit}/{module.branches} | {module.percent:.1f} | {status} |"
        )
    failing = [module.name for module in modules if not module.passes]
    rows.append("")
    rows.append(
        f"Below {MIN_COVERAGE_PERCENT:g}%: {', '.join(failing)}"
        if failing
        else f"All {len(modules)} modules are at or above {MIN_COVERAGE_PERCENT:g}%."
    )
    return "\n".join(rows) + "\n"


def main(argv: list[str]) -> int:
    """Print the table, append it to the job summary if there is one, and gate on the bar."""
    path = Path(argv[1] if len(argv) > 1 else "coverage.xml")
    if not path.is_file():
        print(f"{path} not found: run pytest with --cov-report=xml first", file=sys.stderr)
        return 1
    modules = read_modules(path)
    if not modules:
        print(f"No modules found in {path}", file=sys.stderr)
        return 1

    table = render(modules)
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(table)
    return 0 if all(module.passes for module in modules) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
