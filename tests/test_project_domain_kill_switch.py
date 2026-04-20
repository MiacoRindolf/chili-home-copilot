"""G1 kill switch coverage: flipping ``settings.project_domain_enabled`` to
False closes the domain across every route that makes up the project cockpit.

Fills test-coverage gap C3 — integration tests for `/api/brain/code/*` and
`/api/brain/project/*` endpoints that were previously only asserted by route
registration.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def disable_project_domain(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "project_domain_enabled", False)
    yield


def test_bootstrap_returns_503_when_disabled(client, disable_project_domain):
    r = client.get("/api/brain/project/bootstrap")
    assert r.status_code == 503


def test_code_metrics_returns_503_when_disabled(client, disable_project_domain):
    r = client.get("/api/brain/code/metrics")
    assert r.status_code == 503


def test_code_repos_returns_503_when_disabled(client, disable_project_domain):
    r = client.get("/api/brain/code/repos")
    assert r.status_code == 503


def test_project_status_returns_503_when_disabled(client, disable_project_domain):
    r = client.get("/api/brain/project/status")
    assert r.status_code == 503


def test_domains_omits_project_when_disabled(client, disable_project_domain):
    r = client.get("/api/brain/domains")
    assert r.status_code == 200
    ids = [d["id"] for d in r.json().get("domains", [])]
    assert "project" not in ids
    assert "trading" in ids  # unaffected


def test_brain_page_redirects_project_deep_link_to_hub_when_disabled(
    client, disable_project_domain
):
    # Deep link to ?domain=project should silently fall back to the hub
    # instead of rendering a broken project pane.
    r = client.get("/brain?domain=project", follow_redirects=False)
    # Template is still served, but with brain_initial_domain coerced to hub.
    assert r.status_code == 200
    body = r.text
    # The pane include should NOT be in the HTML at all.
    assert 'id="domain-project"' not in body
    assert "brain-project-domain.js" not in body


def test_domains_includes_project_when_enabled(client):
    r = client.get("/api/brain/domains")
    assert r.status_code == 200
    ids = [d["id"] for d in r.json().get("domains", [])]
    assert "project" in ids
