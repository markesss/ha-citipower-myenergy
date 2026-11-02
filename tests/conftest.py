"""Test configuration.

The parsing tests (portal, NEM12, models) need nothing but the standard
library. The Home Assistant tests need pytest-homeassistant-custom-component;
without it they are skipped, and the integration package is registered as a
bare namespace so its pure modules can be imported without running
``__init__.py`` (which imports Home Assistant).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(ROOT))

HAS_HA = importlib.util.find_spec("pytest_homeassistant_custom_component") is not None

if HAS_HA:
    pytest_plugins = "pytest_homeassistant_custom_component"
else:
    for name, path in (
        ("custom_components", ROOT / "custom_components"),
        ("custom_components.citipower_myenergy", ROOT / "custom_components/citipower_myenergy"),
    ):
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules.setdefault(name, module)


if HAS_HA:

    @pytest.fixture
    def custom_integration(enable_custom_integrations):
        """Make the custom component loadable (not autouse: recorder tests
        must set up the database before hass exists)."""
        return


def pytest_ignore_collect(collection_path: Path, config) -> bool | None:
    """Skip the Home Assistant tests when Home Assistant is not installed."""
    if not HAS_HA and collection_path.name.startswith("test_ha_"):
        return True
    return None


def load_text(name: str) -> str:
    """Return a fixture file's text."""
    return (FIXTURES / name).read_text()


def load_json(name: str):
    """Return a fixture file parsed as JSON."""
    return json.loads(load_text(name))


@pytest.fixture
def fixture_text():
    """Load a fixture's text."""
    return load_text


@pytest.fixture
def fixture_json():
    """Load a fixture as JSON."""
    return load_json
