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
    async def serve(socket_enabled, monkeypatch):
        """Start a local aiohttp server for the portal's routes and point the client at it.

        ``serve(routes)`` takes ``(method, path, handler)`` tuples and returns the server's origin.
        The portal's URLs live in module globals, so they are patched in every module that
        imported them by name. ``socket_enabled`` lifts the test package's socket ban; the server
        only listens on loopback.
        """
        from aiohttp import web
        from aiohttp.test_utils import TestServer

        from custom_components.citipower_myenergy import api, aura, auth

        servers: list[TestServer] = []

        async def _serve(routes) -> str:
            app = web.Application()
            for method, path, handler in routes:
                app.router.add_route(method, path, handler)
            server = TestServer(app)
            await server.start_server()
            servers.append(server)
            origin = str(server.make_url("")).rstrip("/")
            for module in (aura, auth, api):
                for name, path in (
                    ("BASE_URL", ""),
                    ("AURA_ENDPOINT", "/s/sfsites/aura"),
                    ("LOGIN_PAGE", "/s/login/"),
                    ("HOME_PAGE", "/s/"),
                    ("USAGE_PAGE", "/s/energy-usage"),
                    ("REMOTING_ENDPOINT", "/apexremote"),
                    ("SESSION_TIME_URL", "/session"),
                ):
                    if hasattr(module, name):
                        monkeypatch.setattr(module, name, f"{origin}{path}")
            return origin

        yield _serve
        for server in servers:
            await server.close()

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
