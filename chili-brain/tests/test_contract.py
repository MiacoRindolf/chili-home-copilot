"""Contract tests for chili-brain HTTP service (no PostgreSQL; lives outside ``tests/`` to avoid root conftest)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Repo root: chili-brain/tests -> chili-brain -> repo
ROOT = Path(__file__).resolve().parents[2]
_BRAIN_PKG = ROOT / "chili-brain"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if _BRAIN_PKG.is_dir() and str(_BRAIN_PKG) not in sys.path:
    sys.path.insert(0, str(_BRAIN_PKG))


@pytest.fixture
def brain_app():
    os.environ["CHILI_BRAIN_INTERNAL_SECRET"] = ""
    from chili_brain_service.main import app as brain_application

    return brain_application


def test_brain_health(brain_app):
    client = TestClient(brain_app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("service") == "chili-brain"


def test_brain_openapi_info(brain_app):
    client = TestClient(brain_app)
    r = client.get("/openapi.json")
    assert r.status_code == 200
    data = r.json()
    assert "CHILI Brain Service" in (data.get("info") or {}).get("title", "")


def test_brain_capabilities(brain_app):
    client = TestClient(brain_app)
    r = client.get("/v1/capabilities")
    assert r.status_code == 200
    assert "implemented" in r.json()


def test_brain_placeholder_501(brain_app):
    client = TestClient(brain_app)
    r = client.post("/v1/run-code-learning-cycle")
    assert r.status_code == 501
