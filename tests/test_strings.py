"""strings.json and translations/en.json must satisfy what hassfest checks about them.

Pure tests. hassfest is the authority (it runs in CI), but it only runs on a push to main, a pull
request, nightly or by hand, so the rules that have already failed it are checked here too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DIR = Path(__file__).parent.parent / "custom_components" / "citipower_myenergy"
FILES = {"strings.json": DIR / "strings.json", "en.json": DIR / "translations" / "en.json"}


@pytest.fixture(params=sorted(FILES))
def strings(request: pytest.FixtureRequest) -> dict:
    return json.loads(FILES[request.param].read_text(encoding="utf-8"))


def test_an_issue_with_a_fix_flow_has_no_description_of_its_own(strings: dict) -> None:
    """hassfest: 'two or more values in the same group of exclusion fixable'.

    A repairable issue keeps its text inside fix_flow.step.<step>.description; a top-level
    description is only for an issue the user cannot fix from the Repairs page.
    """
    for key, issue in strings.get("issues", {}).items():
        if "fix_flow" in issue:
            assert "description" not in issue, key
            assert "title" in issue, key
        else:
            assert "description" in issue, key


def test_the_fix_flow_step_text_names_the_statistic_and_hour(strings: dict) -> None:
    text = strings["issues"]["chain_break"]["fix_flow"]["step"]["confirm"]["description"]
    assert "{statistic}" in text and "{hour}" in text


def test_the_translation_file_matches_strings_json_for_issues() -> None:
    texts = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in FILES.items()}
    assert texts["strings.json"]["issues"] == texts["en.json"]["issues"]
