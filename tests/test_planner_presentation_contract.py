"""Planner editor/embedding contracts without app startup or a database.

The router imports the coding workflow subsystem. Compile its actual selected
handlers (including their decorators and annotations) into an isolated router
instead, using the real request schemas and stubbed persistence. This exercises
FastAPI validation and the current handler bodies without importing app.main.
"""
from __future__ import annotations

import ast
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import PlanTask, User
from app.pairing import DEVICE_COOKIE_NAME
from app.schemas.planner import TaskBody, TaskUpdateBody
from app.services import planner_service


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_database_connections(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Planner presentation contract tests must not connect to a database")

    monkeypatch.setattr(Engine, "connect", forbidden)
    monkeypatch.setattr(Engine, "raw_connection", forbidden)


@pytest.fixture
def planner_http():
    state = SimpleNamespace(
        identity={"is_guest": False, "user_id": 7, "user_name": "Reader"},
        projects=[], users=[], user_summaries=[],
    )
    fake_db = SimpleNamespace(query=lambda model: SimpleNamespace(all=lambda: state.users))
    service = SimpleNamespace(
        update_task=Mock(return_value={"id": 41}),
        create_task=Mock(return_value={"id": 42}),
        list_projects=lambda *_args: state.projects,
        get_all_users_task_summary=lambda *_args: state.user_summaries,
    )

    def get_db():
        raise AssertionError("The isolated router must use its fake dependency")

    router = APIRouter()
    namespace = {
        "router": router, "Depends": Depends, "Request": Request,
        "Session": Session, "JSONResponse": JSONResponse,
        "HTMLResponse": HTMLResponse, "get_db": get_db,
        "TaskBody": TaskBody, "TaskUpdateBody": TaskUpdateBody,
        "planner_service": service, "User": User, "json_mod": json,
        "DEVICE_COOKIE_NAME": DEVICE_COOKIE_NAME,
        "get_identity_record": lambda *_args: state.identity,
    }
    path = ROOT / "app/routers/planner.py"
    source = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {"_require_user", "planner_page", "api_create_task", "api_update_task"}
    functions = [node for node in source.body
                 if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {node.name for node in functions} == wanted
    # Avoid inheriting this test module's future annotations: use the genuine
    # Pydantic classes in endpoint annotations, as the router itself does.
    code = compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec",
                   dont_inherit=True)
    exec(code, namespace)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: fake_db
    app.state.templates = Jinja2Templates(directory=str(ROOT / "app/templates"))
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, state=state, service=service, db=fake_db)


@pytest.mark.parametrize("payload", [
    {"description": "  ## Evidence\r\n\r\nReceipt ends here  \r\n"},
    {"description": ""},
    {"status": "blocked"},
    {"title": "Revised title"},
    {"start_date": None},
])
def test_update_accepts_only_supplied_fields_without_injecting_defaults(planner_http, payload):
    response = planner_http.client.put("/api/planner/tasks/41", json=payload)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "task": {"id": 41}}
    planner_http.service.update_task.assert_called_once_with(
        planner_http.db, 41, 7, **payload,
    )


@pytest.mark.parametrize("title", ["", " ", "\t\r\n"])
def test_blank_update_title_is_rejected_before_persistence(planner_http, title):
    response = planner_http.client.put("/api/planner/tasks/41", json={"title": title})

    assert response.status_code == 422
    assert any(error["loc"] == ["body", "title"] for error in response.json()["detail"])
    planner_http.service.update_task.assert_not_called()


@pytest.mark.parametrize("payload", [{}, {"description": "A draft without a title"}, {"title": None}])
def test_create_still_requires_a_title(planner_http, payload):
    response = planner_http.client.post("/api/planner/projects/9/tasks", json=payload)

    assert response.status_code == 422
    planner_http.service.create_task.assert_not_called()


def test_create_with_a_title_still_uses_the_create_handler(planner_http):
    response = planner_http.client.post("/api/planner/projects/9/tasks", json={"title": "New task"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "task": {"id": 42}}
    assert planner_http.service.create_task.call_args.args == (planner_http.db, 9, 7, "New task")


@pytest.mark.parametrize("identity", [
    {"is_guest": True, "user_id": 7},
    {"is_guest": False, "user_id": None},
])
@pytest.mark.parametrize("method,path,payload", [
    ("put", "/api/planner/tasks/41", {"description": "New evidence"}),
    ("post", "/api/planner/projects/9/tasks", {"title": "New task"}),
])
def test_pairing_denial_is_preserved(planner_http, identity, method, path, payload):
    planner_http.state.identity = identity
    response = planner_http.client.request(method, path, json=payload)

    assert response.status_code == 403
    assert response.json() == {"error": "Not paired"}
    planner_http.service.update_task.assert_not_called()
    planner_http.service.create_task.assert_not_called()


class _ScriptInventory(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.tags = []
        self._current_script = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._current_script = []
            self.scripts.append(self._current_script)

    def handle_endtag(self, tag):
        if tag == "script":
            self._current_script = None

    def handle_data(self, data):
        if self._current_script is not None:
            self._current_script.append(data)


def test_actual_template_keeps_hostile_json_in_one_script_and_recovers_original_data(planner_http):
    baseline = planner_http.client.get("/planner")
    assert baseline.status_code == 200
    baseline_scripts = _ScriptInventory(baseline.text).scripts
    payload = ('</script><script id="planner-injected">globalThis.__plannerProbe=1</script>'
               '<img src="planner-injected" onerror="globalThis.__plannerProbe=2">'
               " & ' \u2028 \u2029")
    projects = [{"id": 9, "name": "Project " + payload,
                 "description": "  ## Evidence\r\n" + payload + "\r\n  "}]
    users = [{"id": 7, "name": "User " + payload}]
    summaries = [{"user_id": 7, "user_name": "Summary " + payload}]
    planner_http.state.projects = projects
    planner_http.state.users = [SimpleNamespace(**user) for user in users]
    planner_http.state.user_summaries = summaries
    planner_http.state.identity["user_name"] = users[0]["name"]

    response = planner_http.client.get("/planner")

    assert response.status_code == 200
    parsed = _ScriptInventory(response.text)
    assert len(parsed.scripts) == len(baseline_scripts)
    assert not any(attrs.get("id") == "planner-injected" or
                   attrs.get("src") == "planner-injected" for _tag, attrs in parsed.tags)
    scripts = ["".join(parts) for parts in parsed.scripts]
    data_scripts = [script for script in scripts if "var DATA = " in script]
    assert len(data_scripts) == 1
    match = re.search(r"var DATA = JSON\.parse\((.+)\);", data_scripts[0])
    assert match is not None
    # Decode the emitted JS JSON string, then the original JSON payload. No
    # malicious content is evaluated in Node or opened in a browser.
    recovered = json.loads(json.loads(match.group(1)))
    assert recovered == {"projects": projects, "users": users, "user_summaries": summaries}


@pytest.fixture
def fake_task_storage(monkeypatch):
    task = SimpleNamespace(
        id=41, project_id=9, title="Original title", description="  Original\r\n  ",
        priority="high", status="in_progress", project=SimpleNamespace(updated_at=None),
    )
    query = SimpleNamespace(filter=lambda *_args: SimpleNamespace(first=lambda: task))
    db = SimpleNamespace(query=Mock(return_value=query), commit=Mock(), refresh=Mock())
    can_edit = Mock(return_value=True)
    activity = Mock()
    monkeypatch.setattr(planner_service, "_user_can_edit", can_edit)
    monkeypatch.setattr(planner_service, "_log_activity", activity)
    monkeypatch.setattr(planner_service, "_task_dict", lambda value: {
        "id": value.id, "title": value.title, "description": value.description,
        "priority": value.priority, "status": value.status,
    })
    return SimpleNamespace(db=db, task=task, can_edit=can_edit, activity=activity)


@pytest.mark.parametrize("description", [
    "  ## Evidence\r\n\r\n```python\r\n  print('receipt')\r\n```\r\n  ",
    " \t\r\n ",
    "",
])
def test_actual_update_service_preserves_description_whitespace(fake_task_storage, description):
    storage = fake_task_storage
    result = planner_service.update_task(storage.db, 41, 7, description=description)

    assert storage.task.description == description
    assert result == {"id": 41, "title": "Original title", "description": description,
                      "priority": "high", "status": "in_progress"}
    storage.db.query.assert_called_once_with(PlanTask)
    storage.can_edit.assert_called_once_with(storage.db, 9, 7)
    storage.db.commit.assert_called_once()
    storage.db.refresh.assert_called_once_with(storage.task)


def test_actual_title_only_update_leaves_description_untouched(fake_task_storage):
    storage = fake_task_storage
    original_description = storage.task.description
    result = planner_service.update_task(storage.db, 41, 7, title="New title")

    assert result["title"] == "New title"
    assert result["description"] == original_description
    assert storage.task.description == original_description
