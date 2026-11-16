"""The YAML examples in docs/TARIFF_YAML.md must stay valid tariffs.

Pure tests: they read the doc, take every ```yaml block and build a Tariff from it. They need
PyYAML and a time zone database (``tzdata`` on Windows).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from custom_components.citipower_myenergy.tariff import Tariff

yaml = pytest.importorskip("yaml")

DOC = Path(__file__).parent.parent / "docs" / "TARIFF_YAML.md"
BLOCKS = re.findall(r"```yaml\n(.*?)```", DOC.read_text(encoding="utf-8"), re.DOTALL)


def test_the_doc_has_examples() -> None:
    assert len(BLOCKS) >= 3


@pytest.mark.parametrize("block", BLOCKS, ids=[f"example-{i + 1}" for i in range(len(BLOCKS))])
def test_example_is_a_valid_tariff(block: str) -> None:
    tariff = Tariff.from_dict(yaml.safe_load(block))
    assert tariff.versions


def test_the_rate_change_example_has_two_versions() -> None:
    tariff = Tariff.from_dict(yaml.safe_load(BLOCKS[2]))
    assert len(tariff.versions) == 2


def test_the_stored_form_round_trips_through_yaml() -> None:
    """What the editor shows (Tariff.to_dict as YAML) must load back to the same tariff."""
    tariff = Tariff.from_dict(yaml.safe_load(BLOCKS[0]))
    again = Tariff.from_dict(yaml.safe_load(yaml.safe_dump(tariff.to_dict())))
    assert again == tariff
