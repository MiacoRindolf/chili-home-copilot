"""Integration tests for the research-digest HTML report endpoint.

GET /api/brain/reasoning/research/report renders the user's stored
ReasoningResearch rows into a self-contained HTML page via app/visual_report.py.
"""
import json

from app.models import ReasoningResearch


_URL = "/api/brain/reasoning/research/report"


class TestResearchReport:
    def test_guest_gets_empty_digest(self, client):
        resp = client.get(_URL)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        body = resp.text
        assert "<!DOCTYPE html>" in body
        assert "No research yet" in body

    def test_paired_user_sees_their_research(self, paired_client, db):
        c, user = paired_client
        db.add(ReasoningResearch(
            user_id=user.id,
            topic="NVDA earnings outlook",
            summary="Guidance is the swing factor for the print on the 21st.",
            sources=json.dumps([
                {"title": "Reuters preview", "url": "https://www.reuters.com/nvda"},
            ]),
            relevance_score=0.9,
            stale=False,
        ))
        db.commit()

        resp = c.get(_URL)
        assert resp.status_code == 200
        body = resp.text
        assert "NVDA earnings outlook" in body
        assert "swing factor" in body
        assert "reuters.com" in body          # source rendered (domain)
        assert "Sources (1)" in body
        assert ">1<" in body or "Topics" in body  # stats bar present

    def test_stale_research_excluded(self, paired_client, db):
        c, user = paired_client
        db.add(ReasoningResearch(
            user_id=user.id, topic="Stale topic", summary="old", sources="[]",
            relevance_score=0.5, stale=True,
        ))
        db.commit()
        resp = c.get(_URL)
        assert resp.status_code == 200
        assert "Stale topic" not in resp.text
        assert "No research yet" in resp.text

    def test_download_sets_attachment_header(self, paired_client, db):
        c, _user = paired_client
        resp = c.get(_URL, params={"download": 1})
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "chili-research-digest.html" in resp.headers.get("content-disposition", "")

    def test_malformed_sources_does_not_crash(self, paired_client, db):
        c, user = paired_client
        db.add(ReasoningResearch(
            user_id=user.id, topic="Bad sources", summary="body",
            sources="{not valid json", relevance_score=0.5, stale=False,
        ))
        db.commit()
        resp = c.get(_URL)
        assert resp.status_code == 200
        assert "Bad sources" in resp.text

    def test_json_format_returns_structured_payload(self, paired_client, db):
        c, user = paired_client
        db.add(ReasoningResearch(
            user_id=user.id, topic="NVDA earnings", summary="guidance is key",
            sources='[{"title": "Reuters", "url": "https://reuters.com/n"}]',
            relevance_score=0.9, stale=False,
        ))
        db.commit()
        resp = c.get(_URL, params={"format": "json"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert data["ok"] is True
        assert data["topic_count"] == 1
        assert data["topics"][0]["topic"] == "NVDA earnings"
        assert data["sources"][0]["url"] == "https://reuters.com/n"

    def test_text_format_returns_plaintext(self, client):
        resp = client.get(_URL, params={"format": "text"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "<!DOCTYPE html>" not in resp.text
        assert "Research Digest" in resp.text

    def test_default_format_is_html(self, client):
        resp = client.get(_URL)
        assert resp.headers["content-type"].startswith("text/html")
