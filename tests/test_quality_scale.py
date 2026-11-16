"""quality_scale.yaml must stay a valid self-audit of the Home Assistant quality scale.

Pure tests: they need PyYAML only. They check the file's structure (every rule once, a valid
status, a reason for every exemption), not whether a status is true; that is the audit's job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

FILE = (
    Path(__file__).parent.parent / "custom_components" / "citipower_myenergy" / "quality_scale.yaml"
)

BRONZE = (
    "action-setup appropriate-polling brands common-modules config-flow "
    "config-flow-test-coverage dependency-transparency docs-actions docs-conditions "
    "docs-high-level-description docs-installation-instructions docs-removal-instructions "
    "docs-triggers entity-event-setup entity-unique-id has-entity-name runtime-data "
    "test-before-configure test-before-setup unique-config-entry"
).split()
SILVER = (
    "action-exceptions config-entry-unloading docs-configuration-parameters "
    "docs-installation-parameters entity-unavailable integration-owner log-when-unavailable "
    "parallel-updates reauthentication-flow test-coverage"
).split()
GOLD = (
    "devices diagnostics discovery discovery-update-info docs-data-update docs-examples "
    "docs-known-limitations docs-supported-devices docs-supported-functions docs-troubleshooting "
    "docs-use-cases dynamic-devices entity-category entity-device-class "
    "entity-disabled-by-default entity-translations exception-translations icon-translations "
    "reconfiguration-flow repair-issues stale-devices"
).split()
PLATINUM = "async-dependency inject-websession strict-typing".split()
TIERS = {"bronze": BRONZE, "silver": SILVER, "gold": GOLD, "platinum": PLATINUM}
STATUSES = {"done", "todo", "exempt"}


@pytest.fixture(scope="module")
def rules() -> dict:
    data = yaml.safe_load(FILE.read_text(encoding="utf-8"))
    assert set(data) == {"rules"}, "the file has one top-level key, rules"
    return data["rules"]


def _status(rule: object) -> str:
    return rule if isinstance(rule, str) else rule["status"]


def test_the_scale_has_54_rules_in_four_tiers() -> None:
    assert [len(tier) for tier in TIERS.values()] == [20, 10, 21, 3]


def test_every_rule_is_present_exactly_once_and_nothing_else(rules: dict) -> None:
    expected = [slug for tier in TIERS.values() for slug in tier]
    assert sorted(rules) == sorted(expected)
    assert len(rules) == len(expected)


def test_every_status_is_valid(rules: dict) -> None:
    for slug, rule in rules.items():
        assert _status(rule) in STATUSES, slug


def test_every_rule_is_a_status_or_a_status_with_a_comment(rules: dict) -> None:
    for slug, rule in rules.items():
        assert isinstance(rule, str) or set(rule) <= {"status", "comment"}, slug


def test_every_exemption_says_why(rules: dict) -> None:
    for slug, rule in rules.items():
        if _status(rule) == "exempt":
            assert not isinstance(rule, str) and str(rule.get("comment", "")).strip(), slug


def test_a_todo_says_what_is_missing(rules: dict) -> None:
    for slug, rule in rules.items():
        if _status(rule) == "todo":
            assert not isinstance(rule, str) and str(rule.get("comment", "")).strip(), slug


def test_bronze_is_met(rules: dict) -> None:
    """The header claims Bronze on the self-audit; this fails if a Bronze rule goes back to todo."""
    open_rules = [slug for slug in BRONZE if _status(rules[slug]) == "todo"]
    assert open_rules == []


def test_silver_is_met(rules: dict) -> None:
    """The manifest claims Silver; this fails if a Silver rule goes back to todo."""
    open_rules = [slug for slug in SILVER if _status(rules[slug]) == "todo"]
    assert open_rules == []


def test_gold_is_not_claimed_while_rules_are_open(rules: dict) -> None:
    """A tier needs every rule below it too. Update this when Gold is genuinely met."""
    open_rules = [slug for slug in GOLD if _status(rules[slug]) == "todo"]
    assert open_rules == [
        "docs-supported-devices",
        "entity-translations",
        "exception-translations",
        "icon-translations",
    ]


def test_the_manifest_tier_is_one_the_audit_supports(rules: dict) -> None:
    """The manifest may claim a tier only when every rule of it and below is done or exempt."""
    manifest = json.loads((FILE.parent / "manifest.json").read_text(encoding="utf-8"))
    claimed = manifest.get("quality_scale")
    assert claimed in TIERS
    for tier, slugs in TIERS.items():
        for slug in slugs:
            assert _status(rules[slug]) != "todo", f"{slug} is todo but the manifest says {claimed}"
        if tier == claimed:
            break


def test_silver_and_above_need_an_owner(rules: dict) -> None:
    manifest = json.loads((FILE.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["codeowners"], "integration-owner is marked done"
    assert _status(rules["integration-owner"]) == "done"
