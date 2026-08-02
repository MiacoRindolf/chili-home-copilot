"""Captured-paper observer endpoint — read-only, fail-soft (2026-08-02).

Ang bawat query block ay may sariling statement_timeout at fail-soft None —
ang endpoint ay HINDI dapat mag-500 o mag-hang kahit wala ang captured_paper
tables (gaya sa test DB) o nakabara ang isang table. Ito ang mismong sakit ng
momentum desk endpoints na dating nagha-hang nang 45s+.
"""
from __future__ import annotations


def test_captured_paper_observer_fail_soft_shape(client):
    r = client.get("/api/trading/observer/captured-paper")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    for k in ("service", "frontier", "selection", "orders", "tape", "generated_at"):
        assert k in d, k
    # fail-soft: kahit walang data/tables, may hugis pa rin ang bawat block
    assert "heartbeat_age_s" in d["service"]
    assert "status" in d["frontier"]
    assert "outbox_total" in d["orders"]
    assert "age_s" in d["tape"]


def test_captured_paper_observer_multiple_calls_stable(client):
    # Ang rollback-in-block ay hindi dapat mag-iwan ng sirang session state.
    for _ in range(3):
        r = client.get("/api/trading/observer/captured-paper")
        assert r.status_code == 200
        assert r.json()["ok"] is True
