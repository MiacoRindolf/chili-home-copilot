"""Brain page: server-driven initial domain, template contract, normalization."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request

from app.routers.brain import _brain_initial_domain_for_request

_TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"
_STATIC_COMPONENTS = Path(__file__).resolve().parents[1] / "app" / "static" / "components"


@pytest.fixture
def brain_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["tojson"] = lambda obj: json.dumps(obj, default=str)
    return env


def _render_brain(brain_jinja_env: Environment, brain_initial_domain: str, **kwargs) -> str:
    ctx = {
        "title": "Chili Brain",
        "is_guest": False,
        "user_name": "Test",
        "planner_task_id": None,
        "planner_project_id": None,
        "brain_initial_domain": brain_initial_domain,
        "trading_brain_desk_config": {},
        "trading_brain_neural_first_paint": False,
    }
    ctx.update(kwargs)
    return brain_jinja_env.get_template("brain.html").render(**ctx)


def _make_request(query_string: bytes) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/brain",
            "headers": [],
            "query_string": query_string,
        }
    )


@pytest.mark.parametrize(
    "qs,planner_tid,planner_pid,expected",
    [
        (b"domain=project", None, None, "project"),
        (b"domain=trading", None, None, "trading"),
        (b"domain=reasoning", None, None, "reasoning"),
        (b"domain=hub", None, None, "hub"),
        (b"domain=code", None, None, "project"),
        (b"domain=trading&planner_task_id=1", 1, None, "trading"),
        (b"planner_task_id=1", 1, None, "project"),
        (b"planner_project_id=2", None, 2, "project"),
        (b"", None, None, "hub"),
        (b"domain=not_a_domain", None, None, "hub"),
    ],
)
def test_brain_initial_domain_normalization(qs, planner_tid, planner_pid, expected) -> None:
    req = _make_request(qs)
    assert (
        _brain_initial_domain_for_request(req, planner_tid, planner_pid) == expected
    )


def test_brain_initial_domain_jobs_flag() -> None:
    req = _make_request(b"domain=jobs")
    assert _brain_initial_domain_for_request(req, None, None) == "jobs"


def test_brain_template_project_contract(brain_jinja_env) -> None:
    text = _render_brain(brain_jinja_env, "project")
    assert 'data-brain-initial-domain="project"' in text
    assert 'data-trading-boot="deferred"' in text
    assert '/static/components/brain-project-domain.js' in text
    assert '/static/components/brain-project-agents.js' in text
    assert '/static/components/brain-project-handoff.js' in text
    assert '/static/components/brain-project-domain.css' in text
    assert "__CHILI_BRAIN_INITIAL_DOMAIN__" in text
    assert re.search(
        r"__CHILI_BRAIN_INITIAL_DOMAIN__\s*=\s*(?:\"|&#34;)project(?:\"|&#34;)",
        text,
    )
    assert re.search(
        r'id="domain-project"[^>]*style="[^"]*display:\s*block',
        text,
    ), "project root should be display:block on first paint"
    assert text.count('id="domain-project"') == 1
    assert "Developer Cockpit" in text
    assert "Loading workspace bootstrap" in text
    assert 'data-project-pane="workspace"' in text
    assert 'class="brain-act-btn project-pane-tab is-active"' in text
    assert 'project-section-card' in text
    assert re.search(
        r'id="domain-trading"[^>]*style="[^"]*display:\s*none',
        text,
    ), "trading root should be display:none on project route"
    assert "Loading agent messages" in text
    assert re.search(
        r'id="agent-msg-feed"[^>]*>[\s\S]{0,400}?Loading agent messages',
        text,
    )
    assert "function loadAgentMessageFeed()" not in text
    assert "function loadCodeRepos()" not in text
    assert "function triggerCodeLearn()" not in text
    assert "function runCodeAgent()" not in text
    assert "function initProjectAgentBar()" not in text
    assert "function loadPODashboard()" not in text
    assert "function initBrainPlannerHandoffBridge()" not in text
    assert "function brainHandoffPlannerTaskId()" not in text


def test_project_template_is_split_into_partials() -> None:
    brain_template = (_TEMPLATES / "brain.html").read_text(encoding="utf-8")
    assert '/static/components/brain-project-domain.css' in brain_template
    assert ".proj-agent-bar {" not in brain_template
    assert ".code-repo-card {" not in brain_template
    assert ".chili-handoff-launch-flash {" not in brain_template

    domain_template = (_TEMPLATES / "brain_project_domain.html").read_text(encoding="utf-8")
    assert '{% include "brain_project_workspace_header.html" %}' in domain_template
    assert '{% include "brain_project_pane_handoff.html" %}' in domain_template
    assert '{% include "brain_project_pane_workspace.html" %}' in domain_template
    assert '{% include "brain_project_pane_agents.html" %}' in domain_template
    assert '{% include "brain_project_pane_feed.html" %}' in domain_template

    agents_template = (_TEMPLATES / "brain_project_pane_agents.html").read_text(encoding="utf-8")
    assert '{% include "brain_project_agent_panel_product_owner.html" %}' in agents_template
    assert '{% include "brain_project_agent_panel_project_manager.html" %}' in agents_template
    assert '{% include "brain_project_agent_panel_architect.html" %}' in agents_template

    project_styles = (_STATIC_COMPONENTS / "brain-project-domain.css").read_text(encoding="utf-8")
    assert ".proj-agent-bar {" in project_styles
    assert ".code-repo-card {" in project_styles
    assert ".chili-handoff-launch-flash {" in project_styles


def test_brain_template_trading_contract(brain_jinja_env) -> None:
    text = _render_brain(brain_jinja_env, "trading")
    assert 'data-brain-initial-domain="trading"' in text
    assert 'data-trading-boot="immediate"' in text
    assert re.search(
        r'id="domain-trading"[^>]*style="[^"]*display:\s*flex',
        text,
    ), "trading root should be display:flex on trading route"
    assert "Learning-cycle controls" in text
    assert "/trading/autopilot" in text
    assert "Current market thesis" in text


def test_brain_template_reasoning_contract(brain_jinja_env) -> None:
    text = _render_brain(brain_jinja_env, "reasoning")
    assert 'data-brain-initial-domain="reasoning"' in text


def test_brain_template_hub_default_when_domain_missing(brain_jinja_env) -> None:
    """Jinja default matches server when context omits key (defensive)."""
    ctx = {
        "title": "Chili Brain",
        "is_guest": False,
        "user_name": "Test",
        "planner_task_id": None,
        "planner_project_id": None,
        "trading_brain_desk_config": {},
        "trading_brain_neural_first_paint": False,
    }
    text = brain_jinja_env.get_template("brain.html").render(**ctx)
    assert 'data-brain-initial-domain="hub"' in text
