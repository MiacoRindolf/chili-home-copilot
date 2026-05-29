"""Focused fail-closed coverage for admin paired-session dependency."""
from datetime import date, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
import pytest

from app.deps import get_db
from app.models import Birthday, ChatLog, Chore, Device, User
from app.pairing import DEVICE_COOKIE_NAME
from app.routers import admin


class _FakeQuery:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, *, device=None, user=None, chores=None, birthdays=None):
        self.device = device
        self.user = user
        self.chores = chores or []
        self.birthdays = birthdays or []
        self.mutations = []
        self.queries = []

    def query(self, model):
        self.queries.append(model)
        if model is Device:
            return _FakeQuery(self.device)
        if model is User:
            return _FakeQuery(self.user)
        if model is ChatLog:
            return _FakeQuery(rows=[])
        if model is Chore:
            return _FakeQuery(rows=self.chores)
        if model is Birthday:
            return _FakeQuery(rows=self.birthdays)
        raise AssertionError(f"admin handler unexpectedly queried {model!r}")

    def add(self, obj):
        self.mutations.append(("add", obj))
        raise AssertionError("admin handler should not mutate for unpaired requests")

    def commit(self):
        self.mutations.append(("commit", None))
        raise AssertionError("admin handler should not commit for unpaired requests")


def _client_for(fake_db):
    app = FastAPI()
    app.include_router(admin.router)
    app.state.templates = _FakeTemplates()

    def _override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class _FakeTemplates:
    def TemplateResponse(self, request, template_name, context):
        return HTMLResponse(f"{template_name}:{context.get('user_name', '')}")


def test_admin_page_guest_denied_before_handler(monkeypatch):
    def _must_not_run(_db):
        raise AssertionError("admin page handler should not run for guests")

    monkeypatch.setattr(admin, "admin_dashboard_json", _must_not_run)
    client = _client_for(_FakeSession())

    resp = client.get("/admin", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/chat"


def test_admin_api_guest_denied_before_handler(monkeypatch):
    def _must_not_run(_db):
        raise AssertionError("admin handler should not run for guests")

    monkeypatch.setattr(admin, "admin_dashboard_json", _must_not_run)
    client = _client_for(_FakeSession())

    resp = client.get("/api/admin/dashboard", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/chat"


def test_admin_api_unpaired_bearer_denied_before_handler(monkeypatch):
    def _must_not_run(_db):
        raise AssertionError("admin handler should not run for unpaired bearer tokens")

    monkeypatch.setattr(admin, "admin_dashboard_json", _must_not_run)
    client = _client_for(_FakeSession(device=None, user=None))

    resp = client.get(
        "/api/admin/dashboard",
        headers={"Authorization": "Bearer missing-device-token"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/chat"


def test_admin_mutation_guest_denied_before_handler():
    fake_db = _FakeSession()
    client = _client_for(fake_db)

    resp = client.post(
        "/admin/users",
        data={"name": "ShouldNotExist", "email": "blocked@test.com"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/chat"
    assert fake_db.mutations == []


@pytest.mark.parametrize(
    ("path", "export_model"),
    [
        ("/export/chores.csv", Chore),
        ("/export/birthdays.csv", Birthday),
    ],
)
def test_admin_exports_guest_denied_before_handler(path, export_model):
    fake_db = _FakeSession()
    client = _client_for(fake_db)

    resp = client.get(path, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/chat"
    assert export_model not in fake_db.queries


@pytest.mark.parametrize(
    ("path", "export_model"),
    [
        ("/export/chores.csv", Chore),
        ("/export/birthdays.csv", Birthday),
    ],
)
def test_admin_exports_unpaired_bearer_denied_before_handler(path, export_model):
    fake_db = _FakeSession(device=None, user=None)
    client = _client_for(fake_db)

    resp = client.get(
        path,
        headers={"Authorization": "Bearer missing-device-token"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/chat"
    assert export_model not in fake_db.queries


def test_admin_api_paired_cookie_allowed(monkeypatch):
    def _dashboard(_db):
        return {"health": {"ok": True}, "totals": {}}

    monkeypatch.setattr(admin, "admin_dashboard_json", _dashboard)
    fake_db = _FakeSession(
        device=SimpleNamespace(user_id=42),
        user=SimpleNamespace(id=42, name="BearerUser"),
    )
    client = _client_for(fake_db)
    client.cookies.set(DEVICE_COOKIE_NAME, "paired-cookie-token")

    resp = client.get("/api/admin/dashboard")

    assert resp.status_code == 200
    assert resp.json()["health"]["ok"] is True


def test_admin_api_paired_bearer_token_allowed(monkeypatch):
    def _dashboard(_db):
        return {"health": {"ok": True}, "totals": {}}

    monkeypatch.setattr(admin, "admin_dashboard_json", _dashboard)
    fake_db = _FakeSession(
        device=SimpleNamespace(user_id=42),
        user=SimpleNamespace(id=42, name="BearerUser"),
    )
    client = _client_for(fake_db)

    resp = client.get(
        "/api/admin/dashboard",
        headers={"Authorization": "Bearer paired-bearer-token"},
    )

    assert resp.status_code == 200
    assert resp.json()["health"]["ok"] is True


def test_admin_page_paired_bearer_token_allowed(monkeypatch):
    def _dashboard(_db):
        return {"health": {"ok": True}, "totals": {}}

    monkeypatch.setattr(admin, "admin_dashboard_json", _dashboard)
    fake_db = _FakeSession(
        device=SimpleNamespace(user_id=42),
        user=SimpleNamespace(id=42, name="BearerUser"),
    )
    client = _client_for(fake_db)

    resp = client.get(
        "/admin",
        headers={"Authorization": "Bearer paired-bearer-token"},
    )

    assert resp.status_code == 200
    assert "admin.html:BearerUser" in resp.text


def test_admin_export_chores_paired_bearer_token_allowed():
    fake_db = _FakeSession(
        device=SimpleNamespace(user_id=42),
        user=SimpleNamespace(id=42, name="BearerUser"),
        chores=[
            SimpleNamespace(
                id=1,
                title="Take trash",
                done=False,
                priority="high",
                due_date=date(2026, 5, 30),
                recurrence="weekly",
                assigned_to=42,
                created_at=datetime(2026, 5, 29, 8, 0, 0),
                completed_at=None,
            )
        ],
    )
    client = _client_for(fake_db)

    resp = client.get(
        "/export/chores.csv",
        headers={"Authorization": "Bearer paired-bearer-token"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "filename=chores.csv" in resp.headers["content-disposition"]
    assert "id,title,done,priority,due_date,recurrence,assigned_to,created_at,completed_at" in resp.text
    assert "1,Take trash,False,high,2026-05-30,weekly,42,2026-05-29T08:00:00," in resp.text


def test_admin_export_birthdays_paired_bearer_token_allowed():
    fake_db = _FakeSession(
        device=SimpleNamespace(user_id=42),
        user=SimpleNamespace(id=42, name="BearerUser"),
        birthdays=[SimpleNamespace(id=7, name="Ada", date=date(1815, 12, 10))],
    )
    client = _client_for(fake_db)

    resp = client.get(
        "/export/birthdays.csv",
        headers={"Authorization": "Bearer paired-bearer-token"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "filename=birthdays.csv" in resp.headers["content-disposition"]
    assert "id,name,date" in resp.text
    assert "7,Ada,1815-12-10" in resp.text
