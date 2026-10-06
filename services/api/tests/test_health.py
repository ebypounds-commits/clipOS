"""Health endpoint contract tests."""

from __future__ import annotations

from clipos_api.main import APP_VERSION, health


def test_health_endpoint() -> None:
    body = health()
    assert body["status"] == "ok"
    assert body["version"] == APP_VERSION
    assert body["version"]


def test_health_route_is_wired() -> None:
    """GET /health must map to the health() handler (no HTTP client needed)."""
    from clipos_api.main import app, health

    matches = [route for route in app.routes if getattr(route, "path", None) == "/health"]
    assert matches, "no /health route registered"
    assert matches[0].endpoint is health
