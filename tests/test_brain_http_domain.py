"""Brain `/brain` domain behavior via real FastAPI app + TestClient.

Uses ``paired_client`` / ``fastapi_app`` from ``conftest`` (PostgreSQL + migrations).
If these tests appear to hang, confirm ``TEST_DATABASE_URL`` points at a reachable DB.
"""

from __future__ import annotations

import re

import pytest


def _snippet_after_agent_feed(html: str) -> str:
    needle = 'id="agent-msg-feed"'
    i = html.find(needle)
    assert i != -1, "agent-msg-feed missing from /brain HTML"
    return html[i : i + 550]


@pytest.mark.parametrize(
    "path,expect_domain,trading_boot",
    [
        ("/brain?domain=project", "project", "deferred"),
        ("/brain?domain=trading", "trading", "immediate"),
        ("/brain?domain=reasoning", "reasoning", "deferred"),
    ],
)
def test_brain_get_domain_contract(paired_client, path, expect_domain, trading_boot) -> None:
    client, _user = paired_client
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]
    text = r.text
    assert f'data-brain-initial-domain="{expect_domain}"' in text
    assert f'data-trading-boot="{trading_boot}"' in text
    assert re.search(
        r"__CHILI_BRAIN_INITIAL_DOMAIN__\s*=\s*(?:\"|&#34;)"
        + re.escape(expect_domain)
        + r"(?:\"|&#34;)",
        text,
    ), "bootstrap global should match domain"


def test_brain_project_feed_static_not_switch_copy(paired_client) -> None:
    """Project route static feed must not use the off-project placeholder."""
    client, _user = paired_client
    r = client.get("/brain?domain=project")
    assert r.status_code == 200
    snip = _snippet_after_agent_feed(r.text)
    assert "Loading agent messages" in snip
    assert "data-agent-feed-off-project" not in snip
    assert "Switch to the Project domain" not in snip


def test_brain_trading_root_visible(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/brain?domain=trading")
    assert r.status_code == 200
    assert re.search(
        r'id="domain-trading"[^>]*style="[^"]*display:\s*flex',
        r.text,
    )


def test_brain_jobs_redirect(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/brain?domain=jobs", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers.get("location") == "/app/jobs"


def test_brain_planner_params_default_project_without_domain(paired_client) -> None:
    client, _user = paired_client
    r = client.get("/brain?planner_task_id=1")
    assert r.status_code == 200
    assert 'data-brain-initial-domain="project"' in r.text
