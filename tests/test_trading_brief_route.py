"""Integration tests for GET /api/brain/trading/brief.

Renders the user's daily trading brief as self-contained HTML via
trading_summary -> trading_brief -> visual_report. Read-only.
"""
from datetime import datetime

from app.models import Trade


_URL = "/api/brain/trading/brief"


class TestTradingBriefRoute:
    def test_guest_gets_empty_brief(self, client):
        resp = client.get(_URL)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        body = resp.text
        assert "<!DOCTYPE html>" in body
        assert "Daily Trading Brief" in body
        assert "No trading activity to report" in body

    def test_paired_user_sees_closed_trade(self, paired_client, db):
        c, user = paired_client
        db.add(Trade(
            user_id=user.id, ticker="AAPL", direction="long",
            entry_price=100.0, exit_price=110.0, quantity=1.0,
            entry_date=datetime.utcnow(), exit_date=datetime.utcnow(),
            status="closed", pnl=10.0, exit_reason="target",
        ))
        db.commit()
        resp = c.get(_URL)
        assert resp.status_code == 200
        body = resp.text
        assert "Daily Trading Brief" in body   # hero title (not stolen)
        assert "Performance" in body           # section present
        assert "AAPL" in body                  # the closed trade
        assert "+$10.00" in body               # net P/L formatted

    def test_download_sets_attachment_header(self, paired_client, db):
        c, _user = paired_client
        resp = c.get(_URL, params={"download": 1})
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "chili-trading-brief.html" in resp.headers.get("content-disposition", "")

    def test_bad_window_rejected_by_validation(self, client):
        # window_hours has ge=1, le=720 — out of range should 422, not 500.
        resp = client.get(_URL, params={"window_hours": 0})
        assert resp.status_code == 422

    def test_json_format_returns_structured_payload(self, client):
        resp = client.get(_URL, params={"format": "json"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert data["ok"] is True
        assert "summary" in data and "stats" in data and "sources" in data
        assert data["title"] == "Daily Trading Brief"

    def test_text_format_returns_plaintext(self, client):
        resp = client.get(_URL, params={"format": "text"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        # plaintext, not HTML
        assert "<!DOCTYPE html>" not in resp.text
        assert "Daily Trading Brief" in resp.text

    def test_text_download_header(self, client):
        resp = client.get(_URL, params={"format": "text", "download": 1})
        assert resp.status_code == 200
        assert "chili-trading-brief.txt" in resp.headers.get("content-disposition", "")

    def test_default_format_is_html(self, client):
        resp = client.get(_URL)
        assert resp.headers["content-type"].startswith("text/html")
