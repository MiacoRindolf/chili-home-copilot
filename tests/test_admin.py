"""Tests for admin dashboard overhaul: analytics endpoints, metrics, alerts."""
import pytest
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock

from app.models import (
    User, Chore, Birthday, ChatMessage, Conversation,
    ChatLog, UserMemory, ActivityLog, Device, HousemateProfile,
)
from app.metrics import (
    per_user_chore_stats, system_alerts, admin_dashboard_json,
    latency_stats, record_latency, get_counts, model_stats,
    total_stats, messages_per_day, hourly_activity, feature_usage,
    response_time_trend, conversation_stats, top_users,
    _LATENCIES_MS,
)


class TestPerUserChoreStats:
    def test_no_users(self, db):
        result = per_user_chore_stats(db)
        assert result == []

    def test_user_with_no_chores(self, db):
        db.add(User(name="Alice"))
        db.commit()
        result = per_user_chore_stats(db)
        assert len(result) == 1
        assert result[0]["name"] == "Alice"
        assert result[0]["assigned"] == 0
        assert result[0]["rate"] == 0

    def test_user_with_mixed_chores(self, db):
        u = User(name="Bob")
        db.add(u)
        db.commit()
        db.refresh(u)
        db.add(Chore(title="A", assigned_to=u.id, done=True))
        db.add(Chore(title="B", assigned_to=u.id, done=False))
        db.add(Chore(title="C", assigned_to=u.id, done=True))
        db.commit()
        result = per_user_chore_stats(db)
        bob = [r for r in result if r["name"] == "Bob"][0]
        assert bob["assigned"] == 3
        assert bob["done"] == 2
        assert bob["rate"] == pytest.approx(66.7, abs=0.1)

    def test_multiple_users(self, db):
        u1 = User(name="Alice")
        u2 = User(name="Bob")
        db.add_all([u1, u2])
        db.commit()
        db.refresh(u1)
        db.refresh(u2)
        db.add(Chore(title="A", assigned_to=u1.id, done=True))
        db.add(Chore(title="B", assigned_to=u2.id, done=False))
        db.commit()
        result = per_user_chore_stats(db)
        names = {r["name"]: r for r in result}
        assert names["Alice"]["rate"] == 100.0
        assert names["Bob"]["rate"] == 0


class TestSystemAlerts:
    @patch("app.health.check_ollama", return_value={"ok": True})
    @patch("app.openai_client.is_configured", return_value=True)
    @patch("app.metrics.rag_stats", return_value={"available": True})
    def test_all_ok(self, mock_rag, mock_oai, mock_ollama, db):
        alerts = system_alerts(db)
        assert any(a["level"] == "ok" for a in alerts)

    @patch("app.health.check_ollama", return_value={"ok": False})
    @patch("app.openai_client.is_configured", return_value=True)
    @patch("app.metrics.rag_stats", return_value={"available": True})
    def test_ollama_offline(self, mock_rag, mock_oai, mock_ollama, db):
        alerts = system_alerts(db)
        assert any("Ollama" in a["text"] for a in alerts)

    @patch("app.health.check_ollama", return_value={"ok": True})
    @patch("app.openai_client.is_configured", return_value=True)
    @patch("app.metrics.rag_stats", return_value={"available": False})
    def test_rag_not_ingested(self, mock_rag, mock_oai, mock_ollama, db):
        alerts = system_alerts(db)
        assert any("RAG" in a["text"] for a in alerts)

    @patch("app.health.check_ollama", return_value={"ok": True})
    @patch("app.openai_client.is_configured", return_value=True)
    @patch("app.metrics.rag_stats", return_value={"available": True})
    def test_overdue_chores(self, mock_rag, mock_oai, mock_ollama, db):
        db.add(Chore(title="Overdue", done=False, due_date=date.today() - timedelta(days=2)))
        db.commit()
        alerts = system_alerts(db)
        assert any("overdue" in a["text"] for a in alerts)


class TestAdminDashboardJson:
    @patch("app.health.check_db", return_value={"ok": True})
    @patch("app.health.check_ollama", return_value={"ok": True})
    @patch("app.openai_client.is_configured", return_value=False)
    @patch("app.openai_client.OPENAI_MODEL", "test-model")
    def test_returns_all_keys(self, mock_oai, mock_ollama, mock_db, db):
        data = admin_dashboard_json(db)
        expected_keys = {
            "health", "totals", "counts", "latency", "latency_history",
            "model_stats", "action_types", "features", "messages_per_day",
            "hourly_activity", "response_time_trend", "conversation_stats",
            "top_users", "per_user_chores", "rag", "alerts",
        }
        assert expected_keys.issubset(set(data.keys()))


class TestAdminAPI:
    def test_dashboard_redirect_guest(self, client):
        resp = client.get("/admin", follow_redirects=False)
        assert resp.status_code == 303

    def test_dashboard_loads(self, paired_client):
        client, user = paired_client
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert "CHILI Admin" in resp.text

    def test_api_dashboard(self, paired_client):
        client, user = paired_client
        resp = client.get("/api/admin/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert "health" in data
        assert "totals" in data

    def test_api_alerts(self, paired_client):
        client, user = paired_client
        resp = client.get("/api/admin/alerts")
        assert resp.status_code == 200
        alerts = resp.json()
        assert isinstance(alerts, list)

    def test_api_logs(self, paired_client, db):
        client, user = paired_client
        db.add(ChatLog(
            action_type="chat", message="Hello",
            client_ip="127.0.0.1", trace_id="t1",
        ))
        db.commit()
        resp = client.get("/api/admin/logs")
        assert resp.status_code == 200
        logs = resp.json()
        assert len(logs) >= 1
        assert logs[0]["action"] == "chat"

    def test_api_user_memories(self, paired_client, db):
        client, user = paired_client
        db.add(UserMemory(
            user_id=user.id, category="preference",
            content="Likes coffee",
        ))
        db.commit()
        resp = client.get(f"/api/admin/user/{user.id}/memories")
        assert resp.status_code == 200
        mems = resp.json()
        assert len(mems) == 1
        assert mems[0]["content"] == "Likes coffee"

    def test_api_user_memories_excludes_superseded(self, paired_client, db):
        client, user = paired_client
        db.add(UserMemory(
            user_id=user.id, category="preference",
            content="Old fact", superseded=True,
        ))
        db.add(UserMemory(
            user_id=user.id, category="preference",
            content="New fact", superseded=False,
        ))
        db.commit()
        resp = client.get(f"/api/admin/user/{user.id}/memories")
        mems = resp.json()
        assert len(mems) == 1
        assert mems[0]["content"] == "New fact"


class TestAdminUsersPage:
    def test_users_page_loads(self, paired_client):
        client, user = paired_client
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        assert "Housemates" in resp.text

    def test_create_user(self, paired_client, db):
        client, user = paired_client
        resp = client.post("/admin/users", data={"name": "NewUser", "email": "new@test.com"}, follow_redirects=False)
        assert resp.status_code == 303
        assert db.query(User).filter(User.name == "NewUser").first() is not None

    def test_delete_user(self, paired_client, db):
        client, user = paired_client
        u = User(name="ToDelete")
        db.add(u)
        db.commit()
        db.refresh(u)
        resp = client.post(f"/admin/users/{u.id}/delete", follow_redirects=False)
        assert resp.status_code == 303
        assert db.query(User).filter(User.id == u.id).first() is None


class TestExportsEnhanced:
    def test_chores_csv_includes_new_fields(self, db, client):
        db.add(Chore(title="Test", done=False, priority="high", recurrence="weekly"))
        db.commit()
        resp = client.get("/export/chores.csv")
        assert resp.status_code == 200
        assert "priority" in resp.text
        assert "high" in resp.text

    def test_birthdays_csv(self, db, client):
        db.add(Birthday(name="Alice", date=date(2000, 3, 15)))
        db.commit()
        resp = client.get("/export/birthdays.csv")
        assert resp.status_code == 200
        assert "Alice" in resp.text
