"""Tests for Home Page modernization: models, chore scheduling, activity, dashboard."""
from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
from app.models import Chore, Birthday, User, Device, ActivityLog
from app.services import home_service
from app.pairing import DEVICE_COOKIE_NAME


def _make_paired(db):
    user = User(name="HomeUser")
    db.add(user)
    db.commit()
    db.refresh(user)
    token = "home-test-tok"
    db.add(Device(token=token, user_id=user.id, label="test", client_ip_last="127.0.0.1"))
    db.commit()
    return user, token


# ── Chore model tests ────────────────────────────────────────────────────────

class TestChoreModel:
    def test_chore_new_fields(self, db):
        c = Chore(
            title="Test", priority="high", due_date=date(2026, 4, 1),
            recurrence="weekly", created_at=datetime.utcnow(),
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        assert c.priority == "high"
        assert c.due_date == date(2026, 4, 1)
        assert c.recurrence == "weekly"
        assert c.created_at is not None

    def test_chore_assigned_to(self, db):
        user = User(name="Assignee")
        db.add(user)
        db.commit()
        db.refresh(user)
        c = Chore(title="Assigned", assigned_to=user.id)
        db.add(c)
        db.commit()
        db.refresh(c)
        assert c.assigned_to == user.id
        assert c.assignee.name == "Assignee"

    def test_chore_completed_at(self, db):
        c = Chore(title="Done", done=True, completed_at=datetime.utcnow())
        db.add(c)
        db.commit()
        db.refresh(c)
        assert c.completed_at is not None


# ── Activity log model tests ──────────────────────────────────────────────────

class TestActivityLog:
    def test_create_activity(self, db):
        entry = ActivityLog(
            event_type="chore_added", description="Test chore",
            user_name="TestUser", icon="plus",
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        assert entry.id is not None
        assert entry.event_type == "chore_added"

    def test_log_activity_service(self, db):
        home_service.log_activity(
            db, "chore_done", "Completed trash",
            user_name="Tester", icon="check",
        )
        entries = db.query(ActivityLog).all()
        assert len(entries) >= 1
        assert entries[-1].description == "Completed trash"


# ── Chore API tests ──────────────────────────────────────────────────────────

class TestChoreAPI:
    def test_add_chore_with_priority(self, db, client):
        resp = client.post("/api/chores", json={"title": "High chore", "priority": "high"})
        assert resp.status_code == 200
        assert resp.json()["chore"]["priority"] == "high"

    def test_add_chore_with_due_date(self, db, client):
        resp = client.post("/api/chores", json={"title": "Due chore", "due_date": "2026-04-15"})
        assert resp.status_code == 200
        assert resp.json()["chore"]["due_date"] == "2026-04-15"

    def test_add_chore_invalid_priority_defaults(self, db, client):
        resp = client.post("/api/chores", json={"title": "Bad priority", "priority": "ultra"})
        assert resp.status_code == 200
        assert resp.json()["chore"]["priority"] == "medium"

    def test_toggle_chore_sets_completed_at(self, db, client):
        resp = client.post("/api/chores", json={"title": "Toggle"})
        cid = resp.json()["chore"]["id"]
        resp2 = client.post(f"/api/chores/{cid}/done")
        assert resp2.json()["chore"]["done"] is True
        assert resp2.json()["chore"]["completed_at"] is not None

    def test_toggle_chore_unsets_completed_at(self, db, client):
        resp = client.post("/api/chores", json={"title": "Untoggle"})
        cid = resp.json()["chore"]["id"]
        client.post(f"/api/chores/{cid}/done")
        resp2 = client.post(f"/api/chores/{cid}/done")
        assert resp2.json()["chore"]["done"] is False
        assert resp2.json()["chore"]["completed_at"] is None

    def test_update_chore(self, db, client):
        resp = client.post("/api/chores", json={"title": "Old"})
        cid = resp.json()["chore"]["id"]
        resp2 = client.put(f"/api/chores/{cid}", json={"title": "New", "priority": "low"})
        assert resp2.status_code == 200
        assert resp2.json()["chore"]["title"] == "New"
        assert resp2.json()["chore"]["priority"] == "low"

    def test_update_nonexistent(self, db, client):
        resp = client.put("/api/chores/9999", json={"title": "X"})
        assert resp.status_code == 404


# ── Recurring chore tests ────────────────────────────────────────────────────

class TestRecurringChores:
    def test_weekly_recurrence_spawns(self, db, client):
        resp = client.post("/api/chores", json={
            "title": "Weekly", "due_date": "2026-03-02", "recurrence": "weekly"
        })
        cid = resp.json()["chore"]["id"]
        client.post(f"/api/chores/{cid}/done")
        next_chore = db.query(Chore).filter(
            Chore.title == "Weekly", Chore.due_date == date(2026, 3, 9), Chore.done == False,
        ).first()
        assert next_chore is not None

    def test_daily_recurrence_spawns(self, db, client):
        resp = client.post("/api/chores", json={
            "title": "Daily", "due_date": "2026-03-02", "recurrence": "daily"
        })
        cid = resp.json()["chore"]["id"]
        client.post(f"/api/chores/{cid}/done")
        next_chore = db.query(Chore).filter(
            Chore.title == "Daily", Chore.due_date == date(2026, 3, 3), Chore.done == False,
        ).first()
        assert next_chore is not None

    def test_no_recurrence_no_spawn(self, db, client):
        resp = client.post("/api/chores", json={
            "title": "Once", "due_date": "2026-03-02", "recurrence": "none"
        })
        cid = resp.json()["chore"]["id"]
        before_count = db.query(Chore).filter(Chore.title == "Once").count()
        client.post(f"/api/chores/{cid}/done")
        after_count = db.query(Chore).filter(Chore.title == "Once").count()
        assert after_count == before_count

    def test_no_duplicate_spawn(self, db, client):
        resp = client.post("/api/chores", json={
            "title": "NoDup", "due_date": "2026-03-02", "recurrence": "weekly"
        })
        cid = resp.json()["chore"]["id"]
        client.post(f"/api/chores/{cid}/done")
        client.post(f"/api/chores/{cid}/done")  # un-done
        client.post(f"/api/chores/{cid}/done")  # done again
        count = db.query(Chore).filter(
            Chore.title == "NoDup", Chore.due_date == date(2026, 3, 9),
        ).count()
        assert count == 1


# ── Dashboard API tests ──────────────────────────────────────────────────────

class TestDashboardAPI:
    def test_dashboard_returns_data(self, db, client):
        resp = client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert "chores" in data
        assert "birthdays" in data
        assert "pending_chores" in data
        assert "insights" in data

    def test_dashboard_with_chores(self, db, client):
        client.post("/api/chores", json={"title": "A"})
        client.post("/api/chores", json={"title": "B"})
        resp = client.get("/api/dashboard")
        data = resp.json()
        assert data["pending_chores"] >= 2


# ── Activity API tests ───────────────────────────────────────────────────────

class TestActivityAPI:
    def test_empty_activity(self, db, client):
        resp = client.get("/api/activity")
        assert resp.status_code == 200
        assert "events" in resp.json()

    def test_activity_after_chore(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        client.post("/api/chores", json={"title": "Test chore"})
        resp = client.get("/api/activity")
        events = resp.json()["events"]
        assert any("Test chore" in e["description"] for e in events)


# ── Calendar API tests ───────────────────────────────────────────────────────

class TestCalendarAPI:
    def test_calendar_returns_events(self, db, client):
        resp = client.get("/api/calendar?year=2026&month=3")
        assert resp.status_code == 200
        assert "events" in resp.json()
        assert resp.json()["year"] == 2026
        assert resp.json()["month"] == 3

    def test_calendar_with_chore(self, db, client):
        client.post("/api/chores", json={"title": "March chore", "due_date": "2026-03-15"})
        resp = client.get("/api/calendar?year=2026&month=3")
        events = resp.json()["events"]
        assert any(e["title"] == "March chore" for e in events)

    def test_calendar_with_birthday(self, db, client):
        client.post("/api/birthdays", json={"name": "Alice", "date": "2000-03-20"})
        resp = client.get("/api/calendar?year=2026&month=3")
        events = resp.json()["events"]
        assert any("Alice" in e["title"] for e in events)


# ── Home service insights tests ──────────────────────────────────────────────

class TestInsights:
    def test_all_done_insight(self, db):
        insights = home_service.get_insights(db)
        texts = [i["text"] for i in insights]
        assert any("All chores are done" in t for t in texts)

    def test_overdue_insight(self, db):
        c = Chore(title="Old", done=False, due_date=date(2020, 1, 1))
        db.add(c)
        db.commit()
        insights = home_service.get_insights(db)
        texts = [i["text"] for i in insights]
        assert any("overdue" in t for t in texts)

    def test_birthday_today_insight(self, db):
        today = date.today()
        db.add(Birthday(name="Bday Person", date=today.replace(year=2000)))
        db.commit()
        insights = home_service.get_insights(db)
        texts = [i["text"] for i in insights]
        assert any("Bday Person" in t and "today" in t for t in texts)


# ── Home service weather tests ───────────────────────────────────────────────

class TestWeather:
    @patch("app.services.home_service.requests.get")
    def test_weather_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "current_condition": [{
                "temp_F": "72", "temp_C": "22", "FeelsLikeF": "70", "FeelsLikeC": "21",
                "weatherDesc": [{"value": "Sunny"}], "humidity": "45",
                "windspeedMiles": "5", "weatherCode": "113",
            }]
        }
        mock_get.return_value = mock_resp
        home_service._WEATHER_CACHE["data"] = None
        home_service._WEATHER_CACHE["expires"] = 0

        result = home_service.get_weather("New York")
        assert result is not None
        assert result["temp_f"] == "72"
        assert result["description"] == "Sunny"
        assert result["icon"] == "sun"

    def test_weather_no_location(self):
        home_service.WEATHER_LOCATION = ""
        result = home_service.get_weather(None)
        assert result is None


# ── Home page rendering test ─────────────────────────────────────────────────

class TestHomePageRender:
    def test_home_page_loads(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "CHILI Home" in resp.text

    def test_home_page_has_dashboard(self, client):
        resp = client.get("/")
        assert "dashboard" in resp.text.lower()
        assert "calendar" in resp.text.lower()
        assert "activity" in resp.text.lower()
