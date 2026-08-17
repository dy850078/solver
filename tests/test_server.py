"""Tests for HTTP server wiring (app/server.py).

Focus: the Swagger UI asset resolution, which must degrade gracefully.
`swagger-ui-bundle` is an optional docs dependency — deployments whose
package index does not carry it (closed networks) still have to boot.
"""

import builtins
import importlib
import sys

import pytest
from fastapi.testclient import TestClient


def _reload_server(monkeypatch, *, block_bundle: bool, static_dir: str | None):
    """Re-import app.server under simulated environment conditions.

    The asset source is resolved at import time, so the module has to be
    reloaded for each scenario rather than patched in place.
    """
    if static_dir is None:
        monkeypatch.delenv("SWAGGER_STATIC_DIR", raising=False)
    else:
        monkeypatch.setenv("SWAGGER_STATIC_DIR", static_dir)

    if block_bundle:
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "swagger_ui_bundle":
                raise ImportError("simulated: absent from this package index")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

    sys.modules.pop("app.server", None)
    module = importlib.import_module("app.server")
    return module


@pytest.fixture(autouse=True)
def _restore_server_module():
    """Leave app.server in its normal state for other test modules."""
    yield
    sys.modules.pop("app.server", None)
    importlib.import_module("app.server")


def _swagger_mounted(api) -> bool:
    return any(getattr(r, "name", "") == "swagger-static" for r in api.router.routes)


def test_swagger_assets_from_installed_bundle(monkeypatch):
    """Default path: the package is installed, /docs renders from it."""
    server = _reload_server(monkeypatch, block_bundle=False, static_dir=None)
    assert server._SWAGGER_STATIC_DIR is not None
    assert _swagger_mounted(server.api)
    assert TestClient(server.api).get("/docs").status_code == 200


def test_swagger_static_dir_overrides_bundle(monkeypatch, tmp_path):
    """A vendored copy wins over the package — the closed-network path."""
    vendored = tmp_path / "swagger-assets"
    vendored.mkdir()
    server = _reload_server(monkeypatch, block_bundle=True, static_dir=str(vendored))
    assert server._SWAGGER_STATIC_DIR == vendored
    assert _swagger_mounted(server.api)
    assert TestClient(server.api).get("/docs").status_code == 200


def test_service_boots_without_any_swagger_assets(monkeypatch):
    """No package and no override: /docs explains itself, everything else
    keeps working. Importing the bundle at module scope used to take the
    whole service down here."""
    server = _reload_server(monkeypatch, block_bundle=True, static_dir=None)
    assert server._SWAGGER_STATIC_DIR is None
    assert not _swagger_mounted(server.api)

    client = TestClient(server.api)
    docs = client.get("/docs")
    assert docs.status_code == 503
    assert "SWAGGER_STATIC_DIR" in docs.text      # says how to fix it
    # The service itself is unaffected.
    assert client.get("/health").json() == "ok"
    assert client.get("/openapi.json").status_code == 200


def test_both_health_paths_are_served():
    """/health and /healthz are both live. Probes are configured outside
    this repo, so serving only one turns a rename into a failed liveness
    check in whichever deployment used the other."""
    from app.server import api

    client = TestClient(api)
    assert client.get("/health").json() == "ok"
    assert client.get("/healthz").json() == {"status": "ok"}


def test_bad_swagger_static_dir_falls_back_to_bundle(monkeypatch, tmp_path):
    """A misconfigured override is a warning, not a boot failure."""
    server = _reload_server(
        monkeypatch, block_bundle=False, static_dir=str(tmp_path / "does-not-exist")
    )
    assert server._SWAGGER_STATIC_DIR is not None      # fell back to the package
    assert TestClient(server.api).get("/docs").status_code == 200
